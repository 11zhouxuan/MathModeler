"""§10.1 server.py — make_app routes: /ping, /invocations (JSON vs SSE branch)."""
from fastapi.testclient import TestClient

from mm_common.server import make_app


def _handler(body: dict) -> dict:
    return {"ok": True, "echo": body.get("x")}


def _stream_handler(body: dict):
    yield "data: {\"type\": \"stage\", \"stage\": \"analysis\", \"status\": \"start\"}\n\n"
    yield "data: {\"type\": \"final\", \"ok\": true}\n\n"


def test_ping():
    client = TestClient(make_app(_handler))
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_invocations_json_branch():
    client = TestClient(make_app(_handler))
    r = client.post("/invocations", json={"x": 42})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "echo": 42}


def test_invocations_sse_branch():
    client = TestClient(make_app(_handler, stream_handler=_stream_handler))
    r = client.post(
        "/invocations",
        json={"x": 1},
        headers={"accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "\"type\": \"final\"" in r.text


def test_invocations_sse_without_handler_falls_back_to_json():
    # no stream_handler -> even with SSE accept, must use JSON handler
    client = TestClient(make_app(_handler))
    r = client.post(
        "/invocations",
        json={"x": 7},
        headers={"accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "echo": 7}
