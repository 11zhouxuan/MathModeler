"""Orchestrator Runtime — single-Runtime supervisor over the four-stage pipeline.

This is the only AgentCore Runtime in the merged single-image deployment. It runs
a self-built streaming :class:`mm_common.supervisor.Supervisor` (design
``docs/research/supervisor-streaming-hitl-design.md``):

  * The supervisor LLM ``Agent`` decides which sub-agent to call via the framework
    tool ``run_subagent(name, subtask)``; each sub-agent (Analyst/Modeler/Solver/
    Reporter) is a Strands ``Agent`` built in-process from ``mm_common.runners``.
  * ``Supervisor.stream()`` drives ``supervisor.stream_async`` and surfaces sub-agent
    tokens/tool-progress in real time (``run_subagent`` is an async-gen tool whose
    yields bubble up as ``ToolStreamEvent``). ``mm_common.streaming.StrandsToAISDK``
    converts the raw Strands events into AI SDK v6 UI-message-stream SSE frames
    (text-* / tool-* / data-stage / data-agent / data-ask / data-final).
  * HITL ``ask_user`` (supervisor or any sub-agent) pauses the stream with a
    ``data-ask`` frame; the front-end answers in a *later* request carrying
    ``interruptResponses``. The Supervisor restores its state from the
    ``StateStore`` (S3) and resumes.

Strategies (``config.ORCHESTRATION``):
  * ``"supervisor"`` (default) — the streaming Supervisor described above.
  * ``"pipeline"``  — a deterministic four-stage loop kept as a demo-stable,
    non-LLM fallback (synchronous; emits the legacy four-stage SSE protocol).
"""
from __future__ import annotations

import asyncio
import json
import logging

from mm_common import config, events, memory, s3_io, workspace

