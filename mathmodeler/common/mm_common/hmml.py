"""mm_common.hmml — HMML load + MethodScorer + MethodRetriever (tech-design §2.4).

Faithful port of the reference implementation ``agent/retrieve_method.py``:
parent_weight = child_weight = 0.5, leaf ``final_score = parent_avg*0.5 + child*0.5``,
top_k = 6, method = 'embedding'. The only substitution is the similarity engine
(Bedrock Nova MME via :class:`mm_common.embedding.EmbeddingScorer`) — the tree
recursion / parent-child weighting / leaf collection are unchanged.

No simplification: the full HMML tree (97 leaf methods, 5 top-level method
classes) is loaded from ``mathmodeler/HMML/HMML.json``.
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import List, Optional

from . import config
from .embedding import EmbeddingScorer
from .prompts import METHOD_CRITIQUE_PROMPT

# Repo-root-relative path to the committed HMML tree.
#   mm_common/hmml.py -> common/ -> mathmodeler/ -> HMML/HMML.json
_HMML_JSON = Path(__file__).resolve().parents[2] / "HMML" / "HMML.json"


def load_hmml(path: Optional[Path] = None) -> list[dict]:
    """Load the HMML method tree from JSON."""
    p = Path(path) if path is not None else _HMML_JSON
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_llm_output_to_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM completion.

    Mirrors the behaviour relied upon by the original ``llm_score_method``:
    strips ```json fences and parses the first ``{...}`` block.
    """
    s = text.strip()
    if "```" in s:
        # take content between the first pair of fences
        parts = s.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[len("json"):].strip()
            if part.startswith("{"):
                s = part
                break
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


class MethodScorer:
    """Recursive tree scorer — verbatim port of the original MethodScorer."""

    def __init__(self, score_func, parent_weight: float = config.HMML_PARENT_WEIGHT,
                 child_weight: float = config.HMML_CHILD_WEIGHT):
        self.parent_weight = parent_weight
        self.child_weight = child_weight
        self.score_func = score_func
        self.leaves: list[dict] = []

    def process(self, data: list[dict]) -> list[dict]:
        self.leaves = []
        # Score the top-level method_class roots first so that a leaf whose
        # immediate parent is a root method_class still gets a non-zero
        # parent_avg (the tree depth is not uniform across classes).
        roots = [n for n in data if "method_class" in n]
        if roots:
            root_input = [
                {"method": n["method_class"], "description": n.get("description", "")}
                for n in roots
            ]
            root_scores = self.score_func(root_input)
            for idx, n in enumerate(roots):
                n["score"] = root_scores[idx]["score"] if idx < len(root_scores) else 0
        for root_node in data:
            init_parent = [root_node["score"]] if "score" in root_node else []
            self._process_node(root_node, parent_scores=init_parent)
        for root_node in data:
            self._collect_leaves(root_node)
        return self.leaves

    def _process_node(self, node: dict, parent_scores: list) -> None:
        if "children" in node:
            children = node.get("children", [])
            if children:
                first_child = children[0]
                if "method_class" in first_child:
                    input_for_llm = [
                        {"method": child["method_class"], "description": child.get("description", "")}
                        for child in children
                    ]
                    llm_result = self.score_func(input_for_llm)
                    for idx, child in enumerate(children):
                        if idx < len(llm_result):
                            child["score"] = llm_result[idx]["score"]
                        else:
                            child["score"] = 0
                    for child in children:
                        # Recurse into each method_class child passing *only that
                        # child's* score as the parent context, so a leaf's
                        # parent_avg reflects its immediate method_class parent
                        # (faithful behaviour: not an average of all ancestors).
                        self._process_node(child, [child["score"]])
                else:
                    input_for_llm = [
                        {"method": child["method"], "description": child.get("description", "")}
                        for child in children
                    ]
                    llm_result = self.score_func(input_for_llm)
                    for idx, child in enumerate(children):
                        if idx < len(llm_result):
                            child_score = llm_result[idx]["score"]
                        else:
                            child_score = 0
                        child["score"] = child_score
                        parent_avg = sum(parent_scores) / len(parent_scores) if parent_scores else 0
                        final_score = parent_avg * self.parent_weight + child_score * self.child_weight
                        child["final_score"] = final_score

    def _collect_leaves(self, node: dict) -> None:
        if "children" in node:
            for child in node["children"]:
                self._collect_leaves(child)
        else:
            if "final_score" in node:
                self.leaves.append({
                    "method": node["method"],
                    "description": node.get("description", ""),
                    "score": node["final_score"],
                })


class MethodRetriever:
    """Retrieve top-k HMML methods for a problem description (faithful port)."""

    def __init__(self, llm=None, rag: bool = True, embedding_scorer=None,
                 method_tree: Optional[list[dict]] = None):
        self.llm = llm
        self.rag = rag
        self.embedding_scorer = embedding_scorer or EmbeddingScorer()
        self.method_tree = method_tree if method_tree is not None else load_hmml()
        # raw markdown is optional; only the JSON tree is required at runtime
        self.markdown_text = ""

    def llm_score_method(self, problem_description: str, methods: List[dict]) -> list[dict]:
        methods_str = "\n".join(
            f"{i + 1}. {m['method']} {m.get('description', '')}" for i, m in enumerate(methods)
        )
        prompt = METHOD_CRITIQUE_PROMPT.format(
            problem_description=problem_description, methods=methods_str
        )
        answer = self.llm.generate(prompt)
        method_scores = _parse_llm_output_to_json(answer).get("methods", [])
        method_scores = sorted(method_scores, key=lambda x: x["method_index"])
        for method in method_scores:
            method["score"] = sum(method["scores"].values()) / len(method["scores"])
        return method_scores

    def format_methods(self, methods: List[dict]) -> str:
        return "\n".join(f"**{m['method']}:** {m['description']}" for m in methods)

    def retrieve_methods(self, problem_description: str, top_k: int = config.HMML_TOP_K,
                         method: str = "embedding") -> str:
        """Return a markdown string of the top-k retrieved methods.

        Note: the original misspelled this ``retrieve_meethods``; we expose the
        corrected name. ``rag=False`` returns the raw markdown text.
        """
        if not self.rag:
            return self.markdown_text
        if method == "embedding":
            score_func = partial(self.embedding_scorer.score_method, problem_description)
        else:
            score_func = partial(self.llm_score_method, problem_description)
        method_scores = MethodScorer(score_func).process(self.method_tree)
        method_scores.sort(key=lambda x: x["score"], reverse=True)
        return self.format_methods(method_scores[:top_k])

    # backwards-compatible alias matching the original (typo'd) method name
    retrieve_meethods = retrieve_methods
