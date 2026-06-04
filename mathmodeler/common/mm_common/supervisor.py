
"""mm_common.supervisor — generic streaming + HITL multi-agent orchestrator.

A domain-agnostic ``Supervisor`` that wraps an already-instantiated supervisor
``Agent`` plus a set of already-instantiated sub-agents, and:

  * injects two framework tools into the supervisor — ``run_subagent(name, subtask)``
    and ``ask_user(question)``;
  * injects the same ``ask_user`` into every sub-agent (uniform HITL protocol);
  * drives ``supervisor.stream_async`` and yields the *raw* Strands events, so a
    transformer (``mm_common.streaming.StrandsToAISDK``) can turn them into SSE.
    Sub-agent events bubble up as ``ToolStreamEvent`` because ``run_subagent`` is an
    **async-generator tool** (verified against Strands 1.42.0 — see
    ``scripts/verify_supervisor.py``);
  * bridges the two-layer interrupt: a sub-agent ``ask_user`` raises a *child*
    interrupt inside ``run_subagent``; ``run_subagent`` re-raises it as a
    *supervisor-level* ``supervisor-ask`` interrupt carrying ``child_interrupt_id``.
    The front-end only ever sees the supervisor interrupt id;
  * serialize()/restore() the whole thing (per-agent ``messages`` + ``interrupt_state``
    + a completed-stage cache) so ``ask_user`` can pause one HTTP request and resume in
    a later one.

Re-entrancy (§3.3): on resume Strands *re-runs* the interrupted tool from the top.
To avoid re-running an already-finished sub-agent (which would re-hit the model and
re-do S3 side effects), ``run_subagent`` consults ``self._completed`` — a
``{name: result_text}`` cache — and returns the cached result immediately if present.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

try:  # pragma: no cover - exercised indirectly; tests inject fakes
    from strands import tool
    from strands.interrupt import _InterruptState
except Exception:  # strands not installed (pure-wiring unit tests)
    def tool(func=None, **_kwargs):
        if func is None:
            def _wrap(f):
                return f
            return _wrap
        return func

    _InterruptState = None  # type: ignore


def _extract_text(result: Any) -> str:
    """Best-effort plain-text extraction from a Strands AgentResult."""
    if result is None:
        return ""
    try:
        return str(result)
    except Exception:  # noqa: BLE001
        return ""


class Supervisor:
    """Generic supervisor over a set of sub-agents (agents-as-tools + HITL)."""

    def __init__(
        self,
        supervisor,
        subagents: dict,
        *,
        session_id: str | None = None,
        state_store=None,
        max_iterations: int = 20,
        on_domain_event: Callable[[dict], None] | None = None,
    ):
        self.supervisor = supervisor
        self.subagents = dict(subagents)
        self.session_id = session_id
        self.state_store = state_store
        self.max_iterations = max_iterations
        self._on_domain_event = on_domain_event

        # Re-entrancy cache: name -> result text (set when a sub-agent finishes).
        self._completed: dict[str, str] = {}
        # pending ask map: supervisor_interrupt_id -> {agent, question, child_interrupt_id}
        self._pending: dict[str, dict] = {}

        self._inject_tools()

    # ------------------------------------------------------------------ tools
    def _inject_tools(self) -> None:
        """Add run_subagent + ask_user to the supervisor and ask_user to each sub-agent."""
        run_subagent = self._make_run_subagent()
        ask_user = self._make_ask_user()
        _append_tools(self.supervisor, [run_subagent, ask_user])
        for sub in self.subagents.values():
            _append_tools(sub, [self._make_ask_user()])

    def _make_ask_user(self):
        @tool(context=True)
        def ask_user(tool_context, question: str) -> str:
            """Ask the end user a clarifying question and wait for their answer."""
            return tool_context.interrupt(
                "supervisor-ask", reason={"agent": "supervisor", "question": question}
            )

        return ask_user

    def _make_run_subagent(self):
        subagents = self.subagents
        completed = self._completed

        async def _drive(sub, name, prompt):
            """Run a sub-agent's stream_async, yielding wrapped child events.

            The async-gen yields are turned into ToolStreamEvents by Strands so the
            supervisor's stream surfaces sub-agent tokens/tool-progress in real time.
            Returns the final AgentResult via the ``.result`` attribute trick.
            """
            result = None
            async for ev in sub.stream_async(prompt):
                if "data" in ev and ev["data"]:
                    yield {"node": name, "data": ev["data"]}
                elif ev.get("current_tool_use") and ev["current_tool_use"].get("name"):
                    yield {"node": name, "current_tool_use": ev["current_tool_use"]}
                if "result" in ev:
                    result = ev["result"]
            _drive.last_result = result  # type: ignore[attr-defined]

        @tool(context=True)
        async def run_subagent(tool_context, name: str, subtask: str):
            """Delegate ``subtask`` to the named sub-agent and return its result.

            The sub-agent runs to completion (its progress streams live); if it calls
            ``ask_user`` the whole chain pauses for user input and resumes later.
            """
            # Re-entrancy: a finished sub-agent returns its cached result (no re-run).
            if name in completed:
                yield {"node": name, "result_text": completed[name]}
                yield {"status": "success", "content": [{"text": completed[name]}]}
                return

            sub = subagents.get(name)
            if sub is None:
                msg = f"unknown subagent: {name}"
                yield {"status": "error", "content": [{"text": msg}]}
                return

            # Four-stage progress marker (start) — surfaced as data-stage downstream.
            yield {"node": name, "stage_status": "start"}
            gen = _drive(sub, name, subtask)
            async for ev in gen:
                yield ev
            r = getattr(_drive, "last_result", None)


            iterations = 0
            while r is not None and getattr(r, "stop_reason", None) == "interrupt" \
                    and iterations < 50:
                iterations += 1
                child = r.interrupts[0]
                creason = child.reason if isinstance(child.reason, dict) else {}
                ans = tool_context.interrupt(
                    "supervisor-ask",
                    reason={"agent": name, "question": creason.get("question"),
                            "child_interrupt_id": child.id},
                )
                gen = _drive(sub, name,
                             [{"interruptResponse": {"interruptId": child.id, "response": ans}}])
                async for ev in gen:
                    yield ev
                r = getattr(_drive, "last_result", None)

            text = _extract_text(r)
            completed[name] = text
            yield {"node": name, "result_text": text}
            # async-gen tool: the LAST yield is the tool result (decorator.py:618-623).
            yield {"status": "success", "content": [{"text": text}]}

        return run_subagent

    # ----------------------------------------------------------------- stream
    async def stream(self, task: Any = None, *, resume: Any = None):
        """Async generator over raw Strands events for one request.

        * first turn:  ``task`` = the user problem, ``resume`` = None.
        * resume turn: ``task`` = None, ``resume`` = the interruptResponses list.

        After the supervisor stops, a domain ``{"mm": {...}}`` event is yielded:
        either ``{"type":"ask",...}`` (paused on HITL) or ``{"type":"final",...}``.
        """
        prompt = resume if resume is not None else task
        result = None
        async for ev in self.supervisor.stream_async(prompt):
            yield ev
            if "result" in ev:
                result = ev["result"]

        if result is not None and getattr(result, "stop_reason", None) == "interrupt":
            sup_int = result.interrupts[0]
            reason = sup_int.reason if isinstance(sup_int.reason, dict) else {}
            self._pending[sup_int.id] = {
                "agent": reason.get("agent", "supervisor"),
                "question": reason.get("question", ""),
                "child_interrupt_id": reason.get("child_interrupt_id"),
            }
            self._persist()
            yield {"mm": {"type": "ask", "interruptId": sup_int.id,
                          "question": reason.get("question", ""),
                          "agent": reason.get("agent", "supervisor")}}
            return

        # Completed: persist final state (idempotent) and emit a final marker.
        self._persist()
        yield {"mm": {"type": "final", "text": _extract_text(result)}}

    # -------------------------------------------------------------- persistence
    def serialize(self) -> dict:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "completed": dict(self._completed),
            "pending": dict(self._pending),
            "supervisor": _dump_agent(self.supervisor),
            "subagents": {name: _dump_agent(a) for name, a in self.subagents.items()},
        }
        return data

    def restore(self, data: dict | None) -> None:
        if not data:
            return
        # Mutate caches IN PLACE so the run_subagent closure (which captured these
        # dict objects at construction) observes the restored values.
        self._completed.clear()
        self._completed.update(data.get("completed", {}))
        self._pending.clear()
        self._pending.update(data.get("pending", {}))
        _load_agent(self.supervisor, data.get("supervisor"))
        for name, a in self.subagents.items():
            _load_agent(a, data.get("subagents", {}).get(name))


    def _persist(self) -> None:
        if self.state_store is not None and self.session_id:
            try:
                self.state_store.save(self.session_id, self.serialize())
            except Exception:  # noqa: BLE001 - persistence failure must not abort the stream
                pass


# ------------------------------------------------------------------ helpers
def _append_tools(agent, new_tools: list) -> None:
    """Register additional tools on an existing Strands Agent (best-effort)."""
    reg = getattr(agent, "tool_registry", None)
    if reg is not None and hasattr(reg, "process_tools"):
        try:
            reg.process_tools(new_tools)
            return
        except Exception:  # noqa: BLE001
            pass
    # Fallback for fakes used in unit tests.
    existing = list(getattr(agent, "tools", []) or [])
    existing.extend(new_tools)
    try:
        agent.tools = existing
    except Exception:  # noqa: BLE001
        pass


def _replace_framework_tools(agent, run_subagent, ask_user) -> None:
    """Replace the framework tools after a restore (re-register)."""
    _append_tools(agent, [run_subagent, ask_user])


def _dump_agent(agent) -> dict:
    out: dict[str, Any] = {}
    try:
        out["messages"] = list(getattr(agent, "messages", []) or [])
    except Exception:  # noqa: BLE001
        out["messages"] = []
    st = getattr(agent, "_interrupt_state", None)
    if st is not None and hasattr(st, "to_dict"):
        try:
            out["interrupt_state"] = st.to_dict()
        except Exception:  # noqa: BLE001
            out["interrupt_state"] = None
    return out


def _load_agent(agent, data: dict | None) -> None:
    if not data:
        return
    if "messages" in data:
        try:
            agent.messages = list(data["messages"])
        except Exception:  # noqa: BLE001
            pass
    ist = data.get("interrupt_state")
    if ist and _InterruptState is not None:
        try:
            agent._interrupt_state = _InterruptState.from_dict(ist)
        except Exception:  # noqa: BLE001
            pass
