"""Local full-stack portal launcher (dev/testing only — production code reused verbatim).

This runs the **exact production portal** (``portal/backend/server.py``) so local
behaviour matches the deployed Fargate portal 1:1. The ONLY difference is the
transport that reaches the Orchestrator:

  * Production: ``mm_common.invoke.stream_agent`` -> boto3 ``InvokeAgentRuntime``
    -> AgentCore Orchestrator Runtime.
  * Local:      this script monkeypatches ``stream_agent`` to HTTP-POST to a
    locally-running Orchestrator (``app:app`` on ``MM_LOCAL_ORCHESTRATOR_URL``,
    default ``http://127.0.0.1:8080``), which itself is the production
    ``agents/orchestrator/app.py`` run under uvicorn.

Nothing in the production source tree is modified — the swap lives only here.
The portal's login, token signing, ``/api/chat`` SSE pass-through and AI SDK v6
headers are all the real production code paths.

Run (from mathmodeler/):
  PYTHONPATH=common:portal/backend:agents/orchestrator \
  MM_LOCAL_ORCHESTRATOR_URL=http://127.0.0.1:8080 \
  PORTAL_ADMIN_USER=admin PORTAL_ADMIN_PASSWORD=demo123 \
  uv run --with fastapi --with "uvicorn[standard]" --with boto3 \
    python -m uvicorn scripts.local_portal:app --host 127.0.0.1 --port 8090
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request


# Ensure the ``mm.*`` loggers (mm.portal etc.) emit at INFO to stdout — uvicorn
# installs its own root handlers, so attach a dedicated handler to the ``mm``
# parent logger (same approach as agents/orchestrator/app.py).
def _configure_mm_logging() -> None:
    mm_root = logging.getLogger("mm")
    mm_root.setLevel(logging.INFO)
    if not any(getattr(h, "_mm_handler", False) for h in mm_root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler._mm_handler = True  # type: ignore[attr-defined]
        mm_root.addHandler(handler)
    mm_root.propagate = False


_configure_mm_logging()

# --- 1. Monkeypatch the transport BEFORE importing the production portal app ---
from mm_common import invoke


_ORCH_URL = os.environ.get("MM_LOCAL_ORCHESTRATOR_URL", "http://127.0.0.1:8080")


def _local_stream_agent(agent_arn, payload, session_id):
    """Drop-in replacement for invoke.stream_agent: HTTP -> local orchestrator.

    Yields the same thing the production helper yields: each decoded SSE
    ``data:`` payload (one JSON string per line), so the portal's pass-through
    logic is exercised unchanged.
    """
    url = _ORCH_URL.rstrip("/") + "/invocations"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data: "):
                yield line[6:]


invoke.stream_agent = _local_stream_agent  # type: ignore[assignment]

# --- 2. Import the REAL production portal app (uses the patched stream_agent) ---
from server import app  # noqa: E402  (portal/backend/server.py)

__all__ = ["app"]
