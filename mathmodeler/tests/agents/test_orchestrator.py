"""Orchestrator tests (tech-design §5, §10.2) — single-Runtime agents-as-tools.

Two orchestration strategies are covered:

* ``pipeline`` (deterministic fallback): we force cross-Runtime mode
  (``config.INPROCESS=False``) and replace ``invoke.invoke_agent`` with a
  recorder returning canned sub-agent results, then assert the deterministic
  four-stage order analyst → (modeler, solver)* in DAG order → reporter, plus
  the §5.4 SSE event protocol.

* ``supervisor`` (LLM free-routing, primary): we replace ``build_agent`` with a
  fake supervisor that calls the in-process ``invoke_*`` tools, and the
  in-process runners (``runners.run_*``) with recorders, then assert the stage
  events flow through the in-process event bus into the SSE stream.
"""
from __future__ import annotations

import json

from conftest import load_agent


def _force_pipeline_cross_runtime(monkeypatch, orch):
    """Pipeline mode + legacy cross-Runtime dispatch (invoke.invoke_agent)."""
    monkeypatch.setattr(orch.config, "ORCHESTRATION", "pipeline", raising=False)
    monkeypatch.setattr(orch.config, "INPROCESS", False, raising=False)
    # tools._dispatch reads config flags off the shared mm_common.config too.
    from mm_common import config as _cfg
    monkeypatch.setattr(_cfg, "ORCHESTRATION", "pipeline", raising=False)
    monkeypatch.setattr(_cfg, "INPROCESS", False, raising=False)


