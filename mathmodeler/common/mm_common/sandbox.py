"""mm_common.sandbox — Bubblewrap session isolation for S3 Files shared mount.

S3 Files mounts the same filesystem across all sessions. To prevent one session
from seeing another's files, we wrap shell commands in a bubblewrap (bwrap) mount
namespace that hides all session directories except the current one.

Usage:
    from mm_common.sandbox import wrap_command
    actual_cmd = wrap_command(command, session_id)
    # actual_cmd runs in a namespace where /mnt/workspace/jobs only contains
    # the current session's directory.
"""
from __future__ import annotations

import os
import logging

logger = logging.getLogger("mm.sandbox")

BWRAP_PATH = "/usr/bin/bwrap"
WORKSPACE_ROOT = os.environ.get("MM_WORKSPACE_ROOT", "/mnt/workspace/jobs")


def _is_available() -> bool:
    return os.path.isfile(BWRAP_PATH) and os.access(BWRAP_PATH, os.X_OK)


def wrap_command(command: str, session_id: str, cwd: str | None = None) -> str:
    """Wrap a shell command in bwrap for session-level filesystem isolation.

    Creates a mount namespace where:
    - Full rootfs is bind-mounted read-write (system tools work)
    - WORKSPACE_ROOT is replaced with an empty tmpfs (hides all sessions)
    - Only the current session's directory is bind-mounted back
    - /dev and /proc are provided

    Falls back to bare command if bwrap is not available.
    """
    if not _is_available():
        logger.warning("[sandbox] bwrap not available, running without isolation")
        return command

    session_dir = f"{WORKSPACE_ROOT}/{session_id}"
    os.makedirs(session_dir, exist_ok=True)

    if cwd is None:
        cwd = session_dir

    escaped_cmd = command.replace("'", "'\\''")

    return (
        f"{BWRAP_PATH} "
        f"--bind / / "
        f"--tmpfs {WORKSPACE_ROOT} "
        f"--bind {session_dir} {session_dir} "
        f"--dev /dev "
        f"--proc /proc "
        f"--chdir {cwd} "
        f"-- bash -c '{escaped_cmd}'"
    )
