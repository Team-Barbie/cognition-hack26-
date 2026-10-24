"""ASGI entry for Vercel. Same routes as the local studio server.

Sessions stay in this function instance. A run started on Vercel is awaited before
`/api/run` returns, so the work finishes inside the request instead of a daemon thread
the platform would freeze.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ar.studio.server import event_stream, handle, prepare_event_stream, vercel_studio

RUN_WINDOW = 280  # seconds a hosted request may stay open, just under the function limit

app = FastAPI()


async def _run_stream(session, after: int):
    """Stream the run's events as they happen, one JSON object per line, from the instance that runs it.

    A later poll or event stream may reach another instance with no copy of this session, so on a
    serverless host the request that does the work is the one place progress can safely come from."""
    cursor, deadline = after, time.monotonic() + RUN_WINDOW
    while True:
        batch = await asyncio.to_thread(session.wait_since, cursor, 2.0)
        if not session.running:  # the run ended: take whatever it wrote last
            batch = batch + [e for e in session.since(cursor) if e["seq"] > (batch[-1]["seq"] if batch else cursor)]
        for event in batch:
            cursor = int(event["seq"])
            yield (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        if not session.running or time.monotonic() > deadline:
            break
    yield (json.dumps({"done": True, "status": session.status}) + "\n").encode("utf-8")


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
        try:
            data = json.loads(body or b"{}")
            session = vercel_studio().get(str(data.get("id", "")))
            after = data["after"] if isinstance(data.get("after"), int) else 0
        except (KeyError, ValueError):
            session = None
        if session is not None:
            return StreamingResponse(
                _run_stream(session, max(0, after)),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
            )
    return Response(
        content=payload,
        status_code=status,
        headers={
            "Content-Type": kind,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
