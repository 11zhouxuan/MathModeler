"""Tests for mm_common.memory (AgentCore Memory wrapper) — §10.1, mocked client."""
from __future__ import annotations

import pytest

from mm_common import config, memory


class _FakeMemClient:
    def __init__(self):
        self.events = []
        self.retrieve_calls = []

    def create_event(self, memoryId, actorId, sessionId, payload):
        self.events.append({"memoryId": memoryId, "actorId": actorId,
                            "sessionId": sessionId, "payload": payload})

    def list_events(self, memoryId, actorId, sessionId, maxResults):
        return {"events": [e for e in self.events if e["sessionId"] == sessionId]}

    def retrieve_memory_records(self, memoryId, namespace, searchCriteria):
        self.retrieve_calls.append((namespace, searchCriteria))
        return {"memoryRecordSummaries": [
            {"content": {"text": "prefers concise reports"}},
            {"content": {"text": "likes Python"}},
        ]}


@pytest.fixture
def fake_mem(monkeypatch):
    monkeypatch.setenv("MEMORY_ID", "mem-123")
    config.reload()
    client = _FakeMemClient()
    monkeypatch.setattr(memory, "_client", client)
    monkeypatch.setattr(memory, "_get_client", lambda: client)
    return client


def test_save_event(fake_mem):
    memory.save_event("sess-1", "orchestrator", "assistant", "stage 1 done")
    assert len(fake_mem.events) == 1
    ev = fake_mem.events[0]
    assert ev["memoryId"] == "mem-123"
    assert ev["payload"] == [{"role": "assistant", "content": "stage 1 done"}]


def test_list_events(fake_mem):
    memory.save_event("sess-1", "a", "assistant", "x")
    memory.save_event("sess-2", "a", "assistant", "y")
    out = memory.list_events("sess-1", "a")
    assert len(out) == 1


def test_retrieve_preferences(fake_mem):
    out = memory.retrieve("user-1", "report style", namespace="preferences", top_k=5)
    assert out == ["prefers concise reports", "likes Python"]
    assert fake_mem.retrieve_calls[0][0] == "preferences"
    assert fake_mem.retrieve_calls[0][1]["topK"] == 5