def _make_recorder(monkeypatch, orch):
    calls = []

    def fake_invoke(arn, payload, session_id):
        calls.append((arn, payload))
        if arn == orch.config.ANALYST_ARN:
            return {"ok": True, "order": ["1", "2"], "tasknum": 2,
                    "problem_analysis_key": "k", "task_descriptions_key": "k", "dag_key": "k"}
        if arn == orch.config.MODELER_ARN:
            return {"ok": True, "modeling_key": f"modeling/{payload['task_id']}.json",
                    "task_modeling_method": "MIP", "retrieved_methods": ["MIP"]}
        if arn == orch.config.SOLVER_ARN:
            return {"ok": True, "success": True, "attempts": 1,
                    "code_key": "c", "result_key": "r", "artifacts": []}
        if arn == orch.config.REPORTER_ARN:
            return {"ok": True, "report_key": "report/report.md",
                    "report_url": "https://example/report"}
        return {}

    # _dispatch (in tools) calls invoke.invoke_agent; patch it at the source.
    from mm_common import invoke as _invoke
    monkeypatch.setattr(_invoke, "invoke_agent", fake_invoke)
    monkeypatch.setattr(orch.memory, "save_event", lambda *a, **k: None)
    monkeypatch.setattr(orch.memory, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(orch.s3_io, "get_json", lambda sid, rel: [
        {"id": "1", "description": "d1"}, {"id": "2", "description": "d2"}])
    monkeypatch.setattr(orch, "build_agent", lambda *a, **k: object())
    # ARNs must be set for branch routing.
    monkeypatch.setattr(orch.config, "ANALYST_ARN", "arn:analyst", raising=False)
    monkeypatch.setattr(orch.config, "MODELER_ARN", "arn:modeler", raising=False)
    monkeypatch.setattr(orch.config, "SOLVER_ARN", "arn:solver", raising=False)
    monkeypatch.setattr(orch.config, "REPORTER_ARN", "arn:reporter", raising=False)
    return calls


def test_run_pipeline_order_and_response(monkeypatch):
    orch = load_agent("orchestrator")
    _force_pipeline_cross_runtime(monkeypatch, orch)
    calls = _make_recorder(monkeypatch, orch)

    resp = orch.run_pipeline({"session_id": "mm-orch-000000000000000000000000000",
                              "problem": "P", "actor_id": "u1"})

    arns = [c[0] for c in calls]
    # analyst first, reporter last
    assert arns[0] == "arn:analyst"
    assert arns[-1] == "arn:reporter"
    # per-task modeler then solver, in DAG order 1,2
    assert arns[1:5] == ["arn:modeler", "arn:solver", "arn:modeler", "arn:solver"]
    assert calls[1][1]["task_id"] == "1"
    assert calls[3][1]["task_id"] == "2"
    # final response
    assert resp["ok"] is True
    assert resp["order"] == ["1", "2"]
    assert resp["report_url"] == "https://example/report"


def test_stream_pipeline_sse_protocol(monkeypatch):
    orch = load_agent("orchestrator")
    _force_pipeline_cross_runtime(monkeypatch, orch)
    _make_recorder(monkeypatch, orch)

    chunks = list(orch.stream_pipeline({"session_id": "mm-orch-stream-00000000000000000000",
                                        "problem": "P", "actor_id": "u1"}))
    events = [json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]
    types = [e["type"] for e in events]

    assert "stage" in types and "final" in types
    # analysis done carries order
    analysis_done = [e for e in events if e.get("stage") == "analysis" and e.get("status") == "done"]
    assert analysis_done and analysis_done[0]["order"] == ["1", "2"]
    # a modeling event reports the method
    modeling = [e for e in events if e.get("stage") == "modeling" and e.get("status") == "done"]
    assert modeling and modeling[0]["method"] == "MIP"
    # final event carries the report url
    final = [e for e in events if e["type"] == "final"][-1]
    assert final["report_url"] == "https://example/report"


def test_empty_order_emits_error(monkeypatch):
    orch = load_agent("orchestrator")
    _force_pipeline_cross_runtime(monkeypatch, orch)
    _make_recorder(monkeypatch, orch)

    def analyst_empty(arn, payload, session_id):
        if arn == orch.config.ANALYST_ARN:
            return {"ok": True, "order": []}
        return {}

    from mm_common import invoke as _invoke
    monkeypatch.setattr(_invoke, "invoke_agent", analyst_empty)
    resp = orch.run_pipeline({"session_id": "mm-orch-empty-000000000000000000000",
                              "problem": "P"})
    assert resp["ok"] is False
    assert "order" in resp


def _collect_async(agen):
    """Drain an async generator into a list (helper for the streaming Supervisor)."""
    import asyncio

    async def _run():
        return [x async for x in agen]

    return asyncio.run(_run())


def test_supervisor_stream_emits_ai_sdk_frames(monkeypatch):
    """In supervisor mode the streaming Supervisor drives sub-agents via
    run_subagent and emits AI SDK v6 frames (start / data-stage / data-agent /
    data-final / finish / [DONE]). We inject fake Strands agents whose
    ``stream_async`` is scripted, so no Bedrock is touched.
    """
    import pytest
    pytest.importorskip("strands")

    orch = load_agent("orchestrator")
    from conftest import make_fake_supervisor_stack

    sup = make_fake_supervisor_stack()
    monkeypatch.setattr(orch, "build_supervisor", lambda session_id: sup)

    chunks = _collect_async(
        orch.stream_supervisor({"session_id": "mm-orch-sup-0000000000000000000000",
                                "problem": "P", "actor_id": "u1"}))
    events = [json.loads(c[len("data: "):]) for c in chunks
              if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    types = [e["type"] for e in events]

    assert types[0] == "start"
    assert "finish" in types
    assert any(c.strip() == "data: [DONE]" for c in chunks)

    # Four-stage data-stage progress markers (start + done) for each sub-agent.
    stage_seen = {(e["data"].get("stage"), e["data"].get("status"))
                  for e in events if e["type"] == "data-stage"}
    assert ("analysis", "start") in stage_seen
    assert ("analysis", "done") in stage_seen
    assert ("modeling", "done") in stage_seen
    assert ("solving", "done") in stage_seen
    assert ("report", "done") in stage_seen

    # Sub-agent tokens bubble up as data-agent frames.
    assert any(e["type"] == "data-agent" for e in events)

    # A final marker closes the run.
    assert any(e["type"] == "data-final" for e in events)


def test_supervisor_hitl_ask_then_resume(monkeypatch):
    """The modeler asks the user (ask_user) -> the stream pauses with a data-ask
    frame carrying the supervisor-level interrupt id; a second request with
    interruptResponses (same session) resumes and reaches the final marker.
    """
    import pytest
    pytest.importorskip("strands")

    orch = load_agent("orchestrator")
    from conftest import make_fake_supervisor_stack

    sup = make_fake_supervisor_stack(ask=True)
    monkeypatch.setattr(orch, "build_supervisor", lambda session_id: sup)

    sid = "mm-orch-hitl-0000000000000000000000"
    chunks1 = _collect_async(
        orch.stream_supervisor({"session_id": sid, "problem": "P"}))
    events1 = [json.loads(c[len("data: "):]) for c in chunks1
               if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    asks = [e for e in events1 if e["type"] == "data-ask"]
    assert asks, "expected a data-ask frame when the sub-agent calls ask_user"
    interrupt_id = asks[0]["data"]["interruptId"]
    assert interrupt_id

    resume_body = {
        "session_id": sid,
        "interruptResponses": [
            {"interruptResponse": {"interruptId": interrupt_id, "response": "42"}}
        ],
    }
    chunks2 = _collect_async(orch.stream_supervisor(resume_body))
    events2 = [json.loads(c[len("data: "):]) for c in chunks2
               if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    assert any(e["type"] == "data-final" for e in events2), \
        "supervisor did not finish after the HITL resume"


