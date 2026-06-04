"""Shared pytest fixtures for MathModeler (tech-design §10.0).

Adds ``common/`` to sys.path so ``import mm_common`` works without installing,
and provides reusable fakes / sample HMML subtree fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]          # mathmodeler/
_COMMON = _ROOT / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))


@pytest.fixture
def sample_hmml_tree() -> list[dict]:
    """A tiny 2-class HMML subtree with known structure (for hmml/embedding tests)."""
    return [
        {
            "method_class": "Operations Research:",
            "children": [
                {
                    "method_class": "Programming:",
                    "children": [
                        {"method": "Linear Programming (LP)", "description": "lp desc"},
                        {"method": "Mixed Integer Programming (MIP)", "description": "mip desc"},
                    ],
                    "description": "programming desc",
                }
            ],
        },
        {
            "method_class": "Statistics:",
            "children": [
                {"method": "Linear Regression", "description": "reg desc"},
            ],
        },
    ]


class FakeEmbeddingScorer:
    """Returns deterministic, query-independent scores keyed by method name."""

    def __init__(self, scores: dict[str, float] | None = None):
        self.scores = scores or {}
        self.calls: list[tuple[str, int]] = []

    def score_method(self, query: str, methods: list[dict]) -> list[dict]:
        self.calls.append((query, len(methods)))
        out = []
        for i, m in enumerate(methods, start=1):
            name = m.get("method") or m.get("method_class", "")
            out.append({"method_index": i, "score": float(self.scores.get(name, 1.0))})
        return out
