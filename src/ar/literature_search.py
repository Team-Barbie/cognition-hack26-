"""Provider-neutral scholarly discovery; snippets remain discovery data."""
from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import quote, urlencode

from pydantic import Field

from ar.models import Record
from ar.retrieval import Fetched, PublicWeb, RetrievalError, html_text


class Candidate(Record):
    id: str = ""
    title: str
    authors: str = ""
    year: int | None = None
    doi: str = ""
    url: str
    document_urls: list[str] = Field(default_factory=list)
    abstract: str = ""
    abstract_source_sha256: str = ""
    snippet: str = ""
    publication_type: str = ""
    provider: str
    references: list[str] = Field(default_factory=list)


def candidate_id(candidate: Candidate) -> str:
    doi = candidate.doi.lower().strip().removeprefix("https://doi.org/").removeprefix("doi:")
    title = re.sub(r"\W+", "", candidate.title.casefold())
    identity = f"doi:{doi}" if doi else f"title:{title}:{candidate.year}" if title else candidate.url
    return "paper-" + hashlib.sha256(identity.encode()).hexdigest()[:20]


class SearchPage(Record):
    candidates: list[Candidate] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    raw_sha256: str = ""
    url: str = ""


class LiteratureSearch:
    def __init__(self, web: PublicWeb | None = None):
        self.web = web or PublicWeb()

    def search(self, backend: str, query: str, *, cursor: str | None = None, limit: int = 10,
               year_from: int | None = None, year_to: int | None = None,
               doi: str | None = None) -> tuple[SearchPage, Fetched]:
        limit = max(1, min(limit, 20))
        if backend == "crossref":
            # Natural-language research queries contain broad terms such as
            # "review" and "formation". Crossref's bibliographic search often
            # ranks papers from an unrelated field on those generic tokens.
            # Title search keeps discovery anchored to the actual topic.
            params = {"query.title": query, "rows": limit}
            # Crossref changes relevance ordering when cursor=* is present, often
            # returning generic matches instead of the requested topic. Use the
            # normal relevance-ranked first page; only send a real resume cursor.
            if cursor:
                params["cursor"] = cursor
            filters = []
            if year_from:
                filters.append(f"from-pub-date:{year_from}-01-01")
            if year_to:
                filters.append(f"until-pub-date:{year_to}-12-31")
            if filters:
                params["filter"] = ",".join(filters)
            contact = os.environ.get("WEB_SEARCH_CONTACT")
            if contact:
                params["mailto"] = contact
            url = ("https://api.crossref.org/works/" + quote(doi, safe="") if doi else
                   "https://api.crossref.org/works?" + urlencode(params))
            raw = self.web.get(url, max_bytes=2_000_000)
            message = raw.json().get("message", {})
            if doi and isinstance(message, dict) and message.get("DOI"):
                message = {"items": [message], "total-results": 1}
            if not isinstance(message, dict) or not isinstance(message.get("items"), list):
                raise RetrievalError("Crossref returned an invalid result page")
            found = []
            for item in message["items"]:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                title = item["title"][0] if isinstance(item["title"], list) else item["title"]
                doi = str(item.get("DOI", ""))
                dates = item.get("published", item.get("issued", {})).get("date-parts", [[]])
                year = dates[0][0] if dates and dates[0] else None
                links = [link["URL"] for link in (item.get("link") or [])
                         if isinstance(link, dict) and isinstance(link.get("URL"), str)]
                authors = ", ".join(" ".join(str(a.get(k, "")) for k in ("given", "family")).strip()
                                    for a in (item.get("author") or [])[:10] if isinstance(a, dict))
                found.append(Candidate(title=str(title), authors=authors, doi=doi, year=year,
                                       url=f"https://doi.org/{quote(doi, safe='/')}" if doi else str(item.get("URL", "")),
                                       abstract=html_text(str(item.get("abstract") or "")), document_urls=links[:4],
                                       publication_type=str(item.get("type", "")), provider=backend,
                                       references=[str(ref.get("DOI") or ref.get("article-title") or ref.get("unstructured") or "")[:500]
                                                   for ref in (item.get("reference") or [])[:20] if isinstance(ref, dict)]))
            page = SearchPage(candidates=found, next_cursor=message.get("next-cursor") if len(found) >= limit else None,
                              total=message.get("total-results", 0))
        elif backend == "europe_pmc":
            scoped = query
            if year_from or year_to:
                scoped = f"({query}) AND FIRST_PDATE:[{year_from or 1000}-01-01 TO {year_to or 9999}-12-31]"
            params = {"query": scoped, "format": "json", "resultType": "core", "pageSize": limit,
                      "cursorMark": cursor or "*"}
            raw = self.web.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(params),
                               max_bytes=2_000_000)
            data = raw.json()
            items = data.get("resultList", {}).get("result")
            if not isinstance(items, list):
                raise RetrievalError("Europe PMC returned an invalid result page")
            found = []
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                pmcid = str(item.get("pmcid") or "")
                docs = ([f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"]
                        if pmcid and item.get("isOpenAccess") == "Y" else [])
                doi = str(item.get("doi") or "")
                found.append(Candidate(title=item["title"], authors=str(item.get("authorString") or ""),
                                       year=int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
                                       doi=doi, url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item['id']}",
                                       abstract=html_text(str(item.get("abstractText") or "")), document_urls=docs,
                                       publication_type=str(item.get("pubTypeList", {})), provider=backend))
            page = SearchPage(candidates=found, next_cursor=data.get("nextCursorMark") if len(found) >= limit else None,
                              total=data.get("hitCount", 0))
        elif backend == "openalex":
            params = {
                "search": query,
                "per-page": limit,
                "select": "id,doi,title,publication_year,authorships,abstract_inverted_index,primary_location,open_access,type",
            }
            filters = []
            if year_from:
                filters.append(f"from_publication_date:{year_from}-01-01")
            if year_to:
                filters.append(f"to_publication_date:{year_to}-12-31")
            if filters:
                params["filter"] = ",".join(filters)
            if cursor:
                params["cursor"] = cursor
            contact = os.environ.get("WEB_SEARCH_CONTACT")
            if contact:
                params["mailto"] = contact
            raw = self.web.get("https://api.openalex.org/works?" + urlencode(params), max_bytes=2_000_000)
            data = raw.json()
            items = data.get("results")
            if not isinstance(items, list):
                raise RetrievalError("OpenAlex returned an invalid result page")
            found = []
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                inverted = item.get("abstract_inverted_index") or {}
                positions = [(int(position), str(word)) for word, indexes in inverted.items()
                             if isinstance(indexes, list) for position in indexes
                             if isinstance(position, int) or str(position).isdigit()]
                abstract = " ".join(word for _, word in sorted(positions))
                doi = str(item.get("doi") or "").removeprefix("https://doi.org/")
                location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
                access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
                urls = [str(value) for value in (access.get("oa_url"), location.get("pdf_url")) if value]
                landing = str(location.get("landing_page_url") or item.get("id") or "")
                author_names = []
                for entry in (item.get("authorships") or [])[:10]:
                    author = entry.get("author") if isinstance(entry, dict) else None
                    if isinstance(author, dict) and author.get("display_name"):
                        author_names.append(str(author["display_name"]))
                authors = ", ".join(author_names)
                found.append(Candidate(
                    title=str(item["title"]), authors=authors,
                    year=int(item["publication_year"]) if str(item.get("publication_year", "")).isdigit() else None,
                    doi=doi, url=landing, document_urls=list(dict.fromkeys(urls)), abstract=abstract,
                    publication_type=str(item.get("type") or ""), provider=backend,
                ))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            page = SearchPage(candidates=found, next_cursor=meta.get("next_cursor") if cursor and len(found) >= limit else None,
                              total=int(meta.get("count") or 0))
        elif backend == "brave":
            key = os.environ.get("BRAVE_SEARCH_API_KEY")
            if not key:
                raise RetrievalError("BRAVE_SEARCH_API_KEY is required for general web search")
            offset = int(cursor or 0)
            if offset not in range(10):
                raise RetrievalError("Brave pagination limit reached")
            raw = self.web.get("https://api.search.brave.com/res/v1/web/search?" + urlencode(
                {"q": query, "count": limit, "offset": offset}), headers={"X-Subscription-Token": key},
                max_bytes=2_000_000)
            items = raw.json().get("web", {}).get("results", [])
            found = [Candidate(title=item["title"], url=item["url"], snippet=html_text(item.get("description", "")),
                               provider=backend) for item in items if isinstance(item, dict) and item.get("url")]
            page = SearchPage(candidates=found, next_cursor=str(offset + 1) if len(found) >= limit and offset < 9 else None)
        else:
            raise RetrievalError(f"unknown search backend: {backend}")
        for candidate in page.candidates:
            candidate.id = candidate_id(candidate)
            if candidate.abstract:
                candidate.abstract_source_sha256 = hashlib.sha256(raw.body).hexdigest()
        page.raw_sha256 = hashlib.sha256(raw.body).hexdigest()
        page.url = raw.url
        return page, raw
