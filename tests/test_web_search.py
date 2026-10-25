import io
import json

import pytest

from ar.studio.server import Studio, handle
from ar.studio.intake import Brief
from ar.studio.session import Session
from ar.web_search import SearchResult
from ar.web_search import WebSearchError, search_crossref
from ar.agents.fake import FakeAgent


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_crossref_search_normalizes_metadata():
    payload = {"message": {"items": [{
        "DOI": "10.1000/example", "title": ["A useful paper"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "published-online": {"date-parts": [[2025, 2, 3]]},
        "publisher": "Example Press", "type": "journal-article",
    }]}}
    seen = {}

    def fetch(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Response(json.dumps(payload).encode())

    result = search_crossref("agent handoff", fetch=fetch)[0]
    assert result.title == "A useful paper"
    assert (result.authors, result.year) == ("Ada Lovelace", 2025)
    assert result.url == "https://doi.org/10.1000/example"
    assert "query.bibliographic=agent+handoff" in seen["url"] and seen["timeout"] == 12


def test_crossref_search_has_clear_validation_and_network_errors():
    with pytest.raises(ValueError, match="3 characters"):
        search_crossref("x")
    with pytest.raises(WebSearchError, match="Crossref"):
        search_crossref("valid query", fetch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))


def test_search_api_returns_results_without_adding_fake_sources(tmp_path, monkeypatch):
    studio = Studio(tmp_path, fake=True)
    session = studio.new_session()
    found = [{"title": "Paper", "authors": "A. Author", "year": 2024, "publisher": "P",
              "doi": "10/x", "url": "https://doi.org/10/x", "kind": "journal-article"}]
    monkeypatch.setattr(session, "search_web", lambda query: found if query == "handoff" else [])
    status, _, body = handle(studio, "POST", "/api/search", "",
                             json.dumps({"id": session.id, "query": "handoff"}).encode())
    assert status == 200 and json.loads(body)["results"] == found
    assert session.sources == {}


def test_run_web_scout_emits_candidates_without_turning_them_into_evidence(tmp_path, monkeypatch):
    session = Session(tmp_path, None)
    session.intake.brief = Brief(question="How should agents hand work to each other?", context="Research systems")
    session.sources["uploaded-paper"] = "full text line one\nfull text line two"
    result = SearchResult("Related paper", "A. Author", 2026, "Press", "10/x", "https://doi.org/10/x", "article")
    monkeypatch.setattr("ar.studio.session.search_crossref", lambda query, limit: [result])

    session._search_web_for_run()

    assert session.web_results[0]["title"] == "Related paper"
    assert list(session.sources) == ["uploaded-paper"]
    assert next(e for e in session.events if e["type"] == "web_results")["query"].endswith("Research systems")
    scout = [e for e in session.events if e["type"] == "agent" and e["id"] == "scout"]
    assert [e["status"] for e in scout] == ["running", "done"]


def test_selected_search_papers_create_one_restorable_comparison_review(tmp_path):
    import yaml

    session = Session(tmp_path, make_agent=FakeAgent)
    session.intake.brief = Brief(question="Which treatment works?")
    session.web_results = [{"doi": "10.1000/a"}, {"doi": "10.1000/b"}]
    session.set_literature_reviews([{"id": "other", "question": "Other review?"}])
    session.use_search_paper("10.1000/a")
    session.use_search_paper("10.1000/a")
    reviews = session.use_search_paper("10.1000/b")
    assert len(reviews) == 2 and reviews[-1]["seed_dois"] == ["10.1000/a", "10.1000/b"]
    assert not session.sources  # Search metadata is never presented as paper text.
    restored = Session(tmp_path, make_agent=FakeAgent)
    restored.restore({"brief": {"question": "Updated question?"}, "literature_reviews": reviews})
    project = restored.root / "run-1"
    restored._write_project(project)
    specs = yaml.safe_load((project / "input/literature.yaml").read_text())["reviews"]
    assert specs[-1]["question"] == "Updated question?"
    assert specs[-1]["seed_dois"] == ["10.1000/a", "10.1000/b"]
    restored.use_search_paper("10.1000/a", remove=True)
    assert restored.literature_reviews[-1].seed_dois == ["10.1000/b"]
    restored.use_search_paper("10.1000/b", remove=True)
    assert [r.id for r in restored.literature_reviews] == ["other"]


def test_selection_endpoint_validates_results_and_demo_mode(tmp_path):
    studio = Studio(tmp_path, fake=True)
    session = studio.new_session()

    def select(**fields):
        status, _, body = handle(studio, "POST", "/api/search/use", "",
                                 json.dumps({"id": session.id, **fields}).encode())
        return status, json.loads(body)

    assert select(doi="10.1000/unknown")[0] == 400
    session.web_results = [{"doi": "10.1000/a"}]
    assert "live model" in select(doi="10.1000/a")[1]["error"]
    session.make_agent = FakeAgent
    assert select(doi="10.1000/a", remove="false")[0] == 400
    status, data = select(doi="10.1000/a")
    assert status == 200 and data["reviews"][0]["seed_dois"] == ["10.1000/a"]
    session.web_results = [{"doi": f"10.1000/{i}"} for i in range(7)]
    for i in range(5):
        assert select(doi=f"10.1000/{i}")[0] == 200
    assert select(doi="10.1000/6")[0] == 400
    assert len(session.literature_reviews[0].seed_dois) == 6
