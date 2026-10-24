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
from fastapi.responses import Response

from ar.studio.server import handle, vercel_studio

app = FastAPI()


async def _finish_run(body: bytes) -> None:
    try:
        sid = str(json.loads(body or b"{}").get("id", ""))
        session = vercel_studio().get(sid)
    except (KeyError, ValueError):
        return
    await asyncio.to_thread(session.wait, 280)


@app.api_route("/", methods=["GET", "POST"])
@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def studio(request: Request) -> Response:
    body = await request.body()
    status, kind, payload = handle(vercel_studio(), request.method, request.url.path, request.url.query, body,
                                      request.headers.get("authorization", ""))
    if os.environ.get("VERCEL") == "1" and request.method == "POST" and request.url.path == "/api/run" and status == 200:
        await _finish_run(body)
    return Response(
        content=payload,
        status_code=status,
        headers={
            "Content-Type": kind,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
