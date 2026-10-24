"""The Vercel app serves the same studio routes as the local server."""

import json

from fastapi.testclient import TestClient

from ar.studio.asgi import app
from ar.studio import server
from ar.studio.server import Studio


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
    assert client.post("/api/run", json={"id": sid}).json() == {"ok": True}
    events = client.get("/api/events", params={"id": sid}).json()["events"]
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
