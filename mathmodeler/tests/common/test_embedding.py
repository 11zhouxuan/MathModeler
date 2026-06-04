"""Tests for mm_common.embedding (Nova MME scorer) — §10.1."""
from __future__ import annotations

import json

import numpy as np
import pytest

from mm_common import embedding


class _FakeBody:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b


class _FakeClient:
    """Returns deterministic embeddings keyed by text content."""

    def __init__(self):
        self.calls = []
        # map of substring -> vector
        self._vecs = {
            "query": [1.0, 0.0, 0.0],
            "Method A": [1.0, 0.0, 0.0],   # identical to query -> high score
            "Method B": [0.0, 1.0, 0.0],   # orthogonal -> ~0
        }

    def invoke_model(self, modelId, body):
        text = json.loads(body)["text"]
        self.calls.append((modelId, text))
        vec = [0.0, 0.0, 1.0]
        for key, v in self._vecs.items():
            if key in text:
                vec = v
                break
        return {"body": _FakeBody({"embedding": vec})}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(embedding, "_client", client)
    monkeypatch.setattr(embedding, "_get_client", lambda: client)
    return client


def test_score_method_shape_and_length(fake_client):
    scorer = embedding.EmbeddingScorer()
    methods = [
        {"method": "Method A", "description": "desc a"},
        {"method": "Method B", "description": "desc b"},
    ]
    out = scorer.score_method("query about something", methods)
    assert len(out) == 2
    assert [m["method_index"] for m in out] == [1, 2]
    assert all(isinstance(m["score"], float) for m in out)


def test_score_method_cosine_values(fake_client):
    scorer = embedding.EmbeddingScorer()
    methods = [
        {"method": "Method A", "description": ""},  # cos==1 -> 100
        {"method": "Method B", "description": ""},  # cos==0 -> 0
    ]
    out = scorer.score_method("query", methods)
    assert out[0]["score"] == pytest.approx(100.0, abs=1e-3)
    assert out[1]["score"] == pytest.approx(0.0, abs=1e-3)


def test_score_method_single_method(fake_client):
    scorer = embedding.EmbeddingScorer()
    out = scorer.score_method("query", [{"method": "Method A"}])
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["method_index"] == 1


def test_normalize_unit_norm():
    v = np.array([3.0, 4.0], dtype=np.float32)
    n = embedding._normalize(v)
    assert np.linalg.norm(n) == pytest.approx(1.0, abs=1e-5)
