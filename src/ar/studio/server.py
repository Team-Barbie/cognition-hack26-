"""Local web server for the research studio. Binds to 127.0.0.1 only; stdlib, no extra dependencies."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
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
        self.provider = provider
        self.model = model

    @property
    def mode(self) -> str:
        return f"live · {self.provider}" if self.provider else "demo · fake agent"

    def _client(self):
        return make_client(self.provider, self.model) if self.provider else None

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


def handle(studio: Studio, method: str, path: str, query: str, raw_body: bytes,
           authorization: str = "") -> tuple[int, str, bytes]:
    """One router for the local server and the Vercel app. Returns status, content type, body.

    With STUDIO_PASSWORD set, the page itself is public (it holds the lock screen) and every
    /api/ route answers 401 until the request carries the password.
    """
    if path.startswith("/api/") and not _authorized(authorization):
        return _json({"error": "password required"}, HTTPStatus.UNAUTHORIZED)
    if method == "GET":
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
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0) if method == "POST" else 0
            if length > MAX_BODY:
                status, kind, payload = _json({"error": "request is too large (1 MB max)"}, 400)
            else:
                raw = self.rfile.read(length) if method == "POST" else b""
                status, kind, payload = handle(studio, method, url.path, url.query, raw,
                                               self.headers.get("Authorization", ""))
            self._send(status, payload, kind)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def serve(port: int = 8765, *, provider: str | None = None, model: str | None = None, fake: bool = False,
          workdir: Path = DEFAULT_WORKDIR, env_files: tuple[Path, ...] = ()) -> ThreadingHTTPServer:
    load_env(*env_files)
    studio = Studio(workdir, provider, model, fake)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(studio))
    httpd.studio = studio  # type: ignore[attr-defined]
    return httpd
