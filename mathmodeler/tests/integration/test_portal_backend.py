"""Portal FastAPI server tests (tech-design §8.2, plan C = ALB + Fargate).

Loads ``portal/backend/server.py`` with a stubbed ``mm_common.invoke`` so no AWS
is touched, and asserts:
  * P1 authentication — /api/solve is 401 without/with a wrong token; /api/login
    rejects bad credentials and issues a usable bearer token for the right ones.
  * /api/solve streams the Orchestrator four-stage SSE through unchanged.
  * /healthz returns 200 for the ALB health check.
  * the static frontend (index.html) is served, with SPA fallback.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]  # mathmodeler/
_COMMON = _ROOT / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

ADMIN_USER = "demo-admin"
ADMIN_PASSWORD = "s3cr3t-pass"


def _load_server(monkeypatch):
    """Import the FastAPI app fresh with env + a stubbed invoke.stream_agent."""
    static_dir = _ROOT / "portal" / "frontend" / "out"
    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.setenv("AGENT_CORE_ARN", "arn:aws:bedrock-agentcore:us-west-2:0:runtime/mm-orch")
    monkeypatch.setenv("PORTAL_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("PORTAL_ADMIN_PASSWORD", ADMIN_PASSWORD)

    path = _ROOT / "portal" / "backend" / "server.py"
    spec = importlib.util.spec_from_file_location("portal_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # Stub mm_common.invoke.stream_agent to emit a canned AI SDK v6 stream — the
    # streaming Supervisor orchestrator now speaks the AI SDK wire format directly,
    # and the portal relays those frames verbatim.
    from mm_common import invoke

    def fake_stream(arn, payload, sid):
        assert sid and len(sid) >= 33  # AgentCore runtimeSessionId constraint
        yield json.dumps({"type": "start", "messageId": "m1"})
        yield json.dumps({"type": "data-stage",
                          "data": {"stage": "analysis", "status": "done", "agent": "analyst"}})
        yield json.dumps({"type": "data-agent",
                          "data": {"agent": "modeler", "chunk": {"kind": "token", "delta": "hi"}}})
        yield json.dumps({"type": "data-stage",
                          "data": {"stage": "report", "status": "done", "agent": "reporter"}})
        yield json.dumps({"type": "data-final",
                          "data": {"report_key": "k", "report_url": "https://example/report"}})
        yield json.dumps({"type": "finish"})
        yield "[DONE]"

    monkeypatch.setattr(invoke, "stream_agent", fake_stream)
    return mod



def _client(mod):
    from fastapi.testclient import TestClient

    return TestClient(mod.app)


def _login(client):
    r = client.post("/api/login", json={"user": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    return r.json()["token"]


# --- health ----------------------------------------------------------------
def test_healthz(monkeypatch):
    client = _client(_load_server(monkeypatch))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- P1 authentication -----------------------------------------------------
def test_login_rejects_bad_credentials(monkeypatch):
    client = _client(_load_server(monkeypatch))
    assert client.post("/api/login", json={"user": ADMIN_USER, "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"user": "nobody", "password": ADMIN_PASSWORD}).status_code == 401


def test_solve_requires_auth(monkeypatch):
    client = _client(_load_server(monkeypatch))
    # No token -> 401
    assert client.post("/api/solve", json={"problem": "P"}).status_code == 401
    # Bogus token -> 401
    r = client.post("/api/solve", json={"problem": "P"},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# --- streaming SSE forwarding ----------------------------------------------
def test_solve_streams_sse_with_valid_token(monkeypatch):
    client = _client(_load_server(monkeypatch))
    token = _login(client)
    r = client.post("/api/solve", json={"problem": "P"},
                    headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # /api/solve is a raw passthrough of the orchestrator SSE; the canned stub
    # now emits the AI SDK v6 frames the streaming Supervisor produces.
    assert body.count("data: ") == 7
    assert "https://example/report" in body



# --- /api/chat (AI SDK v6 UI Message Stream) -------------------------------
def test_chat_requires_auth(monkeypatch):
    client = _client(_load_server(monkeypatch))
    assert client.post("/api/chat", json={"messages": []}).status_code == 401
    r = client.post("/api/chat", json={"messages": []},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_chat_emits_ai_sdk_v6_stream(monkeypatch):
    client = _client(_load_server(monkeypatch))
    token = _login(client)
    r = client.post(
        "/api/chat",
        json={"messages": [{"role": "user",
                            "parts": [{"type": "text", "text": "为某城市设计调度方案"}]}]},
        headers={"Authorization": "Bearer " + token},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # AI SDK v6 UI Message Stream marker header.
    assert r.headers.get("x-vercel-ai-ui-message-stream") == "v1"
    body = r.text
    # message envelope + custom data parts + [DONE] terminator
    assert '"type": "start"' in body
    assert '"type": "data-stage"' in body      # four-stage progress
    assert '"type": "data-final"' in body       # final event
    assert '"type": "finish"' in body
    assert "data: [DONE]" in body


# --- static frontend -------------------------------------------------------
def test_static_index_served(monkeypatch):

    client = _client(_load_server(monkeypatch))
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "MathModeler" in r.text


def test_unknown_path_spa_fallback(monkeypatch):
    client = _client(_load_server(monkeypatch))
    r = client.get("/does/not/exist")
    assert r.status_code == 200
    assert "MathModeler" in r.text


# --- session id constraint -------------------------------------------------
def test_new_session_id_length(monkeypatch):
    mod = _load_server(monkeypatch)
    sid = mod._new_session_id()
    assert sid.startswith("mm-")
    assert len(sid) >= 33  # AgentCore runtimeSessionId constraint


# --- stateless signed token (survives restart) ----------------------------
def test_signed_token_roundtrip_and_auth(monkeypatch):
    """The login token is a stateless HMAC-signed token: it validates without
    any server-side state, so it survives a container restart (here simulated by
    clearing the in-memory ``_TOKENS`` set)."""
    mod = _load_server(monkeypatch)
    client = _client(mod)

    token = _login(client)
    assert "." in token  # <payload>.<sig> shape, not a random opaque string
    assert mod._verify_token(token) is True

    # Simulate a redeploy/restart wiping any in-memory store; signed token still ok.
    mod._TOKENS.clear()
    r = client.post("/api/chat", json={"messages": []},
                    headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200

    # Tampered / bogus tokens are rejected.
    assert mod._verify_token("abc.def") is False
    assert mod._verify_token(token + "x") is False


def test_signed_token_expiry(monkeypatch):
    """An expired signed token is rejected."""
    mod = _load_server(monkeypatch)
    monkeypatch.setattr(mod, "TOKEN_TTL_SECONDS", -1, raising=False)
    expired = mod._issue_token("demo-admin")
    assert mod._verify_token(expired) is False

