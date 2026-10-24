import io
import json

import pytest

from ar.studio.server import Studio, handle
from ar.web_search import WebSearchError, search_crossref


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
