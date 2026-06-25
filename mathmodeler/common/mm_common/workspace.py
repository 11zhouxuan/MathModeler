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
from pathlib import Path

logger = logging.getLogger("mm.workspace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORKSPACE_ROOT: Path = Path(os.getenv("MM_WORKSPACE_ROOT", "./jobs")).resolve()


def _ensure_root() -> None:
    """Create workspace root if it doesn't exist."""
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def start_s3_watcher() -> None:
    """No-op: S3 Files mount handles sync automatically."""
    pass


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

    S3 Files mount handles persistence — files survive session stop/resume
    and are visible in the backing S3 bucket automatically.
    """
    base = session_path(session_id)
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
