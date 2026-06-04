"""Tests for mm_common.invoke (InvokeAgentRuntime + SSE parsing) — §10.1."""
from __future__ import annotations

import json

import pytest

from mm_common import invoke


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


class _FakeRuntimeClient:
    def __init__(self, lines):
        self._lines = lines
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": _FakeStream(self._lines)}


@pytest.fixture
def patch_client(monkeypatch):
    def _make(lines):
        client = _FakeRuntimeClient(lines)
        monkeypatch.setattr(invoke, "_client", client)
        monkeypatch.setattr(invoke, "_get_client", lambda: client)
        return client
    return _make


def test_invoke_agent_returns_last_json(patch_client):
    lines = [
        b'data: {"type": "stage", "name": "analysis"}',
        b"",
        b'data: {"type": "final", "ok": true, "order": ["1", "2"]}',
    ]
    client = patch_client(lines)
    out = invoke.invoke_agent("arn:analyst", {"problem": "p"}, "mm-" + "x" * 32)
    assert out == {"type": "final", "ok": True, "order": ["1", "2"]}
    # verify request shape
    kw = client.calls[0]
    assert kw["agentRuntimeArn"] == "arn:analyst"
    assert kw["accept"] == "text/event-stream"
    assert json.loads(kw["payload"]) == {"problem": "p"}


def test_invoke_agent_empty_stream(patch_client):
    patch_client([])
    assert invoke.invoke_agent("arn", {}, "mm-" + "x" * 32) == {}


def test_stream_agent_yields_payloads(patch_client):
    lines = [b'data: a', b'', b'data: b']
    patch_client(lines)
    out = list(invoke.stream_agent("arn", {}, "mm-" + "x" * 32))
    assert out == ["a", "b"]


def test_stream_agent_handles_str_lines(patch_client):
    lines = ["data: x", "", "data: y"]
    patch_client(lines)
    out = list(invoke.stream_agent("arn", {}, "mm-" + "x" * 32))
    assert out == ["x", "y"]