# Emit INFO-level logs to stdout so the orchestrator's tool calls / sub-agent
# activity are visible in the container/uvicorn logs (otherwise only uvicorn's
# access lines "POST /invocations 200 OK" show up).
#
# uvicorn installs its own logging handlers on the root logger, which makes a
# bare ``logging.basicConfig`` a no-op. To guarantee our ``mm.*`` loggers always
# emit at INFO regardless of how the app is launched, attach a dedicated stdout
# StreamHandler to the ``mm`` parent logger and force its level.
def _configure_mm_logging() -> None:
    import sys

    mm_root = logging.getLogger("mm")
    mm_root.setLevel(logging.INFO)
    if not any(getattr(h, "_mm_handler", False) for h in mm_root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler._mm_handler = True  # type: ignore[attr-defined]
        mm_root.addHandler(handler)
    # Don't double-emit through the (uvicorn-configured) root logger.
    mm_root.propagate = False


_configure_mm_logging()
logger = logging.getLogger("mm.orchestrator")


from mm_common.llm import build_agent
from mm_common.prompts import SUPERVISOR_SYSTEM
from mm_common.runners import (
    set_session_context,
    BUILTIN_TOOLS,
    build_analyst_agent,
    build_modeler_agent,
    build_reporter_agent,
    build_solver_agent,
)
from mm_common.server import make_app
from mm_common.state_store import MemoryStateStore, S3StateStore
from mm_common.streaming import StrandsToAISDK
from mm_common.supervisor import Supervisor
from mm_common.tools import _dispatch


def _sse(obj: dict) -> str:
    """Serialise one SSE ``data:`` line (legacy four-stage protocol / pipeline)."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _safe_save_event(session_id: str, text: str) -> None:
    try:
        memory.save_event(session_id, "orchestrator", "assistant", text)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Streaming Supervisor (primary; config.ORCHESTRATION == "supervisor")
# ---------------------------------------------------------------------------
def _make_state_store():
    """S3-backed store when a doc bucket is configured, else in-process memory."""
    if config.DOC_BUCKET:
        try:
            return S3StateStore()
        except Exception:  # noqa: BLE001
            pass
    return MemoryStateStore()


# Process-global state store so a paused session can resume in a later request
# served by the same Runtime instance (S3 makes it durable across instances too).
_STATE_STORE = None

# Track running Supervisors by session_id so /cancel can call agent.cancel().
_RUNNING: dict[str, Supervisor] = {}


def cancel_session(session_id: str) -> bool:
    """Cancel a running session's agents using Strands Agent.cancel().

    cancel() is thread-safe and sets _cancel_signal — the agent stops at the
    next checkpoint (during model streaming or before tool execution) and
    returns with stop_reason="cancelled".
    """
    sup = _RUNNING.get(session_id)
    if not sup:
        logger.warning("[orchestrator] CANCEL session=%s — NOT FOUND in _RUNNING", session_id)
        return False
    logger.info("[orchestrator] CANCEL session=%s", session_id)
    try:
        sup.supervisor.cancel()
    except Exception:  # noqa: BLE001
        pass
    for name, a in sup.subagents.items():
        try:
            a.cancel()
        except Exception:  # noqa: BLE001
            pass
    return True



def _state_store():
    global _STATE_STORE
    if _STATE_STORE is None:
        _STATE_STORE = _make_state_store()
    return _STATE_STORE


def build_supervisor(session_id: str) -> Supervisor:
    """Construct the MathModeler Supervisor with the four in-process sub-agents."""
    from mm_common.llm import make_session_manager
    sm = make_session_manager(session_id, "supervisor")
    supervisor_agent = build_agent(SUPERVISOR_SYSTEM, tools=list(BUILTIN_TOOLS), session_manager=sm)
    subagents = {
        "analyst": build_analyst_agent(session_id),
        "modeler": build_modeler_agent(session_id),
        "solver": build_solver_agent(session_id),
        "reporter": build_reporter_agent(session_id),
    }
    return Supervisor(
        supervisor=supervisor_agent,
        subagents=subagents,
        session_id=session_id,
        state_store=_state_store(),
    )


def _build_task(body: dict) -> str:
    """Build the user message for the supervisor.

    Only the user's raw problem (inside <user_problem> delimiters) plus the
    session_id. All workflow/coordination instructions live in the system prompt
    (SUPERVISOR_SYSTEM). Keeping this message minimal and in the user's own
    language ensures the LLM naturally responds in that language.
    """
    session_id = body["session_id"]
    problem = body["problem"]
    return (
        f"session_id={session_id}\n\n"
        f"<user_problem>\n{problem}\n</user_problem>"
    )



async def stream_supervisor(body: dict):
    """Async SSE generator (AI SDK v6 frames) driving the streaming Supervisor."""
    session_id = body["session_id"]
    problem = body.get("problem", "")
    logger.info(
        "[orchestrator] /invocations session=%s problem_len=%d keys=%s",
        session_id, len(problem or ""), sorted(body.keys()),
    )
    # Initialize session workspace (creates directory structure)
    workspace.init_session(session_id)
    set_session_context(session_id)

    sup = build_supervisor(session_id)
    _RUNNING[session_id] = sup
    tx = StrandsToAISDK()

    interrupt_responses = body.get("interruptResponses")
    if interrupt_responses:
        logger.info("[orchestrator] RESUME session=%s with %d interruptResponses",
                    session_id, len(interrupt_responses))
        sup.restore(_state_store().load(session_id))
        gen = sup.stream(resume=interrupt_responses)
    else:
        # Try to restore previous state so _completed cache is loaded.
        # This allows the supervisor to skip already-finished sub-agents
        # when a session is continued after a disconnect.
        saved = _state_store().load(session_id)
        if saved and saved.get("completed"):
            logger.info("[orchestrator] CONTINUE session=%s (restoring %d completed tasks)",
                        session_id, len(saved["completed"]))
            sup.restore(saved)
            # Inject system reminder about disconnect recovery into the task
            completed_keys = list(saved["completed"].keys())
            completed_info = ", ".join(
                k.replace("\x1f", "/") for k in completed_keys
            )
            problem = body.get("problem", "")
            task_text = (
                f"session_id={session_id}\n\n"
                f"<system-reminder>\n"
                f"这是一个中断恢复的 session。以下子任务已完成（无需重复执行）：{completed_info}。\n"
                f"请检查你的对话历史，确认当前进度，从上次中断的位置继续执行。\n"
                f"如果上一次 run_subagent 调用因中断未返回结果，请重新执行该调用。\n"
                f"</system-reminder>\n\n"
                f"<user_problem>\n{problem}\n</user_problem>"
            )
            logger.info("[orchestrator] START session=%s (task %d chars, resume mode)",
                        session_id, len(task_text))
            gen = sup.stream(task=task_text)
        else:
            task = _build_task(body)
            logger.info("[orchestrator] START session=%s (task %d chars)", session_id, len(task))
            gen = sup.stream(task=task)

    try:
        async for frame in tx.run(gen):
            yield frame
    finally:
        _RUNNING.pop(session_id, None)
        # Best-effort: stop any Solver Code Interpreter session opened for this run.
        _teardown_subagents(sup)



def _teardown_subagents(sup: Supervisor) -> None:
    for a in getattr(sup, "subagents", {}).values():
        ci = getattr(a, "_ci", None)
        if ci is not None:
            try:
                ci.stop()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Deterministic four-stage pipeline (fallback; config.ORCHESTRATION == "pipeline")
# ---------------------------------------------------------------------------
def _run(body: dict, emit=None) -> dict:
    """Core deterministic pipeline. ``emit(obj)`` (optional) yields SSE events."""
    session_id = body["session_id"]
    problem = body["problem"]
    actor_id = body.get("actor_id", "anonymous")

    sink = events.EventSink(on_event=emit)
    token = events.bind_sink(sink)
    try:
        try:
            memory.retrieve(actor_id=actor_id, query=problem[:512],
                            namespace="preferences", top_k=5)
        except Exception:  # noqa: BLE001
            pass

        analyst = _dispatch(
            "analyst", config.ANALYST_ARN,
            {"session_id": session_id, "problem": problem, "with_code": True,
             "actor_id": actor_id},
            session_id,
        )
        order = analyst.get("order", []) if isinstance(analyst, dict) else []
        if not order:
            msg = "Analyst returned an empty task order"
            sink.publish({"type": "error", "message": msg})
            return {"ok": False, "error": msg, "report_key": "", "report_url": "", "order": []}
        _safe_save_event(session_id, f"analysis done; order={order}")

        task_descriptions = {}
        try:
            for t in s3_io.get_json(session_id, "analysis/task_descriptions.json") or []:
                task_descriptions[str(t.get("id"))] = t.get("description", "")
        except Exception:  # noqa: BLE001
            task_descriptions = {}

        for tid in order:
            modeler = _dispatch(
                "modeler", config.MODELER_ARN,
                {"session_id": session_id, "task_id": tid, "problem": problem,
                 "task_description": task_descriptions.get(str(tid), ""), "with_code": True},
                session_id,
            )
            modeling_key = modeler.get("modeling_key",
                                       s3_io._key(session_id, f"modeling/{tid}.json")) \
                if isinstance(modeler, dict) else s3_io._key(session_id, f"modeling/{tid}.json")
            _safe_save_event(session_id, f"modeling task {tid} done")

            solver = _dispatch(
                "solver", config.SOLVER_ARN,
                {"session_id": session_id, "task_id": tid, "problem": problem,
                 "modeling_key": modeling_key, "dependent_file_prompt": "",
                 "max_retries": config.SOLVER_MAX_RETRIES},
                session_id,
            )
            success = bool(solver.get("success", False)) if isinstance(solver, dict) else False
            _safe_save_event(session_id, f"solving task {tid} done; success={success}")

        reporter = _dispatch(
            "reporter", config.REPORTER_ARN,
            {"session_id": session_id, "problem": problem, "order": order},
            session_id,
        )
        report_key = reporter.get("report_key", "") if isinstance(reporter, dict) else ""
        report_url = reporter.get("report_url", "") if isinstance(reporter, dict) else ""
        _safe_save_event(session_id, "report done")
        sink.publish({"type": "final", "report_key": report_key, "report_url": report_url})

        return {"ok": True, "report_key": report_key, "report_url": report_url, "order": order}
    finally:
        events.reset_sink(token)


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------
def run_pipeline(body: dict) -> dict:
    """Synchronous (non-streaming) entry — returns the final SolveResponse JSON.

    Always uses the deterministic pipeline (the supervisor path is streaming-only).
    """
    return _run(body, emit=None)


def stream_pipeline(body: dict):
    """SSE entry. Returns an async generator (Supervisor) or a sync generator
    (deterministic pipeline) depending on ``config.ORCHESTRATION``.

    FastAPI's ``StreamingResponse`` accepts both sync and async iterables.
    """
    if config.ORCHESTRATION == "supervisor":
        return stream_supervisor(body)
    return _stream_pipeline_sync(body)


def _stream_pipeline_sync(body: dict):
    """Legacy deterministic four-stage SSE generator (collect-then-emit)."""
    events_collected: list[dict] = []
    try:
        final = _run(body, emit=events_collected.append)
    except Exception as e:  # noqa: BLE001
        events_collected.append({"type": "error", "message": str(e)})
        final = None
    for ev in events_collected:
        yield _sse(ev)
    if final is not None and not any(e.get("type") == "final" for e in events_collected):
        yield _sse({"type": "final",
                    "report_key": final.get("report_key", ""),
                    "report_url": final.get("report_url", "")})


app = make_app(handler=run_pipeline, stream_handler=stream_pipeline)


# --- cancel (agent-level stop) ---------------------------------------------
from fastapi import Request as _CancelRequest
from fastapi.responses import JSONResponse as _CancelResponse


@app.post("/cancel")
async def _cancel_route(request: _CancelRequest) -> _CancelResponse:
    """Cancel a running session by calling Agent.cancel() on all active agents."""
    body = await request.json()
    session_id = (body or {}).get("session_id", "")
    found = cancel_session(session_id) if session_id else False
    return _CancelResponse({"cancelled": found, "session_id": session_id})

