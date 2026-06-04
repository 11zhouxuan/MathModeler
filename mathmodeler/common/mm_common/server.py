"""mm_common.server — shared FastAPI skeleton (tech-design §2.9).

Every agent container exposes the AgentCore Runtime contract:
  * ``GET  /ping``         -> health check ``{"status": "healthy"}``
  * ``POST /invocations``  -> business entry; JSON by default, or a
    ``text/event-stream`` SSE response when the client sends
    ``Accept: text/event-stream`` *and* a ``stream_handler`` is provided.

Run with: ``uvicorn app:app --host 0.0.0.0 --port 8080``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
        if "text/event-stream" in accept and stream_handler is not None:
            return StreamingResponse(
                stream_handler(body), media_type="text/event-stream"
            )
        return JSONResponse(handler(body))

    return app
