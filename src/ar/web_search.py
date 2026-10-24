"""Small, no-key scholarly search client backed by Crossref metadata."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


CROSSREF_WORKS = "https://api.crossref.org/works"
MAX_RESULTS = 10


class WebSearchError(RuntimeError):
    """A safe, user-facing search failure."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    authors: str
    year: int | None
    publisher: str
    doi: str
    url: str
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any, limit: int = 300) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = item.get(key, {}).get("date-parts", []) if isinstance(item.get(key), dict) else []
        try:
            value = int(parts[0][0])
        except (IndexError, TypeError, ValueError):
            continue
        if 1000 <= value <= 9999:
            return value
    return None


def _authors(item: dict[str, Any]) -> str:
    names = []
    for author in item.get("author", [])[:8]:
        if not isinstance(author, dict):
            continue
        name = " ".join(filter(None, (_text(author.get("given"), 80), _text(author.get("family"), 80))))
        if name:
            names.append(name)
    if len(item.get("author", [])) > len(names):
        names.append("et al.")
    return ", ".join(names) or "Authors not listed"


def search_crossref(
    query: str,
    limit: int = 6,
    *,
    fetch: Callable[..., Any] = urlopen,
) -> list[SearchResult]:
    """Search Crossref's public Works API and return normalized paper metadata."""
    query = _text(query, 500)
    if len(query) < 3:
        raise ValueError("enter at least 3 characters to search")
    limit = max(1, min(int(limit), MAX_RESULTS))
    params = urlencode({
        "query.bibliographic": query,
        "rows": limit,
        "select": "DOI,title,author,published-print,published-online,published,issued,created,publisher,type",
    })
    contact = _text(os.environ.get("WEB_SEARCH_CONTACT"), 200)
    agent = "ResLoop/0.1"
    if contact:
        agent += f" (mailto:{contact})"
    request = Request(f"{CROSSREF_WORKS}?{params}", headers={"Accept": "application/json", "User-Agent": agent})
    try:
        with fetch(request, timeout=12) as response:
            payload = json.load(response)
    except Exception as exc:
        raise WebSearchError("Web search could not reach Crossref. Check the internet connection and try again.") from exc

    items = payload.get("message", {}).get("items", [])
    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        if not title:
            continue
        doi = _text(item.get("DOI"), 240)
        results.append(SearchResult(
            title=title,
            authors=_authors(item),
            year=_year(item),
            publisher=_text(item.get("publisher"), 160) or "Publisher not listed",
            doi=doi,
            url=f"https://doi.org/{quote(doi, safe='/()')}" if doi else "",
            kind=_text(item.get("type"), 80) or "work",
        ))
    return results
