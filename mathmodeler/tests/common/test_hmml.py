"""Tests for mm_common.hmml (MethodScorer / MethodRetriever) — §10.1."""
from __future__ import annotations

import pytest

from mm_common import hmml


def test_final_score_parent_avg_times_half(sample_hmml_tree):

    """Leaf final_score == parent_avg*0.5 + child_score*0.5 (faithful weighting)."""
    from conftest import FakeEmbeddingScorer

    scores = {
        "Programming:": 80.0,         # method_class child score
        "Statistics:": 40.0,
        "Linear Programming (LP)": 60.0,
        "Mixed Integer Programming (MIP)": 20.0,
        "Linear Regression": 10.0,
    }
    fake = FakeEmbeddingScorer(scores)
    scorer = hmml.MethodScorer(score_func=lambda methods: fake.score_method("q", methods))
    leaves = scorer.process([dict(n) for n in sample_hmml_tree])

    by_name = {leaf["method"]: leaf["score"] for leaf in leaves}
    # LP: parent_avg = 80 (Programming), child = 60 -> 80*0.5 + 60*0.5 = 70
    assert by_name["Linear Programming (LP)"] == pytest.approx(70.0)
    # MIP: 80*0.5 + 20*0.5 = 50
    assert by_name["Mixed Integer Programming (MIP)"] == pytest.approx(50.0)
    # Linear Regression: parent_avg = 40 (Statistics), child = 10 -> 25
    assert by_name["Linear Regression"] == pytest.approx(25.0)


def test_only_leaves_returned(sample_hmml_tree):
    from conftest import FakeEmbeddingScorer

    fake = FakeEmbeddingScorer({})
    scorer = hmml.MethodScorer(score_func=lambda methods: fake.score_method("q", methods))
    leaves = scorer.process([dict(n) for n in sample_hmml_tree])
    names = {leaf["method"] for leaf in leaves}
    assert names == {"Linear Programming (LP)", "Mixed Integer Programming (MIP)", "Linear Regression"}


def test_retriever_top_k_descending(sample_hmml_tree):
    from conftest import FakeEmbeddingScorer

    scores = {
        "Programming:": 90.0,
        "Statistics:": 10.0,
        "Linear Programming (LP)": 90.0,
        "Mixed Integer Programming (MIP)": 50.0,
        "Linear Regression": 5.0,
    }
    fake = FakeEmbeddingScorer(scores)
    retriever = hmml.MethodRetriever(rag=True, embedding_scorer=fake, method_tree=[dict(n) for n in sample_hmml_tree])
    out = retriever.retrieve_methods("some problem", top_k=2, method="embedding")
    lines = out.strip().split("\n")
    assert len(lines) == 2
    # highest score first: LP (90*.5+90*.5=90) then MIP (90*.5+50*.5=70)
    assert "Linear Programming (LP)" in lines[0]
    assert "Mixed Integer Programming (MIP)" in lines[1]


def test_full_hmml_has_97_leaves():
    tree = hmml.load_hmml()
    fake_scores = lambda methods: [{"method_index": i + 1, "score": 1.0} for i in range(len(methods))]
    leaves = hmml.MethodScorer(score_func=fake_scores).process(tree)
    assert len(leaves) == 97


def test_full_hmml_five_top_level_classes():
    tree = hmml.load_hmml()
    assert len(tree) == 5
    assert all("method_class" in node for node in tree)
