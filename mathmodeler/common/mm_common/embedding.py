"""mm_common.embedding — Bedrock Nova MME embedding scorer (tech-design §2.3).

Replaces the reference implementation 's ``utils/embedding.py`` (Alibaba-NLP/gte-multilingual-base,
torch/transformers, dim 768) with **Amazon Bedrock Nova Multimodal Embeddings**
(``amazon.nova-2-multimodal-embeddings-v1:0``), which is only available in
``us-east-1`` -> a cross-region ``bedrock-runtime`` client.

The public interface ``score_method(query, methods) -> [{"method_index","score"}]``
(cosine similarity scaled ×100) is preserved verbatim so the upstream
``MethodScorer`` tree-recursion logic needs zero changes. On-the-fly embedding,
no vector DB, no data injection. The Modeler container therefore no longer needs
torch/transformers.

Tests inject a ``FakeEmbeddingScorer`` (deterministic scores) to avoid hitting
real Bedrock; the live body/response field names are pinned in the §10.3
``@pytest.mark.aws`` contract tests.
"""
from __future__ import annotations

import json
from typing import List

import numpy as np

from . import config

# Lazily-created cross-region bedrock-runtime client (us-east-1 for Nova MME).
_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3  # imported lazily so AWS-free unit tests can monkeypatch

        _client = boto3.client("bedrock-runtime", region_name=config.EMBED_REGION)
    return _client


def _embed(text: str) -> np.ndarray:
    """Return the Nova MME embedding vector for a single text string."""
    body = {"text": text}  # TEXT modality only; schema pinned in §10.3 contract test
    resp = _get_client().invoke_model(
        modelId=config.EMBED_MODEL_ID,
        body=json.dumps(body).encode(),
    )
    payload = json.loads(resp["body"].read())
    vec = payload["embedding"]  # field name pinned in §10.3 contract test
    return np.asarray(vec, dtype=np.float32)


def _normalize(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-8)


class EmbeddingScorer:
    """Compute cosine similarity (×100) between a query and a list of methods.

    Mirrors the original ``utils/embedding.py`` public contract:
    ``score_method(query, methods) -> [{"method_index": i, "score": float}]``.
    """

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or config.EMBED_MODEL_ID

    def score_method(self, query: str, methods: List[dict]) -> List[dict]:
        q = _normalize(_embed(query))
        result: List[dict] = []
        for i, method in enumerate(methods, start=1):
            text = f"{method['method']}: {method.get('description', '')}"
            v = _normalize(_embed(text))
            score = float(np.dot(q, v) * 100)
            result.append({"method_index": i, "score": score})
        return result
