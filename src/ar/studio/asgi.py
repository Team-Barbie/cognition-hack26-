"""ASGI entry for Vercel. Same routes as the local studio server.

Sessions stay in this function instance. A run started on Vercel is awaited before
`/api/run` returns, so the work finishes inside the request instead of a daemon thread
the platform would freeze.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ar.studio.server import event_stream, handle, prepare_event_stream, vercel_studio

app = FastAPI()


async def _finish_run(body: bytes) -> bytes | None:
    """Wait for the run, then hand back everything it emitted. A later poll may reach another
    instance that has no copy of this session, so the page gets the events from this response."""
    try:
        sid = str(json.loads(body or b"{}").get("id", ""))
        session = vercel_studio().get(sid)
    except (KeyError, ValueError):
        return None
    await asyncio.to_thread(session.wait, 280)
    return json.dumps({"ok": True, "events": session.since(0), "status": session.status}).encode("utf-8")


@app.api_route("/", methods=["GET", "POST"])
@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def studio(request: Request) -> Response:
    if request.method == "GET" and request.url.path == "/api/events/stream":
        session, after, error = prepare_event_stream(
            vercel_studio(), request.url.query, request.headers.get("authorization", "")
        )
        if error:
            status, kind, payload = error
            return Response(content=payload, status_code=status, media_type=kind)
        return StreamingResponse(
            event_stream(session, after),  # type: ignore[arg-type]
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )
    body = await request.body()
    status, kind, payload = handle(vercel_studio(), request.method, request.url.path, request.url.query, body,
                                      request.headers.get("authorization", ""))
    if os.environ.get("VERCEL") == "1" and request.method == "POST" and request.url.path == "/api/run" and status == 200:
        payload = await _finish_run(body) or payload
    return Response(
        content=payload,
        status_code=status,
        headers={
            "Content-Type": kind,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
