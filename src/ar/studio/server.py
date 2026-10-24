"""Local web server for the research studio. Binds to 127.0.0.1 only; stdlib, no extra dependencies."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ar.adapter import LiveAgent, load_env, make_client
from ar.studio.session import Session

MAX_BODY = 1_000_000
DEFAULT_WORKDIR = Path.home() / ".ar-studio" / "projects"
_PAGE: bytes | None = None
_VERCEL: Studio | None = None


class SlidingWindowLimiter:
    """Small, thread-safe, in-process limiter shared by the local and Vercel adapters."""

    def __init__(self, *, clock=time.monotonic, max_buckets: int = 10_000) -> None:
        self._clock = clock
        self._max_buckets = max_buckets
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, rules: list[tuple[str, str, int, float]]) -> int | None:
        """Consume one request from every rule, or return seconds until all rules allow it."""
        now = self._clock()
        with self._lock:
            requested = {(identity, bucket) for identity, bucket, _, _ in rules}
            missing = sum(key not in self._events for key in requested)
            overflow = len(self._events) + missing - self._max_buckets
            if overflow > 0:
                # Never evict a bucket involved in this decision; doing so could weaken one rule.
                victims = sorted(
                    (key for key in self._events if key not in requested),
                    key=lambda key: self._events[key][-1] if self._events[key] else float("-inf"),
                )
                shed = max(overflow, max(1, self._max_buckets // 10))
                for victim in victims[:shed]:
                    del self._events[victim]
            waits: list[float] = []
            queues: list[deque[float]] = []
            for identity, bucket, limit, window in rules:
                key = (identity, bucket)
                queue = self._events.get(key)
                if queue is None:
                    queue = self._events[key] = deque()
                cutoff = now - window
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                queues.append(queue)
                if len(queue) >= limit:
                    waits.append(queue[0] + window - now)
            if waits:
                return max(1, math.ceil(max(waits)))
            for queue in queues:
                queue.append(now)
            return None


_RATE_LIMITER = SlidingWindowLimiter()


class StudioHTTPServer(ThreadingHTTPServer):
    """Threaded server whose listening port belongs to exactly one Studio process.

    ``HTTPServer`` enables ``SO_REUSEADDR``.  On Windows that permits multiple
    live processes to bind the same address, after which requests can be split
    between unrelated in-memory session stores.  A second Studio should fail
    clearly at startup instead of presenting an intermittently broken page.
    """

    allow_reuse_address = False


def _rate_limit(client_id: str | None, path: str) -> tuple[int, str, bytes] | None:
    """Apply a broad API cap plus tighter caps around expensive or connection-heavy routes."""
    if not client_id or not path.startswith("/api/"):
        return None
    rules = [(client_id, "api", 120, 60.0)]
    if path == "/api/session":
        rules.append((client_id, "session", 12, 60.0))
    elif path == "/api/run":
        rules.append((client_id, "run", 6, 600.0))
    elif path == "/api/search":
        rules.append((client_id, "search", 20, 60.0))
    elif path == "/api/events/stream":
        rules.append((client_id, "stream", 20, 60.0))
    retry_after = _RATE_LIMITER.check(rules)
    if retry_after is None:
        return None
    return _json({"error": "rate limit exceeded", "retry_after": retry_after}, HTTPStatus.TOO_MANY_REQUESTS)


def retry_after_header(status: int, payload: bytes) -> str | None:
    """Extract a standard Retry-After header value from our 429 response."""
    if status != HTTPStatus.TOO_MANY_REQUESTS:
        return None
    try:
        return str(max(1, int(json.loads(payload).get("retry_after", 1))))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "1"


class Studio:
    """Sessions plus how to build the live model client, decided once from the environment."""

    def __init__(self, workdir: Path = DEFAULT_WORKDIR, provider: str | None = None, model: str | None = None,
                 fake: bool = False) -> None:
        self.workdir = workdir
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        if fake:
            provider = None
        elif provider is None:
            if os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
            elif os.environ.get("ANTHROPIC_API_KEY"):
                provider = "anthropic"
        if provider not in {None, "openai", "anthropic"}:
            raise ValueError("Research provider must be openai or anthropic; use the coding worker for Codex")
        self.provider = provider
        self.model = model

    @property
    def mode(self) -> str:
        return f"live · {self.provider}" if self.provider else "demo · fake agent"

    def _client(self):
        return make_client(self.provider, self.model) if self.provider in {"openai", "anthropic"} else None

    def new_session(self) -> Session:
        make_agent = (lambda: LiveAgent(self._client())) if self.provider else None
        session = Session(self.workdir, make_agent, client=self._client())
        with self._lock:
            self.sessions[session.id] = session
        return session

    def get(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None:
            raise KeyError("unknown session")
        return session


# The tab icon: the pixel invader from the UI, lime on the CRT black with its eyes in the accent colour.
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 13 13" shape-rendering="crispEdges"><rect width="13" height="13" rx="2.2" fill="#070a05"/><rect x=".5" y=".5" width="12" height="12" rx="1.8" fill="none" stroke="#2f4a12" stroke-width=".5"/><path d="M3 2h1v1h-1zM9 2h1v1h-1zM4 3h1v1h-1zM8 3h1v1h-1zM3 4h7v1h-7zM2 5h2v1h-2zM5 5h3v1h-3zM9 5h2v1h-2zM1 6h11v1h-11zM1 7h1v1h-1zM3 7h7v1h-7zM11 7h1v1h-1zM1 8h1v1h-1zM3 8h1v1h-1zM9 8h1v1h-1zM11 8h1v1h-1zM4 9h2v1h-2zM7 9h2v1h-2z" fill="#b9f04a"/><path d="M4 5h1v1h-1zM8 5h1v1h-1z" fill="#ff4fa3"/></svg>'
)


def studio_page() -> bytes:
    global _PAGE
    if _PAGE is None:
        _PAGE = files("ar.studio").joinpath("app.html").read_text(encoding="utf-8").encode("utf-8")
    return _PAGE


def _json(data: Any, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(data).encode("utf-8")


# The built-in Vercel password is kept only as a salted scrypt hash, so the source does not show it.
_SALT = b'res-loop-studio'
VERCEL_PASSWORD_HASH = "451819a7f6dd488280c1ed427ed1394de003fa3da33c24e16c3ae257f8201ffc"


def _scrypt(text: str) -> bytes:
    return hashlib.scrypt(text.encode("utf-8"), salt=_SALT, n=2**14, r=8, p=1, dklen=32)


def _authorized(authorization: str) -> bool:
    """True when no password applies, or the request carries `Authorization: Bearer <it>`.

    STUDIO_PASSWORD, if set, wins (empty turns the lock off). Otherwise Vercel uses the built-in hash.
    """
    given = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if "STUDIO_PASSWORD" in os.environ:
        password = os.environ["STUDIO_PASSWORD"]
        return not password or hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))
    if os.environ.get("VERCEL") == "1":
        return hmac.compare_digest(_scrypt(given), bytes.fromhex(VERCEL_PASSWORD_HASH))
    return True


def prepare_event_stream(studio: Studio, query: str, authorization: str = "",
                         client_id: str | None = None) -> tuple[Session | None, int, tuple[int, str, bytes] | None]:
    """Validate an SSE request and return its session and initial cursor."""
    limited = _rate_limit(client_id, "/api/events/stream")
    if limited:
        return None, 0, limited
    if not _authorized(authorization):
        return None, 0, _json({"error": "password required"}, HTTPStatus.UNAUTHORIZED)
    q = parse_qs(query)
    try:
        session = studio.get(q.get("id", [""])[0])
        after = max(0, int(q.get("after", ["0"])[0]))
    except (KeyError, ValueError) as exc:
        return None, 0, _json({"error": str(exc).strip("'")}, HTTPStatus.BAD_REQUEST)
    return session, after, None


def event_stream(session: Session, after: int):
    """Yield resumable SSE frames from the session's durable event journal."""
    cursor = after
    while True:
        events = session.wait_since(cursor)
        if not events:
            yield b": keep-alive\n\n"
            continue
        for event in events:
            cursor = int(event["seq"])
            payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            yield f"id: {cursor}\nevent: studio\ndata: {payload}\n\n".encode("utf-8")


