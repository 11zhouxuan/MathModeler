"""mm_common.streaming — Strands events → AI SDK v6 UI-message-stream SSE frames.

A focused port of agent-craft's ``StrandsEventStreamToSSEStreamTransformer``
(``shared/streaming/stream_protocol_v5.py``) adapted to MathModeler's needs.

The Orchestrator drives a :class:`mm_common.supervisor.Supervisor` whose
``stream()`` yields raw Strands ``stream_async`` event dicts. This transformer
converts each event into the AI SDK v6 wire format the portal/frontend expects:

  * ``event["data"]``                 -> ``text-start`` / ``text-delta`` / ``text-end``
                                         (keyed by ``event_loop_cycle_id``)
  * ``event["current_tool_use"]``     -> ``tool-input-start`` / ``tool-input-delta``
  * ``contentBlockStart`` (toolUse)   -> ``tool-input-start``
  * ``message`` (user/toolResult)     -> ``tool-output-available``
  * ``event["tool_stream_event"]``    -> custom ``data-agent`` (sub-agent live progress)
  * ``event["tool_interrupt_event"]`` -> custom ``data-ask`` (HITL question)
  * domain events injected by the Supervisor (``{"mm": {...}}``) -> ``data-stage`` /
    ``data-final`` / ``data-ask`` / ``data-error`` (the four-stage protocol)

Each frame is a single ``data: <json>\\n\\n`` line; the stream begins with
``start`` and ends with ``finish`` + ``data: [DONE]``.

No pydantic models are required — we emit plain dicts as JSON to keep the
Orchestrator image lean and avoid importing agent-craft's heavy schema module.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterable


# Map sub-agent name -> the four-stage SSE label used by the portal/frontend.
_STAGE_OF = {
    "analyst": "analysis",
    "modeler": "modeling",
    "solver": "solving",
    "reporter": "report",
}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"



def _done() -> str:
    return "data: [DONE]\n\n"


def _new_id() -> str:
    return uuid.uuid4().hex


class StrandsToAISDK:
    """Stateful transformer: feed Strands events, get AI SDK v6 SSE frames.

    Usage::

        tx = StrandsToAISDK()
        async for frame in tx.run(supervisor.stream(task=...)):
            yield frame   # already an SSE 'data: ...' string
    """

    def __init__(self, message_id: str | None = None) -> None:
        self.msg_id = message_id or _new_id()
        self.current_text_id: str = ""
        self._sent_tool_input_start: set[str] = set()
        self._block_kind: dict[int, str] = {}
        self._tool_by_index: dict[int, dict] = {}

    # -- public driver ------------------------------------------------------
    async def run(self, event_stream: AsyncIterable[dict]):
        """Yield SSE frames for the whole stream (start … finish … [DONE])."""
        yield _sse({"type": "start", "messageId": self.msg_id})
        try:
            async for event in event_stream:
                for frame in self._process(event):
                    yield frame
        except Exception as e:  # noqa: BLE001 - surface as an error frame, never crash the SSE
            yield _sse({"type": "error", "errorText": str(e)})
        # close any dangling text block
        if self.current_text_id:
            yield _sse({"type": "text-end", "id": self.current_text_id})
            self.current_text_id = ""
        yield _sse({"type": "finish"})
        yield _done()

    # -- per-event ----------------------------------------------------------
    def _process(self, event: Any):
        if isinstance(event, str):
            event = {"data": event}
        if not isinstance(event, dict):
            return

        # Domain events injected by the Supervisor: {"mm": {...}} -> data-* frames.
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

        # contentBlockStart carrying a toolUse -> tool-input-start.
        cbs = inner.get("contentBlockStart")
        if cbs:
            idx = cbs.get("contentBlockIndex")
            start = cbs.get("start", {})
            tu = start.get("toolUse") if isinstance(start, dict) else None
            if tu:
                tool_id = tu.get("toolUseId")
                tool_name = tu.get("name", "tool")
                self._tool_by_index[idx] = {"toolUseId": tool_id, "name": tool_name}
                if tool_id and tool_id not in self._sent_tool_input_start:
                    self._sent_tool_input_start.add(tool_id)
                    yield _sse({"type": "tool-input-start", "toolCallId": tool_id,
                                "toolName": tool_name})

        # track block kind on delta
        cbd = inner.get("contentBlockDelta")
        if cbd:
            idx = cbd.get("contentBlockIndex")
            delta = cbd.get("delta", {})
            if "text" in delta:
                self._block_kind[idx] = "text"
            elif "toolUse" in delta:
                self._block_kind[idx] = "toolUse"

        # text deltas (top-level data) -> text-start/delta keyed by cycle id
        data = event.get("data")
        if data:
            cycle = str(event.get("event_loop_cycle_id") or self.current_text_id or _new_id())
            if cycle != self.current_text_id:
                if self.current_text_id:
                    yield _sse({"type": "text-end", "id": self.current_text_id})
                self.current_text_id = cycle
                yield _sse({"type": "text-start", "id": cycle})
            yield _sse({"type": "text-delta", "id": self.current_text_id, "delta": data})

        # tool input streaming
        ctu = event.get("current_tool_use")
        if ctu and ctu.get("name"):
            tool_id = ctu.get("toolUseId")
            tool_name = ctu.get("name", "tool")
            if tool_id and tool_id not in self._sent_tool_input_start:
                self._sent_tool_input_start.add(tool_id)
                yield _sse({"type": "tool-input-start", "toolCallId": tool_id,
                            "toolName": tool_name})
            delta = event.get("delta", {})
            tud = delta.get("toolUse", {}) if isinstance(delta, dict) else {}
            args = tud.get("input")
            if args:
                yield _sse({"type": "tool-input-delta", "toolCallId": tool_id,
                            "inputTextDelta": args})

        # tool results carried on user message -> tool-output-available
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            for blk in message.get("content", []) or []:
                tr = blk.get("toolResult") if isinstance(blk, dict) else None
                if tr:
                    tool_id = tr.get("toolUseId")
                    parts = [b.get("text") for b in tr.get("content", []) or []
                             if isinstance(b, dict) and b.get("text")]
                    out = "\n\n".join(parts) if parts else "DONE"
                    yield _sse({"type": "tool-output-available", "toolCallId": tool_id,
                                "output": out})

    # -- helpers ------------------------------------------------------------
    def _process_mm(self, mm: dict):
        """Domain frames emitted by the Supervisor's stream() (four-stage protocol)."""
        kind = mm.get("type")
        if kind == "stage":
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "final":
            yield _sse({"type": "data-final", "id": _new_id(),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "ask":
            yield _sse({"type": "data-ask", "id": mm.get("interruptId", _new_id()),
                        "data": {k: v for k, v in mm.items() if k != "type"}})
        elif kind == "error":
            yield _sse({"type": "error", "errorText": mm.get("message", "error")})
        elif kind == "agent":
            yield _sse({"type": "data-agent", "id": mm.get("agent", _new_id()),
                        "data": {k: v for k, v in mm.items() if k != "type"}})

    def _process_tool_stream(self, tse: dict):
        """ToolStreamEvent: {"tool_use": {...}, "data": <yielded sub-event>}."""
        if not isinstance(tse, dict):
            return
        tool_use = tse.get("tool_use", {}) or {}
        agent_name = (tool_use.get("input", {}) or {}).get("name") \
            or tool_use.get("name", "subagent")
        sub = tse.get("data")
        if not isinstance(sub, dict):
            return
        # Supervisor wraps child events as {"node": <agent>, ...}.
        node = sub.get("node") or agent_name

        # Stage start/done markers -> four-stage data-stage frames.
        if "stage_status" in sub:
            stage = _STAGE_OF.get(node, node)
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": stage, "status": sub["stage_status"],
                                 "agent": node}})
            return

        chunk: Any = None
        if "data" in sub and sub["data"]:
            chunk = {"kind": "token", "delta": sub["data"]}
        elif "result_text" in sub:
            # Sub-agent finished -> data-stage done + final result text via data-agent.
            stage = _STAGE_OF.get(node, node)
            yield _sse({"type": "data-stage", "id": _new_id(),
                        "data": {"stage": stage, "status": "done", "agent": node}})
            chunk = {"kind": "result", "text": sub["result_text"]}
        elif sub.get("current_tool_use") and sub["current_tool_use"].get("name"):
            chunk = {"kind": "tool", "tool": sub["current_tool_use"].get("name")}

        if chunk is not None:
            yield _sse({"type": "data-agent", "id": str(node),
                        "data": {"agent": node, "chunk": chunk}})


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
            yield _sse({"type": "data-ask", "id": iid or _new_id(),
                        "data": {"interruptId": iid, "question": question, "agent": agent}})
