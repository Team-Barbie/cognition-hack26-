"""The Vercel app serves the same studio routes as the local server."""

import json

from fastapi.testclient import TestClient

from ar.studio.asgi import app
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
    client = TestClient(app)

    sid = client.post("/api/session", json={}).json()["id"]
    assert client.post("/api/demo", json={"id": sid}).status_code == 200
    assert client.post("/api/run", json={"id": sid}).json() == {"ok": True}
    events = client.get("/api/events", params={"id": sid}).json()["events"]
    report = next(event["report"] for event in events if event["type"] == "report")
    assert report["outcome"] == "completed" and report["cited"] == ["claim-1", "claim-2"]
    assert json.dumps(report)