def handle(studio: Studio, method: str, path: str, query: str, raw_body: bytes,
           authorization: str = "", client_id: str | None = None) -> tuple[int, str, bytes]:
    """One router for the local server and the Vercel app. Returns status, content type, body.

    With STUDIO_PASSWORD set, the page itself is public (it holds the lock screen) and every
    /api/ route answers 401 until the request carries the password.
    """
    limited = _rate_limit(client_id, path)
    if limited:
        return limited
    if path.startswith("/api/") and not _authorized(authorization):
        return _json({"error": "password required"}, HTTPStatus.UNAUTHORIZED)
    if method == "GET":
        if path in ("/favicon.svg", "/favicon.ico"):
            return 200, "image/svg+xml", FAVICON
        if path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", studio_page()
        if path == "/api/info":
            return _json({"mode": studio.mode})
        if path == "/api/events":
            q = parse_qs(query)
            try:
                session = studio.get(q.get("id", [""])[0])
                after = max(0, int(q.get("after", ["0"])[0]))
            except (KeyError, ValueError) as exc:
                return _json({"error": str(exc).strip("'")}, 400)
            return _json({"events": session.since(after), "status": session.status})
        return _json({"error": "not found"}, 404)

    if method != "POST":
        return _json({"error": "not found"}, 404)
    if len(raw_body) > MAX_BODY:
        return _json({"error": "request is too large (1 MB max)"}, 400)
    try:
        data = json.loads(raw_body or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        if path == "/api/session":
            created = studio.new_session()
            if isinstance(data.get("restore"), dict):
                created.restore(data["restore"])
            return _json({"id": created.id, "mode": studio.mode, "seen": len(created.events)})
        session = studio.get(str(data.get("id", "")))
        reply: dict[str, Any] = {"ok": True}
        if path == "/api/chat":
            session.chat(str(data.get("message", ""))[:8000])
        elif path == "/api/source":
            reply["id"] = session.add_source(str(data.get("name", "source"))[:120], str(data.get("text", "")))
        elif path == "/api/source/remove":
            session.remove_source(str(data.get("source", "")))
        elif path == "/api/demo":
            session.use_demo()
        elif path == "/api/search":
            reply["results"] = session.search_web(str(data.get("query", ""))[:500])
        elif path == "/api/literature":
            session.set_literature_reviews(data.get("reviews", []))
            reply["reviews"] = [spec.model_dump(mode="json") for spec in session.literature_reviews]
        elif path == "/api/run":
            session.start()
        else:
            return _json({"error": "not found"}, 404)
        if isinstance(data.get("after"), int):
            # A hosted instance may not be the one the next poll reaches, so the answer carries the news.
            reply["events"] = session.since(max(0, data["after"]))
        return _json(reply)
    except KeyError as exc:
        return _json({"error": str(exc).strip("'\"")}, HTTPStatus.BAD_REQUEST)
    except (ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except OSError as exc:
        return _json({"error": f"studio storage is unavailable: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def vercel_studio() -> Studio:
    """Process-wide studio. On Vercel the writable project dir is /tmp, and keys come from the environment."""
    global _VERCEL
    if _VERCEL is None:
        workdir = Path("/tmp/ar-studio") if os.environ.get("VERCEL") else DEFAULT_WORKDIR
        workdir.mkdir(parents=True, exist_ok=True)
        _VERCEL = Studio(workdir)
    return _VERCEL


def make_handler(studio: Studio) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ar-studio"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
            return

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            retry_after = retry_after_header(status, body)
            if retry_after:
                self.send_header("Retry-After", retry_after)
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and url.path == "/api/events/stream":
                session, after, error = prepare_event_stream(
                    studio, url.query, self.headers.get("Authorization", ""), self.client_address[0]
                )
                if error:
                    status, kind, payload = error
                    self._send(status, payload, kind)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    for frame in event_stream(session, after):  # type: ignore[arg-type]
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            length = int(self.headers.get("Content-Length") or 0) if method == "POST" else 0
            if length > MAX_BODY:
                status, kind, payload = _json({"error": "request is too large (1 MB max)"}, 400)
            else:
                raw = self.rfile.read(length) if method == "POST" else b""
                status, kind, payload = handle(studio, method, url.path, url.query, raw,
                                               self.headers.get("Authorization", ""), self.client_address[0])
            self._send(status, payload, kind)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def serve(port: int = 8765, *, provider: str | None = None, model: str | None = None, fake: bool = False,
          workdir: Path = DEFAULT_WORKDIR, env_files: tuple[Path, ...] = ()) -> StudioHTTPServer:
    load_env(*env_files)
    # Fail before opening a browser if sessions cannot be persisted.  Otherwise
    # the page loads normally and its first API request ends in an empty reply.
    workdir.mkdir(parents=True, exist_ok=True)
    studio = Studio(workdir, provider, model, fake)
    httpd = StudioHTTPServer(("127.0.0.1", port), make_handler(studio))
    httpd.studio = studio  # type: ignore[attr-defined]
    return httpd
