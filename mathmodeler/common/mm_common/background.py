"""mm_common.background — Background task execution with single-subscriber SSE.

Decouples supervisor execution from the HTTP/SSE connection lifecycle:
- Supervisor runs in a background asyncio.Task independent of any request
- Events are buffered in memory; one SSE consumer can subscribe at a time
- Disconnect does NOT stop execution; only explicit cancel does
- Reconnect gets full replay from buffer start, then live tail
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator

logger = logging.getLogger("mm.background")

_TASKS: dict[str, "SessionTask"] = {}


class SessionTask:
    """A background supervisor execution for one session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.events: list[str] = []
        self.done = asyncio.Event()
        self.started_at: float = time.time()
        self._task: asyncio.Task | None = None
        self._notify: asyncio.Event = asyncio.Event()
        self._has_subscriber: bool = False

    def _notify_subscriber(self):
        self._notify.set()

    def append(self, frame: str):
        self.events.append(frame)
        self._notify_subscriber()

    def mark_done(self):
        self.done.set()
        self._notify_subscriber()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def try_subscribe(self) -> bool:
        if self._has_subscriber:
            return False
        self._has_subscriber = True
        return True

    def unsubscribe(self):
        self._has_subscriber = False

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """Yield SSE frames: full replay from start, then live tail with heartbeat."""
        logger.info("[background] subscriber attached session=%s (buffer=%d events)",
                    self.session_id, len(self.events))
        try:
            cursor = 0
            while True:
                while cursor < len(self.events):
                    yield self.events[cursor]
                    cursor += 1
                if self.done.is_set():
                    break
                self._notify.clear()
                try:
                    await asyncio.wait_for(self._notify.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ":heartbeat\n\n"
        finally:
            self.unsubscribe()
            logger.info("[background] subscriber detached session=%s", self.session_id)


def get_task(session_id: str) -> SessionTask | None:
    """Get an existing task (running or completed with buffer) for this session."""
    return _TASKS.get(session_id)


def create_task(session_id: str, coro_factory) -> SessionTask:
    """Create and start a new background task for this session.

    coro_factory is a callable(SessionTask) -> coroutine. It receives the
    SessionTask so it can call st.append(frame) to buffer events.
    """
    old = _TASKS.pop(session_id, None)
    if old and old.is_running:
        old._task.cancel()

    st = SessionTask(session_id)
    _TASKS[session_id] = st

    async def _wrapper():
        try:
            await coro_factory(st)
        except asyncio.CancelledError:
            logger.info("[background] task CANCELLED session=%s", session_id)
            st.append("data: {\"type\":\"error\",\"errorText\":\"Task cancelled\"}\n\n")
        except Exception as e:
            logger.exception("[background] task FAILED session=%s: %s", session_id, e)
            st.append(f"data: {{\"type\":\"error\",\"errorText\":\"{e!s}\"}}\n\n")
        finally:
            st.mark_done()
            logger.info("[background] task ENDED session=%s events=%d elapsed=%.1fs",
                        session_id, len(st.events), time.time() - st.started_at)

    st._task = asyncio.get_event_loop().create_task(_wrapper())
    return st


def cancel_task(session_id: str) -> bool:
    """Cancel a running background task."""
    task = _TASKS.get(session_id)
    if task is None or not task.is_running:
        return False
    task._task.cancel()
    return True


def cleanup_task(session_id: str) -> None:
    """Remove a completed task from the registry (free memory)."""
    task = _TASKS.get(session_id)
    if task and task.done.is_set() and not task._has_subscriber:
        _TASKS.pop(session_id, None)
