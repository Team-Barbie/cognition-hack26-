"""Presenter questions use the real discovery/report pipeline, never fixture answers."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ar.agents.fake import FakeAgent
from ar.studio.asgi import app
from ar.studio.server import SlidingWindowLimiter, Studio, handle
from ar.studio.session import DEMO_BRIEF, Session
from tests.test_literature import Decisions, Search, Web


def test_custom_question_resets_showcase_and_preserves_the_complete_brief(tmp_path):
    session = Session(tmp_path, make_agent=FakeAgent)
    session.use_demo(showcase=True)
    session.open_questions["old question"] = "Reader"
    session.set_question({"question": "  Does treatment help?  ", "context": "Clinical evidence",
                          "constraints": ["Adults only"], "open_questions": ["What are the harms?"]})
    assert session.intake.ready
    assert session.intake.brief.as_dict() == {
        "question": "Does treatment help?", "context": "Clinical evidence",
        "constraints": ["Adults only"], "open_questions": ["What are the harms?"],
    }
    assert not session.sources and not session.literature_reviews and not session.open_questions
    assert not session.test_run and not session.showcase_mode
    assert session.events[-1]["who"] == "Intake"
    assert [e for e in session.events if e["type"] == "research_setup"][-1]["mode"] == "custom"
    session.use_demo()
    assert session.intake.brief == DEMO_BRIEF and len(session.sources) == 3


@pytest.mark.parametrize("data", [
    {"question": "   "}, {"question": "x"}, {"question": "x" * 2001},
    {"question": 123}, {"question": "Valid question?", "constraints": "not a list"},
    {"question": "Valid question?", "open_questions": [" "]},
    {"question": "Valid question?", "open_questions": ["Subquestion?"] * 13},
    {"question": "Valid question?", "unexpected": True},
])
def test_invalid_custom_briefs_leave_the_workspace_untouched(tmp_path, data):
    session = Session(tmp_path, make_agent=FakeAgent)
    session.use_demo()
    with pytest.raises(ValueError):
        session.set_question(data)
    assert session.intake.brief == DEMO_BRIEF and len(session.sources) == 3


def test_offline_custom_question_api_reports_configuration_without_loading_fixtures(tmp_path):
    studio = Studio(tmp_path, fake=True)
    session = studio.new_session()
    status, _, body = handle(studio, "POST", "/api/question", "", json.dumps({
        "id": session.id, "question": "How does hybrid work affect productivity?"
    }).encode())
    assert status == 400 and "live research model" in json.loads(body)["error"]
    assert not session.sources and not session.intake.brief.question and not session.running


def test_custom_question_cannot_replace_a_running_investigation(tmp_path):
    session = Session(tmp_path, make_agent=FakeAgent)
    session._thread = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="finish"):
        session.set_question({"question": "A different question?"})
    assert not session.intake.brief.question


def test_custom_brief_survives_session_recovery_without_demo_papers(tmp_path):
    session = Session(tmp_path, make_agent=FakeAgent)
    session.set_question({"question": "Does treatment help?", "constraints": ["Adults only"]})
    snapshot = {"brief": session.intake.brief.as_dict(), "ready": True, "demo": False,
                "sources": {}, "literature_reviews": [], "test_run": False}
    restored = Session(tmp_path, make_agent=FakeAgent)
    restored.restore(snapshot)
    assert restored.intake.ready and restored.intake.brief == session.intake.brief
    assert not restored.sources and not restored.test_run


def test_presenter_question_discovers_reads_and_streams_a_cited_report(tmp_path, monkeypatch):
    import ar.studio.server as server

    studio = Studio(tmp_path, fake=True)
    session = studio.new_session()
    session.make_agent = Decisions
    monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
    monkeypatch.setattr(server, "_RATE_LIMITER", SlidingWindowLimiter())
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("STUDIO_PASSWORD", "")
    search = Search()
    monkeypatch.setattr("ar.literature.LiteratureSearch", lambda *args: search)
    monkeypatch.setattr("ar.literature.PublicWeb", Web)
    client = TestClient(app)
    setup = client.post("/api/question", json={"id": session.id, "after": 0,
                        "question": "Does treatment help?", "context": "A treatment evidence review"})
    assert setup.status_code == 200
    assert any(e["type"] == "brief" and e["ready"] for e in setup.json()["events"])
    ran = client.post("/api/run", json={"id": session.id, "after": setup.json()["events"][-1]["seq"],
                                       "test_run": False})
    assert ran.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in ran.text.splitlines()]
    assert events[-1] == {"done": True, "status": "done"}, events[-5:]
    report = next(e["report"] for e in events if e.get("type") == "report")
    assert report["outcome"] == "completed" and report["cited"]
    assert "Treatment reduced errors" in report["markdown"]
    assert search.calls and "Does treatment help?" in search.calls[0][1]
    assert report["claims"][0]["links"][0]["receipt"]
    project = session.root / "run-1"
    assert not list((project / "input/papers").glob("*.md"))
    assert "Does treatment help?" in (project / "input/brief.md").read_text()
    assert session.intake.brief.question == "Does treatment help?"
    session.set_question({"question": "What are the treatment limitations?"})
    assert session.report is None and session.status == "intake" and not session.literature_reviews


def test_custom_question_never_uses_a_fixture_only_test_worker(tmp_path, monkeypatch):
    from ar.adapter import LiveAgent
    from ar.controller import RunOutcome

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("ar.studio.server.make_client", lambda *args: object())
    session = Studio(tmp_path, provider="anthropic", model="chosen-model").new_session()
    received = []
    monkeypatch.setattr("ar.controller.run_project", lambda project, agent, **kw:
                        received.append(agent.inner) or RunOutcome.BLOCKED)
    session.set_question({"question": "Does treatment help?"})
    session.start(test_run=True)
    session.wait(30)
    assert not session.test_run and isinstance(received[0], LiveAgent)
