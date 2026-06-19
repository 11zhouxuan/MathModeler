"""Sub-agent runner tests (tech-design §10.2; agents-as-tools §8).

After the 5→1 agents-as-tools refactor the four sub-agents are no longer
separate Runtimes — their logic lives in ``mm_common.runners.run_*`` and is
called in-process by the Orchestrator's ``invoke_*`` tools. These tests exercise
those runners directly: replace ``runners.llm.build_agent`` with a fake whose
``__call__`` writes the expected workspace artifacts (simulating the LLM's tool use),
then assert each runner returns the correct workspace-derived §3 response.
"""
from __future__ import annotations

from mm_common import runners, workspace


class FakeAgent:
    def __init__(self, on_call):
        self._on_call = on_call

    def __call__(self, task: str):
        self._on_call(task)
        return "ok"


def _patch_build_agent(monkeypatch, writer):
    """Make runners.llm.build_agent return a FakeAgent that runs ``writer``."""
    monkeypatch.setattr(runners.llm, "build_agent", lambda *a, **k: FakeAgent(writer))


def test_run_analyst_reconstructs_order_from_s3(doc_bucket, monkeypatch):
    sid = "mm-sess-analyst-000000000000000000"

    def writer(_task):
        workspace.write_text(sid, "analysis/problem_analysis.md", "# analysis")
        workspace.write_json(sid, "analysis/task_descriptions.json",
                       [{"id": "1", "title": "t1", "description": "d1"},
                        {"id": "2", "title": "t2", "description": "d2"}])
        workspace.write_json(sid, "analysis/dag.json", {"1": [], "2": ["1"]})

    _patch_build_agent(monkeypatch, writer)
    monkeypatch.setattr(runners, "analyst_tools", lambda sid_: [])

    resp = runners.run_analyst({"session_id": sid, "problem": "P"})
    assert resp["ok"] is True
    assert resp["order"] == ["1", "2"]
    assert resp["tasknum"] == 2


def test_run_modeler_reconstructs_modeling_from_s3(doc_bucket, monkeypatch):
    sid = "mm-sess-modeler-000000000000000000"

    def writer(_task):
        # Simulate the actor->critic loop: 2 rounds recorded alongside the result.
        workspace.write_json(sid, "modeling/1.json", {
            "task_analysis": "a", "task_modeling_formulas": "x=1",
            "task_modeling_method": "MIP", "retrieved_methods": ["MIP", "LP"],
            "critic_rounds": [
                {"round": 1, "critique": "assumptions too strong"},
                {"round": 2, "critique": "ok"},
            ]})

    _patch_build_agent(monkeypatch, writer)
    monkeypatch.setattr(runners, "modeler_tools", lambda sid_, r: [])
    monkeypatch.setattr(runners, "get_retriever", lambda: None)

    resp = runners.run_modeler({"session_id": sid, "task_id": "1", "problem": "P",
                                "task_description": "d"})
    assert resp["ok"] is True
    assert resp["task_modeling_method"] == "MIP"
    assert resp["retrieved_methods"] == ["MIP", "LP"]
    assert resp["modeling_key"].endswith("modeling/1.json")
    # actor-critic trace round-trips through S3
    assert len(resp["critic_rounds"]) == 2
    assert resp["critic_rounds"][0]["round"] == 1


def test_modeler_tools_expose_critique_self_evaluation():
    """The Modeler must expose a `critique_modeling` self-evaluation tool so the
    agent can run an actor->critic refinement loop (soft-constrained via prompt)."""
    from mm_common.tools import modeler_tools

    tools = modeler_tools("mm-sess-modeler-000000000000000000", retriever=None)
    names = {getattr(t, "__name__", getattr(t, "tool_name", "")) for t in tools}
    assert "critique_modeling" in names
    assert "retrieve_hmml_methods" in names


def test_run_reporter_reconstructs_report_from_s3(doc_bucket, monkeypatch):
    sid = "mm-sess-reporter-00000000000000000"

    def writer(_task):
        # The reporter now writes a PDF; simulate by writing the pdf file
        workspace.write_text(sid, "report/report.tex", "\\documentclass{article}")
        import pathlib
        pdf_path = pathlib.Path(workspace.session_path(sid)) / "report" / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

    _patch_build_agent(monkeypatch, writer)
    monkeypatch.setattr(runners, "reporter_tools", lambda sid_: [])

    resp = runners.run_reporter({"session_id": sid, "problem": "P", "order": ["1"]})
    assert resp["ok"] is True
    assert resp["pdf_exists"] is True


def test_run_solver_reconstructs_result_from_s3(doc_bucket, monkeypatch):
    sid = "mm-sess-solver-0000000000000000000"

    # Avoid touching the real Code Interpreter SDK.
    monkeypatch.setattr(runners.CodeInterpreterClient, "start", lambda self: None)
    monkeypatch.setattr(runners.CodeInterpreterClient, "stop", lambda self: None)

    def writer(_task):
        workspace.write_text(sid, "solving/1.py", "print(42)")
        workspace.write_json(sid, "solving/1.json", {
            "success": True, "attempts": 2, "stdout": "42\n",
            "artifacts": ["solving/1/artifacts/plot.png"]})

    _patch_build_agent(monkeypatch, writer)
    monkeypatch.setattr(runners, "solver_tools", lambda sid_, ci: [])

    resp = runners.run_solver({"session_id": sid, "task_id": "1", "problem": "P",
                               "modeling_key": "k", "max_retries": 3})
    assert resp["ok"] is True
    assert resp["success"] is True
    assert resp["attempts"] == 2
    assert resp["code_key"].endswith("solving/1.py")
    assert resp["artifacts"] == ["solving/1/artifacts/plot.png"]
