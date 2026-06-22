"""mm_common.workspace — unified session workspace file management.

All intermediate artifacts (analysis, modeling, solving) are stored in a local
filesystem workspace. Files are also persisted to S3 (``jobs/{session_id}/...``)
as a reliable persistence layer that survives abnormal disconnects.

Path layout:
    {WORKSPACE_ROOT}/{session_id}/
        data/               ← user-uploaded data files
        analysis/
            actor.md        ← initial analysis
            critic.md       ← self-critique
            improve.md      ← improved analysis
            task_descriptions.json
            dag.json
        modeling/
            {task_id}.json
        solving/
            {task_id}.py
            {task_id}.json
            figures/         ← generated charts/images
        report/
            report.tex       ← LaTeX source (written incrementally)
            report.pdf       ← compiled PDF (final artifact → S3)
            figures/         ← referenced images

Environment:
    MM_WORKSPACE_ROOT:
        - Local dev: ./jobs (relative to project root)
        - AgentCore Runtime: /mnt/workspace/jobs (Managed filesystem, 14-day persistence)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("mm.workspace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORKSPACE_ROOT: Path = Path(os.getenv("MM_WORKSPACE_ROOT", "./jobs")).resolve()


def _ensure_root() -> None:
    """Create workspace root if it doesn't exist."""
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# S3 Sync — polling watcher thread that monitors workspace for changes
# ---------------------------------------------------------------------------
_s3_client = None
_s3_watcher_started = False
_s3_file_state: dict[str, float] = {}  # {absolute_path: mtime}


def _s3_enabled() -> bool:
    """Return True when S3 sync is configured (DOC_BUCKET set)."""
    from . import config
    return bool(config.DOC_BUCKET)


def _get_s3_client():
    """Lazy-init a boto3 S3 client (reused across the background thread)."""
    global _s3_client
    if _s3_client is None:
        import boto3
        from . import config
        _s3_client = boto3.client("s3", region_name=config.REGION)
    return _s3_client


def _s3_key(session_id: str, rel: str) -> str:
    """S3 key: jobs/{session_id}/{rel_path}."""
    return f"jobs/{session_id}/{rel}"


def _s3_watcher() -> None:
    """Background thread: poll workspace root every 2s, upload new/modified files."""
    import time as _time
    from . import config

    while True:
        _time.sleep(2)
        try:
            if not WORKSPACE_ROOT.exists():
                continue
            # Scan all session directories
            for session_dir in WORKSPACE_ROOT.iterdir():
                if not session_dir.is_dir():
                    continue
                session_id = session_dir.name
                # Walk all files in the session
                for file_path in session_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    abs_str = str(file_path)
                    try:
                        mtime = file_path.stat().st_mtime
                    except OSError:
                        continue
                    # Check if new or modified
                    prev_mtime = _s3_file_state.get(abs_str)
                    if prev_mtime is not None and prev_mtime >= mtime:
                        continue
                    # Upload to S3
                    rel = str(file_path.relative_to(session_dir))
                    key = _s3_key(session_id, rel)
                    try:
                        data = file_path.read_bytes()
                        _get_s3_client().put_object(
                            Bucket=config.DOC_BUCKET,
                            Key=key,
                            Body=data,
                        )
                        _s3_file_state[abs_str] = mtime
                        logger.info("[workspace:s3] synced %s (%d bytes)", key, len(data))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[workspace:s3] upload failed %s: %s", key, e)
        except Exception:  # noqa: BLE001
            pass


def start_s3_watcher() -> None:
    """Start the S3 polling watcher thread (call once at server startup)."""
    global _s3_watcher_started
    if _s3_watcher_started or not _s3_enabled():
        return
    _s3_watcher_started = True
    t = threading.Thread(target=_s3_watcher, daemon=True, name="ws-s3-watcher")
    t.start()
    logger.info("[workspace:s3] watcher thread started (polling every 2s)")


