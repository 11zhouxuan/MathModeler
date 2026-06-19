
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

import hashlib
import json
import logging

from typing import Any, Awaitable, Callable

logger = logging.getLogger("mm.supervisor")


def _completion_key(name: str, subtask: Any) -> str:
    """Re-entrancy cache key scoped to (sub-agent name, subtask).

    The cache exists to avoid RE-RUNNING an interrupted ``run_subagent`` call
    from the top on HITL resume (Strands re-invokes the tool). It must be keyed
    by BOTH the sub-agent name AND its subtask: the supervisor legitimately
    calls the SAME sub-agent (e.g. ``modeler``) many times with DIFFERENT
    subtasks (T1, T2, …). Keying by name alone made every later call short-
    circuit to the first task's cached result (cross-task contamination).
    """
    h = hashlib.sha1(str(subtask or "").encode("utf-8")).hexdigest()[:16]
    return f"{name}\x1f{h}"



def _short(value: Any) -> str:


    """Render a tool input/output as a one-line string for logs (NO truncation)."""
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, str):
            text = value
        else:
            text = str(value)
    except Exception:  # noqa: BLE001
        text = repr(value)
    return " ".join(text.split())  # collapse newlines/whitespace for one-line logs




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
        """Add run_subagent + ask_user + update_task + thinking to the supervisor;
        ask_user + thinking to each sub-agent."""
        run_subagent = self._make_run_subagent()
        ask_user = self._make_ask_user()
        update_task = self._make_update_task()
        thinking = self._make_thinking()
        _append_tools(self.supervisor, [run_subagent, ask_user, update_task, thinking])
        for sub in self.subagents.values():
            _append_tools(sub, [self._make_ask_user(), self._make_thinking()])

    def _make_ask_user(self):
        @tool(context=True)
        def ask_user(tool_context, question: str, prompt_type: str = "text") -> str:
            """Ask the end user a question and wait for their answer.

            Args:
                question: The question to ask the user.
                prompt_type: Interaction type. One of:
                    - "text": User types a free-form text answer (default).
                    - "confirm": User clicks a Confirm or Modify button.
                    - "choice": User selects from options listed in the question.
            """
            logger.info("[supervisor] ask_user -> %r (prompt_type=%s)", question, prompt_type)
            return tool_context.interrupt(
                "supervisor-ask",
                reason={"agent": "supervisor", "question": question, "inputType": prompt_type},
            )


        return ask_user

    def _make_update_task(self):
        """Create the update_task tool for the supervisor to report task progress.

        The supervisor calls this tool with the full task list (each item has
        id, title, status, deps) whenever the plan changes or a task transitions.
        The tool result is echoed back to the model; the streaming transformer
        (StrandsToAISDK) intercepts it and emits a ``data-task`` SSE frame so the
        front-end can render the DAG panel in real time.
        """
        @tool
        def update_task(tasks: list) -> str:
            """Update the task progress panel with the current state of all tasks.

            Args:
                tasks: A list of task objects. Each object has:
                    - id (str): Task identifier (e.g. "问题分析", "T1", "T2")
                    - title (str): Human-readable task title
                    - status (str): One of "idle", "active", "done"
                    - deps (list[str]): List of task IDs this task depends on

            Call this tool:
            1. At the very start with only the analysis task (status="active").
            2. After analysis completes, with the full task list from the DAG.
            3. Before each subsequent run_subagent call to mark the next task active.
            """
            logger.info("[supervisor] update_task: %d tasks — %s", len(tasks or []), json.dumps(tasks, ensure_ascii=False)[:2000])
            # Return the tasks in the result so the SSE transformer can also
            # emit data-task from the toolResult (more reliable than partial input).
            return json.dumps({"ok": True, "n_tasks": len(tasks or []), "tasks": tasks})

        return update_task

    def _make_thinking(self):
        @tool
        def thinking(thought: str) -> str:
            """Use this tool to think and reason about the current situation.

            Call this tool FIRST when you receive a new task to analyze the
            problem, determine the output language, and plan your approach.
            You can also call it anytime you need to reason through a
            difficult decision.

            Args:
                thought: Your internal reasoning. Include:
                    - What language the user is using (and therefore what
                      language you must output in)
                    - Your understanding of the current task
                    - Your plan for next steps
                    - Any concerns or decisions to make
            """
            logger.info("[supervisor] thinking: %s", thought[:200])
            return "OK"

        return thinking

    def _make_run_subagent(self):
        subagents = self.subagents
        completed = self._completed
        supervisor = self.supervisor


        async def _drive(sub, name, prompt):
            """Run a sub-agent's stream_async, yielding wrapped child events.

            The async-gen yields are turned into ToolStreamEvents by Strands so the
            supervisor's stream surfaces sub-agent tokens/tool-progress in real time.
            Returns the final AgentResult via the ``.result`` attribute trick.
            """
            result = None
            _seen_tools: set[str] = set()
            _logged_input: set[str] = set()
            _tool_name_by_id: dict[str, str] = {}
            try:
                async for ev in sub.stream_async(prompt):
                    if "data" in ev and ev["data"]:
                        yield {"node": name, "data": ev["data"]}
                    elif ev.get("current_tool_use") and ev["current_tool_use"].get("name"):
                        tu = ev["current_tool_use"]
                        tname = tu.get("name")
                        tid = tu.get("toolUseId") or tname
                        _tool_name_by_id[tid] = tname
                        # Log each distinct tool call the sub-agent makes (once per id).
                        if tid not in _seen_tools:
                            _seen_tools.add(tid)
                            logger.info("[supervisor] subagent %r -> tool %s", name, tname)
                        # Log the tool INPUT once the streamed args parse as valid JSON
                        # (the input arrives incrementally as a partial JSON string).
                        if tid not in _logged_input:
                            raw_in = tu.get("input")
                            parsed = raw_in
                            if isinstance(raw_in, str):
                                try:
                                    parsed = json.loads(raw_in)
                                except Exception:  # noqa: BLE001 - still partial
                                    parsed = None
                            if parsed is not None and parsed != "":
                                _logged_input.add(tid)
                                logger.info(
                                    "[supervisor] subagent %r tool %s INPUT: %s",
                                    name, tname, _short(parsed),
                                )
                        yield {"node": name, "current_tool_use": tu}
                    # Tool RESULT bubbles back as a user-role message carrying toolResult.
                    msg = ev.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        has_tool_result = False
                        for blk in msg.get("content", []) or []:
                            tr = blk.get("toolResult") if isinstance(blk, dict) else None
                            if not tr:
                                continue
                            has_tool_result = True
                            rid = tr.get("toolUseId")
                            rname = _tool_name_by_id.get(rid, "tool")
                            texts = [b.get("text") for b in tr.get("content", []) or []
                                     if isinstance(b, dict) and b.get("text")]
                            out = "\n".join(t for t in texts if t) or tr.get("status", "")
                            logger.info(
                                "[supervisor] subagent %r tool %s OUTPUT: %s",
                                name, rname, _short(out),
                            )
                        # Forward the tool-result message so the SSE transformer can
                        # close the matching tool card (state -> output-available).
                        if has_tool_result:
                            yield {"node": name, "message": msg}
                    # Nested ToolStreamEvent from sub-agent tools (e.g. execute_code
                    # streaming stdout). Surface as text so frontend shows live output.
                    tse = ev.get("tool_stream_event")
                    if isinstance(tse, dict):
                        tse_data = tse.get("data")
                        if isinstance(tse_data, dict) and "stdout_chunk" in tse_data:
                            chunk = tse_data["stdout_chunk"]
                            if chunk:
                                yield {"node": name, "data": chunk}
                    if "result" in ev:

                        result = ev["result"]

            except Exception:  # noqa: BLE001 - surface the real failure in the logs
                logger.exception("[supervisor] subagent %r stream_async CRASHED", name)
                raise
            _drive.last_result = result  # type: ignore[attr-defined]


        @tool(context=True)
        async def run_subagent(tool_context, description: str, name: str, subtask: str):
            """Delegate ``subtask`` to the named sub-agent and return its result.

            Args:
                description: ≤10字中文动作摘要（展示给用户，如"分析问题结构"）。
                name: Sub-agent name (analyst/modeler/solver/reporter).
                subtask: The task instruction to pass to the sub-agent.

            The sub-agent runs to completion (its progress streams live); if it calls
            ``ask_user`` the whole chain pauses for user input and resumes later.
            """
            logger.info(
                "[supervisor] run_subagent(name=%r) subtask=%s",
                name, _short(subtask or ""),
            )

            # Re-entrancy: a finished sub-agent call returns its cached result so an
            # interrupted call (re-run from the top by Strands on HITL resume) does
            # not re-hit the model / redo S3 side effects. Keyed by (name, subtask)
            # so the SAME sub-agent invoked for DIFFERENT subtasks (T1, T2, …) is
            # NOT short-circuited to an earlier task's result.
            ckey = _completion_key(name, subtask)
            if ckey in completed:
                logger.info("[supervisor] run_subagent(%r): cache hit (skip re-run) subtask=%s",
                            name, _short(subtask or ""))
                yield {"node": name, "result_text": completed[ckey]}
                yield {"status": "success", "content": [{"text": completed[ckey]}]}
                return


            sub = subagents.get(name)
            if sub is None:
                msg = f"unknown subagent: {name}"
                logger.warning("[supervisor] %s", msg)
                yield {"status": "error", "content": [{"text": msg}]}
                return

            # Four-stage progress marker (start) — surfaced as data-stage downstream.
            logger.info("[supervisor] subagent %r START", name)
            yield {"node": name, "stage_status": "start"}

            # LANGUAGE: handled purely via prompts. SUPERVISOR_SYSTEM instructs the
            # supervisor to write subtask instructions in the user's language, and
            # LANGUAGE_PREAMBLE + LANGUAGE_DIRECTIVE (prepended/appended to every
            # agent's system prompt) enforce that each agent outputs in the same
            # language as the user's problem. No code-side detection/rewrite here.
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
            completed[ckey] = text
            logger.info("[supervisor] subagent %r DONE (result %d chars)", name, len(text))

            yield {"node": name, "result_text": text}
            # async-gen tool: the LAST yield is the tool result (decorator.py:618-623).
            # Include a system-reminder to prompt supervisor to call update_task.
            reminder = (
                "\n\n<system-reminder>"
                "请在适当时机调用 update_task 工具更新任务进度面板。"
                "</system-reminder>"
            )
            yield {"status": "success", "content": [{"text": text + reminder}]}


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
                          "agent": reason.get("agent", "supervisor"),
                          "inputType": reason.get("inputType", "text")}}
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
