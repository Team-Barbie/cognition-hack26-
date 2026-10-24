"""Local web server for the research studio. Binds to 127.0.0.1 only; stdlib, no extra dependencies."""

from __future__ import annotations

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
            raise KeyError("unknown session; reload the page")
        return session


def make_handler(studio: Studio) -> type[BaseHTTPRequestHandler]:
    page = files("ar.studio").joinpath("app.html").read_text(encoding="utf-8")

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

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request is too large (1 MB max)")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if url.path == "/api/info":
                self._json({"mode": studio.mode})
                return
            if url.path == "/api/events":
                q = parse_qs(url.query)
                try:
                    session = studio.get(q.get("id", [""])[0])
                    after = max(0, int(q.get("after", ["0"])[0]))
                except (KeyError, ValueError) as exc:
                    self._json({"error": str(exc).strip("'")}, 400)
                    return
                self._json({"events": session.since(after), "status": session.status})
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                data = self._body()
                if path == "/api/session":
                    s = studio.new_session()
                    self._json({"id": s.id, "mode": studio.mode})
                    return
                session = studio.get(str(data.get("id", "")))
                if path == "/api/chat":
                    session.chat(str(data.get("message", ""))[:8000])
                elif path == "/api/source":
                    session.add_source(str(data.get("name", "source"))[:120], str(data.get("text", "")))
                elif path == "/api/demo":
                    session.use_demo()
                elif path == "/api/run":
                    session.start()
                else:
                    self._json({"error": "not found"}, 404)
                    return
                self._json({"ok": True})
            except KeyError as exc:
                self._json({"error": str(exc).strip("'\"")}, HTTPStatus.BAD_REQUEST)
            except (ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve(port: int = 8765, *, provider: str | None = None, model: str | None = None, fake: bool = False,
          workdir: Path = DEFAULT_WORKDIR, env_files: tuple[Path, ...] = ()) -> ThreadingHTTPServer:
    load_env(*env_files)
    studio = Studio(workdir, provider, model, fake)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(studio))
    httpd.studio = studio  # type: ignore[attr-defined]
    return httpd