def _s3_restore(session_id: str) -> None:
    """Download all files from S3 prefix jobs/{session_id}/ to local workspace.

    Called during init_session to recover workspace state after a runtime restart.
    Skips files that already exist locally (local is authoritative if present).
    """
    if not _s3_enabled():
        return
    from . import config

    prefix = f"jobs/{session_id}/"
    base = WORKSPACE_ROOT / session_id
    try:
        client = _get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        restored = 0
        for page in paginator.paginate(Bucket=config.DOC_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):]
                if not rel:
                    continue
                local_path = base / rel
                if local_path.exists():
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                resp = client.get_object(Bucket=config.DOC_BUCKET, Key=key)
                local_path.write_bytes(resp["Body"].read())
                # Track mtime so watcher doesn't re-upload restored files
                _s3_file_state[str(local_path)] = local_path.stat().st_mtime
                restored += 1
        if restored:
            logger.info("[workspace:s3] restored %d files for session %s", restored, session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[workspace:s3] restore failed for session %s: %s", session_id, e)


# ---------------------------------------------------------------------------
# Session path helpers
# ---------------------------------------------------------------------------
def session_path(session_id: str) -> Path:
    """Return the base path for a session, creating directories as needed."""
    p = WORKSPACE_ROOT / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def init_session(session_id: str) -> Path:
    """Initialize a session workspace with the standard directory structure.

    If S3 has files under jobs/{session_id}/, they are restored to local workspace
    first (handles runtime restart recovery).
    """
    base = session_path(session_id)
    # Restore from S3 before creating subdirs (so we recover prior state)
    _s3_restore(session_id)
    for subdir in ["data", "analysis", "modeling", "solving", "solving/figures",
                   "report", "report/figures"]:
        (base / subdir).mkdir(parents=True, exist_ok=True)
    logger.info("[workspace] init_session %s -> %s", session_id, base)
    return base


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------
def write_text(session_id: str, rel: str, text: str) -> str:
    """Write text to a file in the session workspace. Returns the file path."""
    path = session_path(session_id) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info("[workspace] write_text %s/%s (%d bytes)", session_id, rel, len(text))

    return str(path)


def read_text(session_id: str, rel: str) -> str:
    """Read text from a file in the session workspace. Returns '' if not found."""
    path = session_path(session_id) / rel
    if not path.exists():
        logger.info("[workspace] read_text MISS %s/%s", session_id, rel)
        return ""
    return path.read_text(encoding="utf-8")


def write_json(session_id: str, rel: str, obj) -> str:
    """Write JSON to a file in the session workspace. Returns the file path."""
    body = json.dumps(obj, ensure_ascii=False, indent=2)
    return write_text(session_id, rel, body)


def read_json(session_id: str, rel: str):
    """Read JSON from a file in the session workspace. Returns None if not found."""
    text = read_text(session_id, rel)
    if not text:
        return None
    return json.loads(text)


def write_bytes(session_id: str, rel: str, data: bytes) -> str:
    """Write binary data to a file in the session workspace. Returns the file path."""
    path = session_path(session_id) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    logger.info("[workspace] write_bytes %s/%s (%d bytes)", session_id, rel, len(data))

    return str(path)


def read_bytes(session_id: str, rel: str) -> bytes:
    """Read binary data from a file in the session workspace."""
    path = session_path(session_id) / rel
    if not path.exists():
        return b""
    return path.read_bytes()


def exists(session_id: str, rel: str) -> bool:
    """Check if a file exists in the session workspace."""
    return (session_path(session_id) / rel).exists()


def file_path(session_id: str, rel: str) -> Path:
    """Get the absolute path of a file in the session workspace."""
    return session_path(session_id) / rel


# ---------------------------------------------------------------------------
# Directory listing (for file browser)
# ---------------------------------------------------------------------------
def list_files(session_id: str, subdir: str = "") -> list[dict]:
    """Recursively list all files in a session workspace (or subdirectory).

    Returns a list of {name, rel_path, size, is_dir} dicts.
    """
    base = session_path(session_id)
    root = base / subdir if subdir else base
    if not root.exists():
        return []

    results = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            rel = str(item.relative_to(base))
            results.append({
                "name": item.name,
                "rel_path": rel,
                "size": item.stat().st_size,
                "is_dir": False,
            })
    return results


def list_tree(session_id: str) -> list[dict]:
    """List session workspace as a tree structure for the frontend file browser.

    Returns nested structure: [{name, rel_path, is_dir, children?, size?}]
    """
    base = session_path(session_id)
    if not base.exists():
        return []

    def _build_tree(directory: Path) -> list[dict]:
        items = []
        for entry in sorted(directory.iterdir()):
            rel = str(entry.relative_to(base))
            if entry.is_dir():
                children = _build_tree(entry)
                if children:  # only include non-empty dirs
                    items.append({
                        "name": entry.name,
                        "rel_path": rel,
                        "is_dir": True,
                        "children": children,
                    })
            else:
                items.append({
                    "name": entry.name,
                    "rel_path": rel,
                    "is_dir": False,
                    "size": entry.stat().st_size,
                })
        return items

    return _build_tree(base)


# ---------------------------------------------------------------------------
# Append (for incremental report writing)
# ---------------------------------------------------------------------------
def append_text(session_id: str, rel: str, text: str) -> str:
    """Append text to a file (create if not exists). Returns the file path."""
    path = session_path(session_id) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    logger.info("[workspace] append_text %s/%s (+%d bytes)", session_id, rel, len(text))

    return str(path)
