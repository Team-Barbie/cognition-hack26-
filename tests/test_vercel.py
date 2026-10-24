"""The Vercel app serves the same studio routes as the local server."""

import json

import pytest
from fastapi.testclient import TestClient

from ar.studio.asgi import app
from ar.studio import server
from ar.studio.server import SlidingWindowLimiter, Studio


@pytest.fixture(autouse=True)
def fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(server, "_RATE_LIMITER", SlidingWindowLimiter())
    monkeypatch.setattr("ar.studio.session.search_crossref", lambda query, limit: [])


def test_vercel_app_serves_the_studio_page_and_a_demo_session(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Res Loop</title>" in page.text
    assert "https://" not in page.text and "http://" not in page.text

    created = client.post("/api/session", json={})
    assert created.status_code == 200
    body = created.json()
    assert body["mode"] == "demo · fake agent"
    sid = body["id"]

    assert client.get("/api/info").json() == {"mode": "demo · fake agent"}
    assert client.post("/api/run", json={"id": sid}).status_code == 400
    assert client.post("/api/demo", json={"id": sid}).json() == {"ok": True}
    missing = client.get("/api/events", params={"id": "nope"})
    assert missing.status_code == 400
    assert client.get("/api/missing").status_code == 404

    events = client.get("/api/events", params={"id": sid, "after": 0}).json()
    assert events["status"] == "intake"
    assert any(event["type"] == "sources" for event in events["events"])


def test_vercel_run_waits_for_the_demo_before_returning(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("STUDIO_PASSWORD", "")  # this test is about the run, not the lock
    client = TestClient(app)

    sid = client.post("/api/session", json={}).json()["id"]
    assert client.post("/api/demo", json={"id": sid}).status_code == 200
    ran = client.post("/api/run", json={"id": sid, "after": 0})
    assert ran.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in ran.text.splitlines()]
    assert lines[-1] == {"done": True, "status": "done"}
    events = lines[:-1]  # streamed from the instance that runs it: a later poll may reach another instance
    assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})
    assert events == client.get("/api/events", params={"id": sid}).json()["events"][-len(events):]
    report = next(event["report"] for event in events if event["type"] == "report")
    assert report["outcome"] == "completed" and report["cited"] == ["claim-1", "claim-2"]
    assert json.dumps(report)


def test_studio_password_locks_the_api_but_not_the_page(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.setenv("STUDIO_PASSWORD", "open sesame")
    client = TestClient(app)

    assert client.get("/").status_code == 200  # the page holds the lock screen
    assert client.post("/api/session", json={}).status_code == 401
    assert client.get("/api/info").status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/api/session", json={}, headers=wrong).status_code == 401
    assert client.post("/api/session", json={}, headers={"Authorization": "open sesame"}).status_code == 401

    right = {"Authorization": "Bearer open sesame"}
    assert client.post("/api/session", json={}, headers=right).status_code == 200
    assert client.get("/api/info", headers=right).status_code == 200


def test_no_studio_password_leaves_the_api_open(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.delenv("STUDIO_PASSWORD", raising=False)
    assert TestClient(app).post("/api/session", json={}).status_code == 200


def test_vercel_is_locked_with_the_built_in_password_hash(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.setattr("ar.studio.server.VERCEL_PASSWORD_HASH", server._scrypt("test-pass").hex())
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("STUDIO_PASSWORD", raising=False)
    client = TestClient(app)
    assert client.post("/api/session", json={}).status_code == 401
    assert client.post("/api/session", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/session", json={}, headers={"Authorization": "Bearer test-pass"}).status_code == 200
    monkeypatch.setenv("STUDIO_PASSWORD", "other")  # the environment variable still wins
    assert client.post("/api/session", json={}, headers={"Authorization": "Bearer test-pass"}).status_code == 401


def test_a_lost_session_is_rebuilt_from_what_the_page_remembers(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.delenv("STUDIO_PASSWORD", raising=False)
    client = TestClient(app)

    assert client.post("/api/chat", json={"id": "gone", "message": "hi"}).json() == {"error": "unknown session"}

    restore = {"brief": {"question": "Is seasonal naive a good baseline?", "context": "weekly demand"},
               "ready": True, "sources": {"notes-2": "line one\nline two\n"}}
    made = client.post("/api/session", json={"restore": restore}).json()
    session = studio.get(made["id"])
    assert made["seen"] == len(session.events)
    assert session.intake.brief.question == "Is seasonal naive a good baseline?"
    assert session.intake.ready and list(session.sources) == ["notes-2"]


def test_post_replies_carry_the_events_the_caller_has_not_seen(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.delenv("STUDIO_PASSWORD", raising=False)
    client = TestClient(app)
    sid = client.post("/api/session", json={}).json()["id"]
    seen = len(studio.get(sid).events)

    reply = client.post("/api/chat", json={"id": sid, "after": seen, "message": "Is seasonal naive good?"}).json()
    assert [e["type"] for e in reply["events"]][:2] == ["chat", "chat"]
    assert all(e["seq"] > seen for e in reply["events"])

    added = client.post("/api/source", json={"id": sid, "name": "My Notes.md", "text": "a\nb\n"}).json()
    assert added == {"ok": True, "id": "my-notes"}  # no `after`, so no events


def test_vercel_rate_limit_uses_sanitized_client_ip_and_returns_retry_after(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.setattr(server, "_RATE_LIMITER", SlidingWindowLimiter())
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("STUDIO_PASSWORD", "")
    client = TestClient(app)
    headers = {"X-Vercel-Forwarded-For": "192.0.2.10"}

    for _ in range(12):
        assert client.post("/api/session", json={}, headers=headers).status_code == 200
    limited = client.post("/api/session", json={}, headers=headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json() == {"error": "rate limit exceeded", "retry_after": 60}

    other = client.post("/api/session", json={}, headers={"X-Vercel-Forwarded-For": "192.0.2.11"})
    assert other.status_code == 200
