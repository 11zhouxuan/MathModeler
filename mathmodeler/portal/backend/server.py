"""Portal FastAPI server (ALB + Fargate; tech-design §8.2, plan C).

Runs as a long-lived ``uvicorn`` process behind an Application Load Balancer
(HTTP listener). The ALB streams chunked/SSE responses **without buffering**, so
the four-stage Orchestrator progress reaches the browser in real time:

    ALB (chunked) -> Fargate FastAPI (StreamingResponse)
        -> boto3 InvokeAgentRuntime(accept=text/event-stream)
        -> Orchestrator Runtime SSE

Routes
------
* ``GET  /healthz``        — ALB health check (no auth).
* ``POST /api/login``      — P1 password gate; returns an opaque session token.
* ``POST /api/solve``      — authenticated; streams the Orchestrator SSE live.
* ``GET  /``, static files — serves the bundled frontend.

Authentication (P1 = authentication, not authorization)
-------------------------------------------------------
A single admin credential is provided at deploy time via the
``PORTAL_ADMIN_USER`` / ``PORTAL_ADMIN_PASSWORD`` environment variables. The
browser logs in once (``/api/login``) and receives a bearer token that it sends
on ``/api/solve``. There is no multi-user / role model — only a password door.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import time
import uuid
from pathlib import Path

logger = logging.getLogger("mm.portal")



from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


AGENT_ARN = os.environ.get("AGENT_CORE_ARN", "")


def _resolve_static_dir() -> Path:
    """Locate the frontend bundle.

    In the container it lives at ``./static`` (set via ``STATIC_DIR``); when
    running locally with ``uv``/``uvicorn`` straight from the source tree it
    lives at ``../frontend/out``. Fall back to the latter so local dev needs no
    environment variables.
    """
    if os.environ.get("STATIC_DIR"):
        return Path(os.environ["STATIC_DIR"])
    here = Path(__file__).resolve().parent
    for cand in (here / "static", here.parent / "frontend" / "out"):
        if cand.exists():
            return cand
    return here / "static"


STATIC_DIR = _resolve_static_dir()
ADMIN_USER = os.environ.get("PORTAL_ADMIN_USER", "admin")
# Local-dev convenience: if no password is configured, default to "admin" so the
# portal is runnable out-of-the-box. In the deployed stack the password is
# always injected (CDK requires it), so this default never applies in prod.
ADMIN_PASSWORD = os.environ.get("PORTAL_ADMIN_PASSWORD", "admin")


# Stateless signed-token settings. The token is an HMAC-signed
# ``<base64url(payload)>.<base64url(sig)>`` string carrying ``{user, exp}`` so it
# can be validated WITHOUT any server-side session store — logins therefore
# survive container restarts / redeploys and work across multiple tasks. The
# signing secret defaults to a key derived from the admin password (always set
# in prod); override with ``PORTAL_TOKEN_SECRET`` for a dedicated secret.
TOKEN_SECRET = (
    os.environ.get("PORTAL_TOKEN_SECRET")
    or ("mm-portal::" + ADMIN_PASSWORD)
).encode()
TOKEN_TTL_SECONDS = int(os.environ.get("PORTAL_TOKEN_TTL", "86400"))  # 24h default

# Legacy in-memory token store kept as a fallback (e.g. tokens minted by an
# older build). Stateless signed tokens are the primary mechanism.
_TOKENS: set[str] = set()

app = FastAPI(title="MathModeler Portal", docs_url=None, redoc_url=None)

# CORS: allow the frontend dev server (port 3000) to call the portal directly,
# bypassing the Next.js proxy (which doesn't stream SSE properly in Turbopack).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev convenience; in prod, frontend is same-origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-vercel-ai-ui-message-stream"],
)

_bearer = HTTPBearer(auto_error=False)


# --- helpers ---------------------------------------------------------------
def _new_session_id() -> str:
    """runtimeSessionId must be >= 33 chars (AgentCore constraint)."""
    return "mm-" + uuid.uuid4().hex  # 35 chars


def _check_password(password: str) -> bool:
    """Constant-time password comparison; empty configured password denies all."""
    if not ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(password, ADMIN_PASSWORD)


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _issue_token(user: str) -> str:
    """Mint a stateless HMAC-signed token ``<payload>.<sig>`` carrying user+exp.

    No server-side state — validated purely by signature + expiry, so it
    survives container restarts and works across multiple tasks.
    """
    payload = {"u": user, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_token(token: str) -> bool:
    """Validate a stateless signed token (signature + not expired)."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return False
    expected = _b64u(hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(_b64u_decode(body))
    except Exception:  # noqa: BLE001
        return False
    return int(payload.get("exp", 0)) > int(time.time())


def _require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Reject requests without a valid bearer token.

    Accepts a stateless signed token (primary) or, for backward compatibility, a
    token still present in the legacy in-memory ``_TOKENS`` set.
    """
    token = creds.credentials if creds else None
    if not token or (not _verify_token(token) and token not in _TOKENS):
        raise HTTPException(status_code=401, detail="unauthorized")



def _iter_orchestrator_sse(body: dict):
    """Yield ``data: ...\\n\\n`` lines from the Orchestrator Runtime (live)."""
    from mm_common import invoke  # common is installed in the image

    sid = body.get("session_id") or _new_session_id()
    payload = {**body, "session_id": sid}
    for ev in invoke.stream_agent(AGENT_ARN, payload, sid):
        yield f"data: {ev}\n\n"


# --- AI SDK v6 UI Message Stream adapter -----------------------------------
# The portal translates the Orchestrator's internal four-stage SSE into the
# Vercel AI SDK v6 "UI Message Stream" wire format so the frontend can consume
# it with `useChat` + `DefaultChatTransport`. Each frame is ``data: <json>\n\n``;
# the stream is terminated with ``data: [DONE]\n\n`` and the response carries the
# ``x-vercel-ai-ui-message-stream: v1`` header.
#
# Mapping of internal events -> v6 parts:
#   stage / subagent  -> ``data-stage`` (custom data part; drives the timeline)
#   final.report_url  -> ``data-final`` + the report text streamed as text-delta
#   error             -> ``data-error`` + an ``error`` chunk
def _sdk_frame(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _extract_user_problem(messages: list) -> str:
    """Pull the latest user message's text from an AI SDK messages array."""
    for msg in reversed(messages or []):
        if (msg or {}).get("role") != "user":
            continue
        parts = msg.get("parts")
        if isinstance(parts, list):
            txt = "".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
            if txt:
                return txt
        if isinstance(msg.get("content"), str) and msg["content"].strip():
            return msg["content"].strip()
    return ""


def _read_report(report_url: str) -> str:
    """Best-effort fetch of the final report markdown from its presigned URL."""
    if not report_url:
        return ""
    try:
        import urllib.request

        with urllib.request.urlopen(report_url, timeout=15) as r:  # noqa: S310
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_interrupt_responses(body: dict) -> list | None:
    """Pull an ``interruptResponses`` list from the chat body (HITL resume).

    The frontend resumes a paused ``ask_user`` by re-POSTing ``/api/chat`` with
    the same ``session_id`` plus an ``interruptResponses`` array (or a single
    ``{interruptId, response}`` pair). Returns None when this is a fresh turn.
    """
    irs = body.get("interruptResponses")
    if isinstance(irs, list) and irs:
        return irs
    # Convenience single-pair form.
    iid = body.get("interruptId")
    resp = body.get("answer") if "answer" in body else body.get("response")
    if iid and resp is not None:
        return [{"interruptResponse": {"interruptId": iid, "response": resp}}]
    # AI SDK may carry the answer as the latest user message text on resume.
    return None


def _iter_chat_stream(body: dict):
    """Relay the Orchestrator's AI SDK v6 UI Message Stream to the browser.

    The Orchestrator (streaming Supervisor) now emits AI SDK v6 frames directly
    (start / text-* / tool-* / data-stage / data-agent / data-ask / data-final /
    finish / [DONE]). The portal therefore *passes them through* verbatim, only:
      * choosing/propagating the ``session_id`` (so a paused ``ask_user`` can be
        resumed by a later request targeting the SAME session), and
      * forwarding ``interruptResponses`` for the resume turn, and
      * emitting a leading ``data-session`` part so the frontend learns the sid.
    """
    from mm_common import invoke

    messages = body.get("messages") or []
    problem = _extract_user_problem(messages) or body.get("problem", "")
    sid = body.get("session_id") or _new_session_id()
    actor_id = body.get("actor_id", "anonymous")
    interrupt_responses = _extract_interrupt_responses(body)

    # Diagnostic: surface how the incoming chat body is shaped so an empty
    # ``problem`` (the "problem appears empty" symptom) can be traced to the
    # frontend message format vs. the portal's extraction.
    try:
        last_user = next(
            (m for m in reversed(messages) if (m or {}).get("role") == "user"), {}
        )
        part_types = [
            p.get("type") for p in (last_user.get("parts") or [])
            if isinstance(p, dict)
        ]
        logger.info(
            "[portal] /api/chat body_keys=%s n_messages=%d last_user_part_types=%s "
            "extracted_problem_len=%d session=%s",
            sorted(body.keys()), len(messages), part_types, len(problem or ""), sid,
        )
    except Exception:  # noqa: BLE001 - logging must never break the stream
        pass


    # Let the frontend capture the session id for any later resume turn.
    yield _sdk_frame({"type": "data-session", "id": sid, "data": {"session_id": sid}})

    payload: dict = {"problem": problem, "session_id": sid, "actor_id": actor_id}
    if interrupt_responses is not None:
        payload["interruptResponses"] = interrupt_responses

    try:
        for raw in invoke.stream_agent(AGENT_ARN, payload, sid):
            raw = raw.strip()
            if not raw:
                continue
            # The orchestrator already speaks AI SDK v6 wire frames; relay as-is.
            # Suppress the orchestrator's own [DONE] so we can append our own.
            if raw == "[DONE]":
                continue
            yield f"data: {raw}\n\n"
    except Exception as e:  # noqa: BLE001
        yield _sdk_frame({"type": "error", "errorText": str(e)})
    finally:
        yield "data: [DONE]\n\n"




# --- health ----------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# --- auth (P1) -------------------------------------------------------------
@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    user = (body or {}).get("user", "")
    password = (body or {}).get("password", "")
    if user != ADMIN_USER or not _check_password(password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Stateless signed token: survives container restarts / redeploys; no
    # server-side session store needed.
    return JSONResponse({"token": _issue_token(user)})



# --- solve (authenticated, streaming) -------------------------------------
@app.post("/api/solve")
async def solve(request: Request, _: None = Depends(_require_auth)) -> StreamingResponse:
    body = await request.json()
    return StreamingResponse(
        _iter_orchestrator_sse(body or {}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- chat (authenticated, AI SDK v6 UI Message Stream) --------------------
@app.post("/api/chat")
async def chat(request: Request, _: None = Depends(_require_auth)) -> StreamingResponse:
    body = await request.json()
    return StreamingResponse(
        _iter_chat_stream(body or {}),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


# --- cancel (forward to orchestrator agent.stop) ----------------------------
@app.post("/api/cancel")
async def cancel(request: Request, _: None = Depends(_require_auth)) -> JSONResponse:
    body = await request.json()
    session_id = (body or {}).get("session_id", "")
    # Forward to the orchestrator's /cancel endpoint (local: port 8080).
    import urllib.request
    orch_url = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8080")
    try:
        req = urllib.request.Request(
            f"{orch_url}/cancel",
            data=json.dumps({"session_id": session_id}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            result = json.loads(r.read())
    except Exception:  # noqa: BLE001
        result = {"cancelled": False, "session_id": session_id, "error": "unreachable"}
    return JSONResponse(result)


# --- chat history persistence (DynamoDB, cross-browser) --------------------

@app.get("/api/sessions")
async def list_sessions(_: None = Depends(_require_auth)) -> JSONResponse:
    """List all chat sessions (most recent first) from DynamoDB."""
    from mm_common import chat_store
    sessions = chat_store.list_sessions()
    return JSONResponse({"sessions": sessions})


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, _: None = Depends(_require_auth)) -> JSONResponse:
    """Return the saved UI messages for a session from DynamoDB."""
    from mm_common import chat_store
    messages = chat_store.load_messages(session_id)
    return JSONResponse({"messages": messages})


@app.post("/api/sessions/{session_id}/messages")
async def save_session_messages(session_id: str, request: Request, _: None = Depends(_require_auth)) -> JSONResponse:
    """Save UI messages to DynamoDB (called by frontend when stream completes)."""
    from mm_common import chat_store
    body = await request.json()
    messages = body.get("messages", [])
    problem = body.get("problem", "")
    try:
        chat_store.save_session(session_id, messages, problem=problem)
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# --- workspace file browser API (session files) ----------------------------
# These routes expose the session workspace files for the frontend file browser
# panel. In local dev, workspace is ./jobs/{session_id}/; in Runtime it's
# /mnt/workspace/jobs/{session_id}/.

@app.get("/api/files/{session_id}")
async def list_session_files(session_id: str, _: None = Depends(_require_auth)) -> JSONResponse:
    """List all files in a session workspace as a tree structure."""
    from mm_common import workspace
    tree = workspace.list_tree(session_id)
    return JSONResponse({"session_id": session_id, "tree": tree})


@app.get("/api/files/{session_id}/{file_path:path}")
async def download_session_file(session_id: str, file_path: str, _: None = Depends(_require_auth)):
    """Download a specific file from the session workspace."""
    from mm_common import workspace

    full_path = workspace.file_path(session_id, file_path)
    # Security: ensure the resolved path is within the session workspace
    session_root = workspace.session_path(session_id)
    try:
        full_path.resolve().relative_to(session_root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal denied")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {file_path}")

    # Determine content type
    ctype = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
    return FileResponse(
        str(full_path),
        media_type=ctype,
        filename=full_path.name,
        headers={"Content-Disposition": f'attachment; filename="{full_path.name}"'},
    )


# --- static frontend -------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return _serve_static("index.html")




@app.get("/{path:path}")
def static_files(path: str) -> FileResponse:
    return _serve_static(path)


def _serve_static(rel: str) -> FileResponse:
    rel = rel.lstrip("/") or "index.html"
    target = (STATIC_DIR / rel).resolve()
    # Guard against path traversal outside STATIC_DIR.
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        target = STATIC_DIR / "index.html"
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        target = STATIC_DIR / "index.html"  # SPA fallback
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=ctype)
