"""mm_common.events — in-process progress event bus (agents-as-tools §6.2).

When the five agents run inside a single Runtime (agents-as-tools), the four
sub-agents are no longer separate AgentCore Runtimes streaming their own SSE.
Instead, the in-process ``invoke_*`` orchestrator tools (and the deterministic
pipeline) publish stage/progress events to a process-local sink, which the
Orchestrator's ``stream_pipeline`` drains into a single SSE stream to the portal.

Design:
- A :class:`EventSink` collects ``dict`` events (the §5.4 four-stage protocol:
  ``{type:"stage"|"final"|"error", ...}``).
- The *current* sink is stored in a :class:`contextvars.ContextVar` so that tool
  functions (which receive no explicit emitter argument) can find it. This is
  safe for concurrent invocations because each request binds its own sink.
- :func:`emit` is a no-op when no sink is bound (e.g. unit tests calling tools
  directly, or the non-streaming ``run_pipeline`` path).

The sink is intentionally a simple synchronous list-backed collector: the
deterministic pipeline runs to completion building the event list and the
generator yields them (matching the existing ``stream_pipeline`` semantics),
while still allowing the agentic/supervisor path to publish events as tools fire.
"""
from __future__ import annotations

import contextvars
from typing import Callable, Optional

# The currently-bound sink for this logical request/context.
_current_sink: contextvars.ContextVar[Optional["EventSink"]] = contextvars.ContextVar(
    "mm_event_sink", default=None
)


class EventSink:
    """Collects progress events and optionally forwards them live.

    ``on_event`` (optional) is invoked synchronously for each published event,
    enabling a streaming consumer to forward events as they happen. All events
    are also retained in ``events`` for non-streaming aggregation.
    """

    def __init__(self, on_event: Optional[Callable[[dict], None]] = None):
        self.events: list[dict] = []
        self._on_event = on_event

    def publish(self, event: dict) -> None:
        self.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - a broken consumer must not abort the run
                pass


def bind_sink(sink: Optional[EventSink]) -> contextvars.Token:
    """Bind ``sink`` as the current sink; returns a token for :func:`reset_sink`."""
    return _current_sink.set(sink)


def reset_sink(token: contextvars.Token) -> None:
    """Restore the sink bound before the matching :func:`bind_sink`."""
    try:
        _current_sink.reset(token)
    except Exception:  # noqa: BLE001
        pass


def current_sink() -> Optional[EventSink]:
    return _current_sink.get()


def emit(event: dict) -> None:
    """Publish ``event`` to the current sink (no-op if none is bound)."""
    sink = _current_sink.get()
    if sink is not None:
        sink.publish(event)
