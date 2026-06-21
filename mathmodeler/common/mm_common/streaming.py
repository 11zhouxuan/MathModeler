"""mm_common.streaming — Strands events → AI SDK UI-message-stream SSE frames.

Adapted from agent-craft's ``StrandsEventStreamToSSEStreamTransformer``
(``shared/streaming/stream_protocol_v5.py``; the "v5" wire format is identical,
for the chunk types we use, to AI SDK v6 — agent-craft ships no v6 file).

MESSAGE MODEL (ONE UIMessage, split into bubbles on the frontend):
  Like agent-craft, MathModeler emits the whole run as a SINGLE assistant
  UIMessage (one ``start`` … one ``finish``). The ordered ``parts`` carry:

    * The Supervisor's narration text  -> ``text-*`` parts.
    * EACH sub-agent invocation        -> a ``data-agent`` part (the sub-agent's
      nested ``parts``: text + one tool card per tool call).
    * Stage / final / ask markers      -> ``data-stage`` / ``data-final`` /
      ``data-ask`` parts.

  The FRONTEND (``groupAssistantParts``) splits this single message into separate
  chat bubbles by owner — the supervisor narration is one bubble and each
  ``data-agent`` run is its own bubble — so we do NOT segment at the wire level
  (mid-stream ``finish``/``start`` cycles caused the supervisor text to be
  duplicated across messages in @ai-sdk/react).

  The Supervisor's ``run_subagent`` / ``ask_user`` tool frames are NEVER surfaced
  as plain ``tool-*`` cards. ``ask_user`` (by the supervisor OR a sub-agent) is
  surfaced only as ``data-ask`` (HITL banner + question text); the composer keeps
  a normal "send" button.

The whole SSE stream begins with ``start`` and ends with ``finish`` + ``[DONE]``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterable

logger = logging.getLogger("mm.streaming")


# Map sub-agent name -> the four-stage SSE label used by the portal/frontend.
_STAGE_OF = {
    "analyst": "analysis",
    "modeler": "modeling",
    "solver": "solving",
    "reporter": "report",
}

# Framework tools that must never render as a tool card (supervisor or sub-agent).
_HIDDEN_TOOLS = {"ask_user", "update_task"}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _done() -> str:
    return "data: [DONE]\n\n"


def _new_id() -> str:
    return uuid.uuid4().hex


class _SubAgentAccumulator:
    """Accumulates ONE sub-agent run into an ordered ``parts`` list.

    parts: text runs (``{type:'text', text, state}``) interleaved with one
    ``{type:'tool-<name>', toolCallId, state, input, output}`` per tool call.
    """

    def __init__(self, node: str) -> None:
        self.node = node
        self.parts: list[dict] = []
        self._active_text: dict | None = None
        self._tools: dict[str, dict] = {}

    def add_text(self, delta: str) -> None:
        if not delta:
            return
        if self._active_text is None:
            self._active_text = {"type": "text", "text": "", "state": "streaming"}
            self.parts.append(self._active_text)
        self._active_text["text"] += delta

    def close_text(self) -> None:
        if self._active_text is not None:
            self._active_text["state"] = "done"
            self._active_text = None

    def tool_input(self, tool_id: str, name: str, tool_input: Any) -> None:
        self.close_text()
        # Extract the `description` field from tool input for UI display title.
        title = ""
        if isinstance(tool_input, dict) and "description" in tool_input:
            title = str(tool_input["description"])
        part = self._tools.get(tool_id)
        if part is None:
            part = {
                "type": f"tool-{name}",
                "toolCallId": tool_id,
                "state": "input-available",
                "input": tool_input,
            }
            if title:
                part["title"] = title
            self._tools[tool_id] = part
            self.parts.append(part)
        else:
            part["input"] = tool_input
            if title:
                part["title"] = title
            if part.get("state") in (None, "input-streaming"):
                part["state"] = "input-available"

    def tool_output(self, tool_id: str, output: Any) -> None:
        part = self._tools.get(tool_id)
        if part is None:
            return
        part["state"] = "output-available"
        part["output"] = output

    def snapshot(self) -> list[dict]:
        return [dict(p) for p in self.parts]


class StrandsToAISDK:
    """Stateful transformer: feed Strands events, get AI SDK SSE frames.

    Usage::

        tx = StrandsToAISDK()
        async for frame in tx.run(supervisor.stream(task=...)):
            yield frame   # already an SSE 'data: ...' string
    """

    def __init__(self, message_id: str | None = None) -> None:
        # Single-message state -------------------------------------------
        # We emit ONE assistant UIMessage (one start … one finish). The frontend
        # renders parts linearly using a `currentAgent` cursor driven by
        # `data-agent-marker` frames (emitted only on owner switch).
        self._started: bool = False
        self._message_id: str = message_id or _new_id()
        # Active text block id (within the single message).
        self.current_text_id: str = ""
        # Current output owner — used to emit `data-agent-marker` only on change.
        self._current_owner: str = ""

        # Supervisor tool plumbing (we suppress framework tools entirely).
        self._suppressed_tool_ids: set[str] = set()
        # tool_id -> tool_name (for non-hidden supervisor tools, to track title).
        self._tool_names: dict[str, str] = {}
        # tool_ids for which we already emitted tool-input-delta (avoid duplicates).
        self._input_sent: set[str] = set()
        # Per sub-agent-run accumulators, keyed by segment key (node#n).
        self._subagents: dict[str, _SubAgentAccumulator] = {}
        # node -> current run index (incremented when a sub-agent run (re)starts).
        self._run_index: dict[str, int] = {}
        # node -> the active segment key for that node's current run.
        self._active_seg: dict[str, str] = {}
        # Whether a "planning" data-stage is currently active (emitted after
        # a sub-agent completes; dismissed when supervisor takes next action).
        self._planning_active: bool = False


    # -- public driver ------------------------------------------------------
    async def run(self, event_stream: AsyncIterable[dict]):
        """Yield SSE frames for the whole stream (segmented messages … [DONE])."""
        import asyncio

        HEARTBEAT_INTERVAL = 15  # seconds — keep connection alive during model thinking
        try:
            aiter = event_stream.__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(aiter.__anext__(), timeout=HEARTBEAT_INTERVAL)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.info("[streaming] heartbeat — no event for %ds, keeping connection alive", HEARTBEAT_INTERVAL)
                    yield ":heartbeat\n\n"
                    continue
                for frame in self._process(event):
                    yield frame
        except Exception as e:  # noqa: BLE001 - surface as an error frame, never crash
            logger.exception("[streaming] event stream failed: %s", e)
            for f in self._ensure_owner("supervisor"):
                yield f
            yield _sse({"type": "error", "errorText": str(e)})

        # Close any dangling message.
        for f in self._close_open():
            yield f
        yield _done()

    # -- single message lifecycle ------------------------------------------
    def _close_open(self):
        """Finish the single message (if started), flushing any open text block."""
        if not self._started:
            return
        if self.current_text_id:
            yield _sse({"type": "text-end", "id": self.current_text_id})
            self.current_text_id = ""
        yield _sse({"type": "finish"})

    def _dismiss_planning(self):
        """If a planning marker is active, emit planning done to clear it."""
        if self._planning_active:
            self._planning_active = False
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": "planning", "status": "done",
                                 "agent": "supervisor"}})

    def _ensure_owner(self, owner: str):
        """Ensure the single assistant message is started and emit a
        ``data-agent-marker`` frame whenever the output owner changes.

        The marker tells the frontend to switch the `currentAgent` cursor so
        subsequent text/tool bubbles are attributed to the new owner.
        """
        if not self._started:
            self._started = True
            yield _sse({"type": "start", "messageId": self._message_id})
        # Close any open text block when switching away from supervisor.
        if owner != self._current_owner and self.current_text_id:
            yield _sse({"type": "text-end", "id": self.current_text_id})
            self.current_text_id = ""
        # Emit marker only on owner change.
        if owner != self._current_owner:
            # Resolve the display agent name: segment keys like "solver#1" -> "solver"
            agent_name = owner.split("#")[0] if "#" in owner else owner
            stage = _STAGE_OF.get(agent_name, agent_name)
            self._current_owner = owner
            yield _sse({"type": "data-agent-marker", "id": _new_id(),
                        "data": {"agent": agent_name, "stage": stage}})



    # -- per-event ----------------------------------------------------------
    def _process(self, event: Any):
        if isinstance(event, str):
            event = {"data": event}
        if not isinstance(event, dict):
            return

        # Domain events injected by the Supervisor: {"mm": {...}}.
        mm = event.get("mm")
        if isinstance(mm, dict):
            yield from self._process_mm(mm)
            return

        inner = event.get("event", {}) if isinstance(event.get("event"), dict) else {}

        # Sub-agent stream events bubbled via ToolStreamEvent.
        if "tool_stream_event" in event:
            yield from self._process_tool_stream(event["tool_stream_event"])

        # Tool interrupt (ask_user) -> data-ask.
        if "tool_interrupt_event" in event:
            yield from self._process_tool_interrupt(event["tool_interrupt_event"])

        # contentBlockStart carrying a toolUse -> suppress framework tools only.
        # Dismiss planning indicator when supervisor takes next action.
        if inner.get("contentBlockStart") or event.get("data"):
            yield from self._dismiss_planning()
        cbs = inner.get("contentBlockStart")
        if cbs:
            start = cbs.get("start", {})
            tu = start.get("toolUse") if isinstance(start, dict) else None
            if tu:
                tool_id = tu.get("toolUseId")
                tool_name = tu.get("name", "tool")
                if tool_id and tool_name in _HIDDEN_TOOLS:
                    # Hidden tools: suppress entirely (no card).
                    self._suppressed_tool_ids.add(tool_id)
                elif tool_id:
                    # Non-hidden supervisor tools (run_subagent, shell, etc.):
                    # emit a tool-input-start frame so the frontend shows a card.
                    yield from self._ensure_owner("supervisor")
                    yield _sse({"type": "tool-input-start",
                                "toolCallId": tool_id, "toolName": tool_name})
                    # Track tool_id -> name for description extraction later.
                    self._tool_names[tool_id] = tool_name

        # Supervisor narration text -> supervisor message text-* parts.
        data = event.get("data")
        if data:
            yield from self._ensure_owner("supervisor")
            cycle = str(event.get("event_loop_cycle_id") or self.current_text_id or _new_id())
            if cycle != self.current_text_id:
                if self.current_text_id:
                    yield _sse({"type": "text-end", "id": self.current_text_id})
                self.current_text_id = cycle
                yield _sse({"type": "text-start", "id": cycle})
            yield _sse({"type": "text-delta", "id": self.current_text_id, "delta": data})

        # Supervisor tool input streaming -> suppress framework tools (no cards).
        ctu = event.get("current_tool_use")
        if ctu and ctu.get("name"):
            tool_id = ctu.get("toolUseId")
            tool_name = ctu.get("name", "tool")
            if tool_id and tool_name in _HIDDEN_TOOLS:
                self._suppressed_tool_ids.add(tool_id)
            # Intercept update_task: when the input JSON is valid, emit data-task.
            if tool_name == "update_task":
                raw_input = ctu.get("input")
                parsed = None
                if isinstance(raw_input, dict):
                    parsed = raw_input
                elif isinstance(raw_input, str):
                    try:
                        parsed = json.loads(raw_input)
                    except Exception:  # noqa: BLE001 - partial JSON, not ready yet
                        pass
                if parsed and isinstance(parsed, dict) and "tasks" in parsed:
                    yield from self._ensure_owner("supervisor")
                    yield _sse({"type": "data-task", "id": _new_id(),
                                "data": {"tasks": parsed["tasks"]}})
            # For non-hidden supervisor tools: emit tool-input-delta ONCE when
            # the input JSON is fully parseable so the frontend can show params
            # immediately (not just after tool output arrives).
            elif tool_id and tool_id in self._tool_names and tool_id not in self._input_sent:
                raw_input = ctu.get("input")
                input_json_str = None
                if isinstance(raw_input, dict):
                    input_json_str = json.dumps(raw_input, ensure_ascii=False)
                elif isinstance(raw_input, str):
                    try:
                        json.loads(raw_input)  # validate it's complete JSON
                        input_json_str = raw_input
                    except Exception:  # noqa: BLE001 - partial JSON
                        pass
                if input_json_str is not None:
                    self._input_sent.add(tool_id)
                    yield from self._ensure_owner("supervisor")
                    yield _sse({"type": "tool-input-delta",
                                "toolCallId": tool_id,
                                "inputTextDelta": input_json_str})

        # Supervisor tool results -> suppress framework tool results.
        # Also intercept update_task toolResult to emit data-task (more reliable
        # than the partial current_tool_use input, which may parse incompletely).
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            for blk in message.get("content", []) or []:
                tr = blk.get("toolResult") if isinstance(blk, dict) else None
                if not tr:
                    continue
                tool_use_id = tr.get("toolUseId")
                is_suppressed = tool_use_id in self._suppressed_tool_ids
                # Check ALL tool results for update_task content (emit data-task).
                # This is more robust than relying only on _suppressed_tool_ids
                # which may miss subsequent calls due to event ordering.
                texts = [b.get("text") for b in tr.get("content", []) or []
                         if isinstance(b, dict) and b.get("text")]
                for txt in texts:
                    try:
                        obj = json.loads(txt)
                        if isinstance(obj, dict) and "tasks" in obj and obj["tasks"]:
                            yield from self._ensure_owner("supervisor")
                            yield _sse({"type": "data-task", "id": _new_id(),
                                        "data": {"tasks": obj["tasks"]}})
                    except Exception:  # noqa: BLE001
                        pass
                if is_suppressed:
                    continue  # framework tool result: never surfaced as tool card
                # Also suppress any tool result for which we never emitted a
                # tool-input-start (happens on HITL resume — the contentBlockStart
                # was in the previous HTTP session so the ID isn't tracked).
                if tool_use_id and tool_use_id not in self._tool_names:
                    continue
                # Non-suppressed supervisor tool: emit tool-output-available.
                if tool_use_id:
                    output_text = "\n".join(texts) if texts else "done"
                    yield from self._ensure_owner("supervisor")
                    yield _sse({"type": "tool-output-available",
                                "toolCallId": tool_use_id, "output": output_text})

    # -- domain (mm) frames -------------------------------------------------
    def _process_mm(self, mm: dict):
        kind = mm.get("type")
        if kind == "stage":
            # Right-rail timeline marker; attach to whatever message is open
            # (open a supervisor message if none yet) — the rail scans all parts.
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "final":
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-final", "id": _new_id(),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "ask":
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-ask", "id": mm.get("interruptId", _new_id()),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "error":
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "error", "errorText": mm.get("message", "error")})
        elif kind == "agent":
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-agent", "id": mm.get("agent", _new_id()),
                        "data": {k: v for k, v in mm.items() if k != "type"}})

    # -- sub-agent stream ---------------------------------------------------
    def _seg_key(self, node: str) -> str:
        return f"{node}#{self._run_index.get(node, 1)}"

    def _emit_agent(self, seg_key: str, acc: _SubAgentAccumulator) -> str:
        stage = _STAGE_OF.get(acc.node, acc.node)
        return _sse({
            "type": "data-agent",
            "id": seg_key,                # stable id for THIS run's bubble
            "data": {
                "id": seg_key,
                "agent": acc.node,
                "name": acc.node,
                "stage": stage,
                "parts": acc.snapshot(),
            },
        })

    def _process_tool_stream(self, tse: dict):
        """ToolStreamEvent: fold one sub-agent's live events into its own message."""
        if not isinstance(tse, dict):
            return
        tool_use = tse.get("tool_use", {}) or {}
        agent_name = (tool_use.get("input", {}) or {}).get("name") \
            or tool_use.get("name", "subagent")
        sub = tse.get("data")
        if not isinstance(sub, dict):
            return
        # Heartbeat from execute_code polling — emit SSE comment to keep connection alive
        if sub.get("heartbeat"):
            yield ":heartbeat\n\n"
            return
        node = sub.get("node") or agent_name

        # Stage start/done markers -> data-stage (right rail). "start" also marks
        # the (re)start of a sub-agent run -> new bubble (new run index/segment).
        if "stage_status" in sub:
            status = sub["stage_status"]
            stage = _STAGE_OF.get(node, node)
            if status == "start":
                self._run_index[node] = self._run_index.get(node, 0) + 1
                seg = self._seg_key(node)
                self._active_seg[node] = seg
                self._subagents[seg] = _SubAgentAccumulator(node)
            seg = self._active_seg.get(node) or self._seg_key(node)
            yield from self._ensure_owner(seg)
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": stage, "status": status, "agent": node}})
            return

        seg = self._active_seg.get(node)
        if seg is None:
            # No explicit start seen; synthesize a run.
            self._run_index[node] = self._run_index.get(node, 0) + 1
            seg = self._seg_key(node)
            self._active_seg[node] = seg
            self._subagents[seg] = _SubAgentAccumulator(node)
        acc = self._subagents.get(seg)
        if acc is None:
            acc = _SubAgentAccumulator(node)
            self._subagents[seg] = acc

        changed = False

        # 1) A nested tool call by the sub-agent -> one tool card (skip ask_user).
        ctu = sub.get("current_tool_use")
        if ctu and ctu.get("name"):
            tname = ctu.get("name")
            if tname not in _HIDDEN_TOOLS:
                tid = ctu.get("toolUseId") or f"{seg}:{tname}"
                raw_input = ctu.get("input")
                tool_input = None
                if isinstance(raw_input, dict):
                    tool_input = raw_input
                elif isinstance(raw_input, str):
                    try:
                        tool_input = json.loads(raw_input)
                    except Exception:  # noqa: BLE001 - partial JSON, skip
                        pass
                # Create tool card skeleton immediately on first sight (even
                # before input is fully parsed) so the frontend shows the card
                # in "Running" state while the LLM streams the arguments.
                if tid not in acc._tools:
                    acc.tool_input(tid, tname, tool_input if tool_input is not None else {})
                    changed = True
                elif tool_input is not None:
                    # Update with complete parsed input once available.
                    acc.tool_input(tid, tname, tool_input)
                    changed = True

        # 2) The sub-agent emitted a result for one of its tools (skip hidden ids).
        msg = sub.get("message")
        if isinstance(msg, dict) and msg.get("role") == "user":
            for blk in msg.get("content", []) or []:
                tr = blk.get("toolResult") if isinstance(blk, dict) else None
                if tr:
                    tid = tr.get("toolUseId")
                    if tid not in acc._tools:
                        continue  # result for a hidden/unknown tool: ignore
                    parts = [b.get("text") for b in tr.get("content", []) or []
                             if isinstance(b, dict) and b.get("text")]
                    out = "\n\n".join(parts) if parts else "DONE"
                    acc.tool_output(tid, out)
                    changed = True

        # 3) Token deltas (sub-agent narration) -> a text part.
        # However, if there's a currently-active tool (input-available but no
        # output yet), append to its output field so stdout_chunk from
        # execute_code renders inside the tool card, not as separate text.
        if "data" in sub and sub["data"]:
            # Find the last tool that's still running (no output yet).
            active_tool_id = None
            for tid, part in acc._tools.items():
                if part.get("state") == "input-available":
                    active_tool_id = tid
            if active_tool_id:
                part = acc._tools[active_tool_id]
                existing = part.get("output") or ""
                part["output"] = existing + sub["data"]
            else:
                acc.add_text(sub["data"])
            changed = True

        # 4) Final result text -> stage done + the result as a text part.
        if "result_text" in sub:
            stage = _STAGE_OF.get(node, node)
            yield from self._ensure_owner(seg)
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": stage, "status": "done", "agent": node}})
            rt = sub.get("result_text") or ""
            if rt:
                acc.close_text()
                acc.add_text(rt)
                acc.close_text()
            changed = True
            # After a sub-agent completes, emit a "planning" marker so the
            # frontend knows the supervisor is thinking about the next step
            # (Opus TTFT can be 10-30s). This keeps the UI from appearing frozen.
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": "planning", "status": "start",
                                 "agent": "supervisor"}})
            self._planning_active = True

        if changed:
            yield from self._ensure_owner(seg)
            yield self._emit_agent(seg, acc)

    def _process_tool_interrupt(self, tie: dict):
        """ToolInterruptEvent: surface as data-ask (one per interrupt)."""
        if not isinstance(tie, dict):
            return
        for itr in tie.get("interrupts", []) or []:
            iid = getattr(itr, "id", None)
            reason = getattr(itr, "reason", None) or {}
            if isinstance(reason, dict):
                question = reason.get("question", "")
                agent = reason.get("agent", "supervisor")
            else:
                question, agent = str(reason), "supervisor"
            yield from self._ensure_owner("supervisor")
            yield _sse({"type": "data-ask", "id": iid or _new_id(),
                        "data": {"interruptId": iid, "question": question, "agent": agent}})
