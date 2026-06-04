"""Fixtures for agent tests — add each agent dir to sys.path and provide a
moto-backed S3 doc bus plus a fake Strands agent factory.

Agent ``app.py`` modules do ``from mm_common ...`` and ``import`` siblings by
filename (``app``). Since all five live in different dirs but share the module
name ``app``, tests import them lazily via ``importlib`` with an explicit path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]  # mathmodeler/
_COMMON = _ROOT / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

# Both tests/common/conftest-less tests and tests/agents share the bare module
# name ``conftest``; when the whole suite runs, only one ``conftest`` module
# wins in ``sys.modules``. Re-export the shared helpers/fixtures from the root
# conftest so ``from conftest import FakeEmbeddingScorer`` resolves regardless
# of import order.
_ROOT_CONFTEST = _ROOT / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_root_conftest", _ROOT_CONFTEST)
_root = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root)
FakeEmbeddingScorer = _root.FakeEmbeddingScorer
sample_hmml_tree = _root.sample_hmml_tree


def load_agent(name: str):
    """Import ``agents/<name>/app.py`` as a uniquely-named module."""
    path = _ROOT / "agents" / name / "app.py"
    spec = importlib.util.spec_from_file_location(f"agent_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Scripted FakeModel + Supervisor stack for streaming/HITL tests.
#
# These require a REAL ``strands`` install (the Supervisor relies on the genuine
# @tool decorator + interrupt machinery). Tests that use them must guard with
# ``pytest.importorskip("strands")`` so the AWS-free default suite still passes
# when strands is absent; run the full streaming suite with:
#   uv run --with strands-agents ... pytest tests/agents/test_orchestrator.py
# ---------------------------------------------------------------------------
def _build_fake_model(planner):
    """Construct a scripted ``strands.models.Model`` driven by ``planner``."""
    from strands.models import Model

    def _chunks(s, n=8):
        for i in range(0, len(s), n):
            yield s[i:i + n]

    class FakeModel(Model):
        def __init__(self):
            self._cfg = {}

        def update_config(self, **c):
            self._cfg.update(c)

        def get_config(self):
            return self._cfg

        async def structured_output(self, *a, **k):  # pragma: no cover
            raise NotImplementedError
            yield {}

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            plan = planner(messages)
            yield {"messageStart": {"role": "assistant"}}
            if "text" in plan:
                yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
                for piece in _chunks(plan["text"]):
                    yield {"contentBlockDelta": {"delta": {"text": piece},
                                                 "contentBlockIndex": 0}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
                yield {"messageStop": {"stopReason": "end_turn"}}
            else:
                tid = plan.get("id", "tu-1")
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tid,
                        "name": plan["tool"]}}, "contentBlockIndex": 0}}
                yield {"contentBlockDelta": {"delta": {"toolUse":
                        {"input": json.dumps(plan["input"])}}, "contentBlockIndex": 0}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
                yield {"messageStop": {"stopReason": "tool_use"}}

    return FakeModel()


def _count_tooluses(messages):
    c = 0
    for m in messages:
        if m.get("role") == "assistant":
            for blk in m.get("content", []):
                if isinstance(blk, dict) and "toolUse" in blk:
                    c += 1
    return c


def _make_subagent(name, result_text, *, ask_first=False, question="问题？"):
    """Build a real Strands sub-agent. With ``ask_first`` it calls ask_user once
    (the Supervisor injects ask_user), then finishes after the answer arrives."""
    from strands import Agent

    asked = {"n": 0}

    def planner(messages):
        # The sub-agent only emits text (it has no own tools except injected
        # ask_user). To trigger ask_user we would need the model to call it, but
        # the Supervisor injects ask_user as a tool; for the ask path the model
        # calls it on the first turn, then emits the result text afterwards.
        if ask_first and asked["n"] == 0:
            asked["n"] = 1
            return {"tool": "ask_user", "input": {"question": question},
                    "id": f"{name}-ask"}
        return {"text": result_text}

    return Agent(model=_build_fake_model(planner), system_prompt=name, tools=[],
                 callback_handler=None)


def make_fake_supervisor_stack(*, ask=False):
    """Build a real Supervisor wired with a scripted supervisor + four sub-agents.

    The supervisor model deterministically runs analyst -> modeler -> solver ->
    reporter via run_subagent, then emits a FINAL text. With ``ask=True`` the
    modeler asks the user once.
    """
    from strands import Agent

    from mm_common.supervisor import Supervisor

    subagents = {
        "analyst": _make_subagent("analyst", "ORDER=[1]"),
        "modeler": _make_subagent("modeler", "MODELING done",
                                  ask_first=ask, question="How many widgets?"),
        "solver": _make_subagent("solver", "SOLVING done"),
        "reporter": _make_subagent("reporter", "REPORT done"),
    }
    plan = [("analyst", "analyze"), ("modeler", "model t1"),
            ("solver", "solve t1"), ("reporter", "report")]

    def sup_planner(messages):
        n = _count_tooluses(messages)
        if n < len(plan):
            name, subtask = plan[n]
            return {"tool": "run_subagent",
                    "input": {"name": name, "subtask": subtask}, "id": f"sup-{n}"}
        return {"text": "FINAL: report assembled."}

    supervisor = Agent(model=_build_fake_model(sup_planner), system_prompt="sup",
                       tools=[], callback_handler=None)
    return Supervisor(supervisor=supervisor, subagents=subagents,
                      session_id="mm-fake-000000000000000000000000000")


@pytest.fixture
def doc_bucket(monkeypatch):
    """moto S3 bucket wired into mm_common.config + a fresh s3 client."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        from mm_common import config, s3_io

        bucket = "mm-test-bus"
        monkeypatch.setattr(config, "DOC_BUCKET", bucket, raising=False)
        client = boto3.client("s3", region_name=config.REGION)
        client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": config.REGION},
        )
        # Reset cached client in s3_io so it picks up the moto-mocked one.
        if hasattr(s3_io, "_client"):
            monkeypatch.setattr(s3_io, "_client", None, raising=False)
        yield bucket
