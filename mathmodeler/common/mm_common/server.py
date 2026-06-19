"""mm_common.server — shared FastAPI skeleton (tech-design §2.9).

Every agent container exposes the AgentCore Runtime contract:
  * ``GET  /ping``         -> health check ``{"status": "healthy"}``
  * ``POST /invocations``  -> business entry; JSON by default, or a
    ``text/event-stream`` SSE response when the client sends
    ``Accept: text/event-stream`` *and* a ``stream_handler`` is provided.

Run with: ``uvicorn app:app --host 0.0.0.0 --port 8080``.
"""
from __future__ import annotations

import json
import logging

from typing import Any, Awaitable, Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("mm.server")

Handler = Callable[[dict], dict]
StreamHandler = Callable[[dict], Iterable[str] | Awaitable[Any]]



def make_app(handler: Handler, stream_handler: StreamHandler | None = None) -> FastAPI:
    app = FastAPI(title="MathModeler Agent", version="0.1.0")

    @app.get("/ping")
    def ping() -> dict:
        return {"status": "healthy"}

    @app.post("/invocations")
    async def invocations(req: Request):
        body = await req.json()
        accept = req.headers.get("accept", "")
        # Log the FULL request body the agent backend receives (truncated) so we
        # can see exactly what reached the Runtime — e.g. whether ``problem`` is
        # actually populated when the portal forwards a chat turn.
        try:
            dumped = json.dumps(body, ensure_ascii=False)
            logger.info(
                "[invocations] accept=%r body=%s",
                accept, dumped if len(dumped) <= 2000 else dumped[:2000] + "…(truncated)",
            )
        except Exception:  # noqa: BLE001 - logging must never break the request
            logger.info("[invocations] accept=%r body_keys=%s", accept,
                        sorted(body.keys()) if isinstance(body, dict) else type(body))
        if "text/event-stream" in accept and stream_handler is not None:
            return StreamingResponse(
                stream_handler(body), media_type="text/event-stream"
            )

        return JSONResponse(handler(body))

    return app
