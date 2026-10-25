import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ar import db
from ar.agents.fake import FakeAgent
from ar.agents.literature_review import ReviewAction, Screening, SearchQuery
from ar.broker import Broker
from ar.evidence import get_claim
from ar.literature import LiteratureReviewAgent, ReviewConfig, ReviewSpec, load_reviews
from ar.literature_search import Candidate, LiteratureSearch, SearchPage, candidate_id
from ar.models import AgentResult, ClaimDraft, EvidenceLink, LineSelector
from ar.retrieval import Fetched, PublicWeb, RetrievalError, extract_document, public_url
from ar.snapshots import BrokerError, sha256_text


XML = b'<?xml version="1.0"?><article><body><sec><title>Results</title><p>The treatment reduced errors in 20 datasets.</p><p>The study was not randomized.</p></sec></body></article>'


class Search:
    def __init__(self, *, pages=False, abstract=False):
        self.calls = []
        self.pages, self.abstract = pages, abstract

    def search(self, backend, query, **kwargs):
        self.calls.append((backend, query, kwargs))
        hit = Candidate(title="A controlled comparison", doi="10.1000/a", year=2025, provider=backend,
                        url="https://papers.example/paper", document_urls=["https://papers.example/paper.xml"],
                        abstract="The treatment reduced errors in 20 datasets." if self.abstract else "")
        hit.id = candidate_id(hit)
        raw = Fetched("https://index.example/search", "application/json", json.dumps({"title": hit.title}).encode())
        hit.abstract_source_sha256 = sha256_text(raw.body.decode()) if self.abstract else ""
        return SearchPage(candidates=[hit], next_cursor="page2" if self.pages and not kwargs["cursor"] else None,
                          raw_sha256=sha256_text(raw.body.decode())), raw


class Web:
    def __init__(self, *, failure=False):
        self.calls = []
        self.failure = failure

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.failure:
            raise RetrievalError("HTTP 403")
        return Fetched(url, "application/xml", XML)


class Decisions:
    def __init__(self, *, followup=False, bad_quote=False, unknown_citation=False, missing_screen=False):
        self.packets = []
        self.followup, self.bad_quote = followup, bad_quote
        self.unknown_citation, self.missing_screen = unknown_citation, missing_screen
        self.fake = FakeAgent()

    def run(self, role, packet):
        self.packets.append(packet)
        if role == "literature_review":
            ctx = packet.review_context
            phase = ctx["phase"]
            questions = ctx["subquestions"]
            claims = [dep.id for dep in packet.claim_items()]
            payload = ReviewAction()
            if phase == "plan":
                payload.queries = [SearchQuery(query=question + " evidence", backend="crossref", question=question, intent="discovery") for question in questions]
                payload.queries.append(SearchQuery(query=questions[0] + " limitations null results", backend="europe_pmc", question=questions[0], intent="contradictory"))
            elif phase == "screen":
                payload.screening = [] if self.missing_screen else [Screening(candidate_id=hit["id"], decision="include", reason="Relevant study", basis="abstract" if hit["abstract"] else "metadata") for hit in ctx["candidates"]]
            else:
                payload.coverage = {question: claims for question in questions}
                if phase == "reflect" and self.followup:
                    payload.queries = [SearchQuery(query="10.1000/a replication", backend="crossref", question=questions[0], intent="citation")]
                if phase == "synthesize":
                    payload.summary_markdown = "# Findings\n" + "\n".join(f"Supported result [claim:{cid}]" for cid in claims)
                    if self.unknown_citation:
                        payload.summary_markdown += " [claim:invented]"
            return AgentResult(role=role, task_id=packet.task_id, outcome="produced", model_calls=1,
                               payload=ReviewAction.model_validate(payload.model_dump()).model_dump(mode="json"))
        if role == "evidence_reader":
            read = packet.reads[0]
            lines = read.content.splitlines()
            index = next((n for n, text in enumerate(lines) if "reduced errors" in text), None)
            claims = []
            if index is not None:
                claims = [ClaimDraft(statement="Treatment reduced errors in the evaluated datasets.",
                                     scope="20 datasets; non-randomized study", evidence_links=[EvidenceLink(
                                         relation="supports", source_ref=read.source_ref,
                                         locator=LineSelector(start=read.selector.start + index, end=read.selector.start + index),
                                         quote="invented quote" if self.bad_quote else lines[index],
                                         read_receipt_id=read.read_receipt_id)])]
            return AgentResult(role=role, task_id=packet.task_id, outcome="produced", claims=claims, model_calls=1,
                               payload={"source_id": read.source_ref.source_id, "relevant": True, "summary": "Study results"})
        return self.fake.run(role, packet)


@pytest.fixture
def project(tmp_path):
    db.init_project(tmp_path).close()
    return tmp_path


def run_review(project, **options):
    search, web, agent = Search(**options.pop("search_options", {})), Web(**options.pop("web_options", {})), Decisions(**options.pop("agent_options", {}))
    spec = ReviewSpec(id="methods", question="Does treatment help?", **options)
    runner = LiteratureReviewAgent(project, agent, search=search, web=web)
    return runner.run(spec), runner, search, web, agent, spec


def test_review_searches_deduplicates_reads_and_exports_verified_evidence(project):
    result, runner, search, web, agent, spec = run_review(project)
    assert result.status == "finished", result
    assert len(search.calls) == 2 and len(web.calls) == 1
    assert len(result.source_refs) == len(result.claim_ids) == 1
    assert result.model_calls == 5 and result.documents == 1 and result.fetches == 1
    with db.connect(project) as conn:
        claim = get_claim(conn, result.claim_ids[0])
        link = claim.evidence_links[0]
        row = conn.execute("SELECT * FROM read_receipts WHERE id=?", (link.read_receipt_id,)).fetchone()
        assert row["task_id"] == claim.origin_task
        assert link.quote == "The treatment reduced errors in 20 datasets."
    ledger = json.loads((project / result.report_path).with_name("search-ledger.json").read_text())
    assert ledger["protocol"]["question"] == spec.question
    assert len(ledger["candidates"]) == 1 and ledger["sources"][0]["kind"] == "full_text"
    assert ledger["sources"][0]["raw_sha256"]
    assert any(p.role == "evidence_reader" and p.skills == ["literature_extraction"] for p in agent.packets)
    before = (len(search.calls), len(web.calls), len(agent.packets))
    assert runner.run(spec) == result
    assert before == (len(search.calls), len(web.calls), len(agent.packets))


def test_selected_papers_are_read_and_supplied_together_for_cross_reference(project):
    class SeedSearch(Search):
        def search(self, backend, query, **kwargs):
            page, raw = super().search(backend, query, **kwargs)
            if query == "10.1000/b":
                hit = page.candidates[0]
                hit.doi, hit.title = query, "A replication study"
                hit.id = candidate_id(hit)
            return page, raw

    search, agent = SeedSearch(), Decisions()
    spec = ReviewSpec(id="selected", question="Does treatment help?", seed_dois=["10.1000/b", "10.1000/a"])
    runner = LiteratureReviewAgent(project, agent, search=search, web=Web())
    result = runner.run(spec)
    assert result.status == "finished", result
    assert [call[1] for call in search.calls[:2]] == spec.seed_dois
    assert result.documents == 2 and len(result.claim_ids) == 2
    synthesis = next(p for p in agent.packets if p.review_context.get("phase") == "synthesize")
    assert {dep.id for dep in synthesis.claim_items()} == set(result.claim_ids)
    assert {s["doi"] for s in synthesis.review_context["source_overview"]} == set(spec.seed_dois)
    with db.connect(project) as conn:
        for cid in result.claim_ids:
            claim = get_claim(conn, cid)
            assert claim.evidence_links[0].read_receipt_id
    before = len(search.calls)
    assert runner.run(spec) == result and len(search.calls) == before


def test_unresolved_selected_doi_is_reported_as_a_limitation(project):
    result, _, search, _, _, _ = run_review(project, seed_dois=["10.1000/missing"])
    assert search.calls[0][1] == "10.1000/missing"
    assert any("10.1000/missing could not be resolved" in text for text in result.limitations)
    assert result.status == "partial"


def test_selected_doi_uses_exact_crossref_record_with_document_links():
    class API:
        def get(self, url, **kwargs):
            assert url == "https://api.crossref.org/works/10.1000%2Fa"
            return Fetched(url, "application/json", json.dumps({"message": {
                "DOI": "10.1000/a", "title": ["Selected paper"],
                "abstract": "<p>Indexed abstract.</p>",
                "link": [{"URL": "https://papers.example/full.pdf"}],
                "reference": [{"DOI": "10.1000/b"}],
            }}).encode())

    page, raw = LiteratureSearch(API()).search("crossref", "10.1000/a", doi="10.1000/a")
    assert len(page.candidates) == 1 and page.next_cursor is None
    hit = page.candidates[0]
    assert hit.doi == "10.1000/a" and hit.document_urls == ["https://papers.example/full.pdf"]
    assert hit.references == ["10.1000/b"] and hit.abstract_source_sha256 == page.raw_sha256


@pytest.mark.parametrize("overrides", [
    {"seed_dois": ["http://localhost/private"]},
    {"seed_dois": ["10.1000/a"], "backends": ["europe_pmc"]},
    {"seed_dois": [f"10.1000/{n}" for n in range(7)]},
])
def test_selected_paper_protocol_rejects_invalid_configuration(overrides):
    with pytest.raises(ValueError):
        ReviewSpec(id="selected", question="Compare findings?", **overrides)


def test_unseeded_reviews_keep_their_existing_journal_identity():
    spec = ReviewSpec(id="existing", question="Existing review?")
    old_protocol = spec.model_dump(exclude={"seed_dois"})
    assert spec.key == "existing-" + sha256_text(json.dumps(old_protocol, sort_keys=True))[:16]
    assert spec.model_copy(update={"seed_dois": ["10.1000/a"]}).key != spec.key


def test_multiple_instances_keep_evidence_and_decisions_separate(project):
    first, runner, search, web, agent, spec = run_review(project)
    second = runner.run(spec.model_copy(update={"id": "safety", "question": "Is treatment safe?"}))
    assert second.key != first.key and second.report_path != first.report_path
    assert set(first.claim_ids).isdisjoint(second.claim_ids)
    assert set(first.coverage) == {spec.question}
    assert set(second.coverage) == {"Is treatment safe?"}
    assert all(cid not in second.summary_markdown for cid in first.claim_ids)


@pytest.mark.parametrize("budget", [0, 1, 3])
def test_budget_is_hard_and_partial_work_is_preserved(project, budget):
    result, _, _, _, agent, _ = run_review(project, max_model_calls=budget)
    assert result.status == "partial"
    assert result.model_calls <= budget
    assert len(agent.packets) <= budget
    assert "budget" in result.stop_reason
    assert (project / result.report_path).is_file()


def test_global_allowance_does_not_change_instance_identity(project):
    agent = Decisions()
    runner = LiteratureReviewAgent(project, agent, search=Search(), web=Web())
    spec = ReviewSpec(id="small", question="A research question")
    result = runner.run(spec, call_limit=1)
    assert result.key == spec.key and result.model_calls == 1
    assert runner.run(spec, call_limit=100).model_calls == 1


@pytest.mark.parametrize("phase", ["search", "acquired"])
def test_interrupted_review_resumes_without_repeating_finished_network_operations(project, phase):
    search, web, agent = Search(), Web(), Decisions()
    interrupted = []
    def stop_once(event):
        if event["phase"] == phase and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
    runner = LiteratureReviewAgent(project, agent, search=search, web=web, on_event=stop_once)
    spec = ReviewSpec(id="resume", question="Does treatment help?")
    with pytest.raises(KeyboardInterrupt):
        runner.run(spec)
    result = runner.run(spec)
    assert result.status == "finished"
    assert len(search.calls) == 2 and len(web.calls) == 1
    assert result.model_calls == 5 and len(result.claim_ids) == 1


def test_unknown_model_outcome_is_not_silently_retried(project):
    class Interrupted(Decisions):
        def run(self, role, packet):
            raise KeyboardInterrupt
    runner = LiteratureReviewAgent(project, Interrupted(), search=Search(), web=Web())
    spec = ReviewSpec(id="unknown", question="A research question")
    with pytest.raises(KeyboardInterrupt):
        runner.run(spec)
    result = runner.run(spec)
    assert result.status == "partial" and result.model_calls == 1
    assert "unknown outcome" in result.stop_reason


def test_concurrent_review_instances_are_isolated_and_duplicate_instance_is_leased(project):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()
    class Blocked(Decisions):
        def run(self, role, packet):
            if role == "literature_review" and packet.review_context["phase"] == "plan":
                entered.set()
                release.wait(10)
            return super().run(role, packet)
    runner = LiteratureReviewAgent(project, Blocked(), search=Search(), web=Web())
    spec = ReviewSpec(id="parallel", question="Does treatment help?")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.run, spec)
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError, match="already running"):
                runner.run(spec)
            second = pool.submit(runner.run, spec.model_copy(update={"id": "second"}))
        finally:
            release.set()
        a, b = first.result(30), second.result(30)
    assert a.status == b.status == "finished", (a, b)
    assert set(a.claim_ids).isdisjoint(b.claim_ids)


def test_abstract_fallback_is_labeled_and_not_claimed_as_full_text(project):
    result, *_ = run_review(project, search_options={"abstract": True}, web_options={"failure": True})
    assert result.status == "partial" and result.claim_ids
    with db.connect(project) as conn:
        claim = get_claim(conn, result.claim_ids[0])
        assert any("abstract" in note for note in claim.limitations)
        meta = json.loads(conn.execute("SELECT metadata_json FROM literature_sources").fetchone()[0])
        assert meta["kind"] == "abstract" and meta["raw_sha256"]


def test_acquisition_failure_never_turns_a_snippet_into_evidence(project):
    result, *_ = run_review(project, web_options={"failure": True})
    assert result.status == "partial" and result.source_refs == [] and result.claim_ids == []


def test_invented_quotes_are_rejected_by_existing_broker(project):
    result, *_ = run_review(project, agent_options={"bad_quote": True})
    assert result.claim_ids == [] and result.status == "partial"
    with db.connect(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM claim_rejections").fetchone()[0] == 1


def test_unknown_synthesis_citations_fail_without_exporting_them(project):
    result, *_ = run_review(project, agent_options={"unknown_citation": True})
    assert result.status == "failed" and "outside this review" in result.stop_reason
    assert "invented" not in (project / result.report_path).read_text()


def test_screening_must_account_for_every_candidate(project):
    result, *_ = run_review(project, agent_options={"missing_screen": True})
    assert result.status == "failed" and "every supplied candidate" in result.stop_reason


def test_pagination_and_followup_are_bounded_and_recorded(project):
    result, _, search, *_ = run_review(project, pages_per_query=2, search_options={"pages": True},
                                       agent_options={"followup": True}, max_rounds=2)
    assert len(search.calls) == 6
    assert result.status == "partial" and "round budget" in result.stop_reason
    assert any(call[2]["cursor"] == "page2" for call in search.calls)


def test_network_policy_is_enforced_before_model_or_network_use(project):
    (project / "policies" / "permissions.yaml").write_text(yaml.safe_dump({"roles": {"literature_review": ["files.read"]}, "denied": ["network.search"]}))
    agent, search = Decisions(), Search()
    with pytest.raises(BrokerError, match="ACCESS_DENIED"):
        LiteratureReviewAgent(project, agent, search=search, web=Web()).run(ReviewSpec(id="denied", question="A research question"))
    assert not agent.packets and not search.calls


def test_main_pipeline_can_use_review_evidence_without_uploaded_papers(project):
    from ar.controller import RunOutcome, run_project

    (project / "input" / "brief.md").write_text("# Brief\nQuestion: Does treatment help?\nDeliverable: Report with limitations.\n")
    spec = ReviewSpec(id="methods", question="Does treatment help?")
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [spec.model_dump()]}))
    agent = Decisions()
    runner = LiteratureReviewAgent(project, agent, search=Search(), web=Web())
    outcome = run_project(project, agent, literature_runner=runner)
    assert outcome == RunOutcome.COMPLETED
    report = (project / "output" / "report.md").read_text()
    assert "Treatment reduced errors" in report and "https://papers.example/paper.xml" in report
    manifest = json.loads((project / "output" / "provenance.json").read_text())
    assert manifest["literature_reviews"][0]["id"] == "methods"
    assert manifest["sources"] and manifest["receipts"]
    assert manifest["literature_sources"][0]["provenance"] == "runtime_retrieved"
    assert any(name.endswith("search-ledger.json") for name in manifest["files"])


def test_partial_review_keeps_the_combined_report_incomplete(project):
    from ar.controller import RunOutcome, run_project
    (project / "input" / "brief.md").write_text("# Brief\nQuestion: Does treatment help?\nDeliverable: Report with limitations.\n")
    spec = ReviewSpec(id="methods", question="Does treatment help?")
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [spec.model_dump()]}))
    agent = Decisions()
    runner = LiteratureReviewAgent(project, agent, search=Search(abstract=True), web=Web(failure=True))
    assert run_project(project, agent, literature_runner=runner) == RunOutcome.BLOCKED
    manifest = json.loads((project / "output" / "provenance.json").read_text())
    assert not manifest["complete"] and manifest["literature_reviews"][0]["status"] == "partial"
    assert "abstract" in (project / "output" / "report.md").read_text()


def test_invalid_main_inputs_do_not_spend_a_review_budget(project):
    from ar.controller import run_project
    class Unused:
        def run(self, *args, **kwargs):
            raise AssertionError("should not dispatch a review")
    (project / "input" / "literature.yaml").write_text("reviews:\n- id: test\n  question: A valid question\n")
    with pytest.raises(FileNotFoundError):
        run_project(project, Decisions(), literature_runner=Unused())


def test_configuration_rejects_duplicate_ids_bad_dates_and_unsafe_paths():
    with pytest.raises(ValueError):
        ReviewSpec(id="../escape", question="A research question")
    with pytest.raises(ValueError):
        ReviewSpec(id="x", question="A research question", year_from=2025, year_to=2020)
    spec = ReviewSpec(id="x", question="A research question")
    with pytest.raises(ValueError, match="unique"):
        ReviewConfig(reviews=[spec, spec])


def test_cli_runs_selected_configured_review(project, monkeypatch):
    from ar import cli

    specs = [ReviewSpec(id=name, question=f"Question about {name}") for name in ("methods", "safety")]
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [s.model_dump() for s in specs]}))
    monkeypatch.setattr("ar.literature_client.review_agent", lambda *args, **kwargs: Decisions())
    monkeypatch.setattr("ar.literature.LiteratureSearch", lambda *args: Search())
    monkeypatch.setattr("ar.literature.PublicWeb", Web)
    result = CliRunner().invoke(cli.app, ["literature", str(project), "--review", "safety"])
    assert result.exit_code == 0, result.output
    assert "safety:" in result.output and "methods:" not in result.output


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/x", "https://[::1]/x", "http://169.254.169.254/", "https://localhost/", "https://user:pass@example.com/", "https://example.com:8080/", "https://example.com/\nheader"])
def test_fetch_url_validation_rejects_private_and_credentialed_destinations(url):
    with pytest.raises(RetrievalError):
        public_url(url)


def test_dns_is_checked_at_connection_time(monkeypatch):
    from ar.retrieval import _public_connection
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(RetrievalError, match="private"):
        _public_connection(("looks-public.example", 443), 1)


def test_xml_entities_are_not_expanded_and_html_scripts_are_ignored():
    hostile = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><article><body><p>&x;</p></body></article>'
    with pytest.raises(RetrievalError):
        extract_document(Fetched("https://example.com", "application/xml", hostile))
    document = extract_document(Fetched("https://example.com", "text/html", b'<p>Real text</p><script>steal secrets</script><meta name="citation_pdf_url" content="/paper.pdf">'))
    assert document.kind == "web_page" and "steal" not in document.text
    assert document.citation_urls == ("https://example.com/paper.pdf",)


def test_search_budget_still_processes_already_discovered_candidates(project):
    result, _, search, *_ = run_review(project, max_searches=1)
    assert result.status == "partial" and result.searches == len(search.calls) == 1
    assert result.claim_ids and "search budget" in result.stop_reason


def test_crossref_adapter_preserves_dates_cursors_references_and_abstract_origin():
    from urllib.parse import parse_qs, urlsplit
    class API:
        def get(self, url, **kwargs):
            self.url = url
            return Fetched(url, "application/json", json.dumps({"message": {"items": [{
                "title": ["Paper"], "DOI": "10.123/a", "published": {"date-parts": [[2025]]},
                "abstract": "<jats:p>Original abstract.</jats:p>", "reference": [{"DOI": "10.123/b"}],
                "link": [{"URL": "https://example.com/paper.pdf"}]
            }], "next-cursor": "next", "total-results": 10}}).encode())
    api = API()
    page, raw = LiteratureSearch(api).search("crossref", "paper", cursor="previous", limit=1, year_from=2020, year_to=2025)
    params = parse_qs(urlsplit(api.url).query)
    assert params["query.title"] == ["paper"] and "query.bibliographic" not in params
    assert params["cursor"] == ["previous"] and "from-pub-date:2020-01-01" in params["filter"][0]
    assert page.next_cursor == "next" and page.candidates[0].references == ["10.123/b"]
    assert page.candidates[0].abstract == "Original abstract."
    assert page.candidates[0].abstract_source_sha256 == page.raw_sha256


def test_crossref_initial_search_uses_relevance_ranking_without_star_cursor():
    from urllib.parse import parse_qs, urlsplit
    class API:
        def get(self, url, **kwargs):
            self.url = url
            return Fetched(url, "application/json", json.dumps({"message": {
                "items": [{"title": ["Giant Radio Galaxies"], "DOI": "10.123/grg"}],
                "total-results": 1,
            }}).encode())

    api = API()
    page, _ = LiteratureSearch(api).search("crossref", "giant radio galaxies", limit=6)
    params = parse_qs(urlsplit(api.url).query)
    assert params["query.title"] == ["giant radio galaxies"]
    assert "cursor" not in params
    assert page.candidates[0].title == "Giant Radio Galaxies"


def test_literature_model_dependency_request_falls_back_to_safe_runtime_actions():
    from ar.literature import ReviewSpec, _ReviewExecution

    spec = ReviewSpec(id="fallback", question="What do giant radio galaxies show?",
                      backends=["crossref", "europe_pmc"])
    plan = _ReviewExecution._fallback_action(
        spec, "plan", {}, [], ["Need full text before proceeding."])
    assert {query.backend for query in plan.queries} == {"crossref", "europe_pmc"}
    assert any(query.intent == "contradictory" for query in plan.queries)
    assert plan.gaps == ["Need full text before proceeding."]

    screened = _ReviewExecution._fallback_action(spec, "screen", {"candidates": [
        {"id": "with-abstract", "abstract": "Indexed abstract"},
        {"id": "metadata-only", "abstract": ""},
    ]}, [], ["Need more context."])
    assert [(item.candidate_id, item.decision, item.basis) for item in screened.screening] == [
        ("with-abstract", "uncertain", "abstract"),
        ("metadata-only", "uncertain", "metadata"),
    ]


def test_europe_pmc_adapter_exposes_open_access_xml_and_never_promotes_snippets():
    class API:
        def get(self, url, **kwargs):
            return Fetched(url, "application/json", json.dumps({"resultList": {"result": [{
                "title": "Paper", "id": "123", "source": "MED", "pmcid": "PMC123", "isOpenAccess": "Y",
                "abstractText": "<p>Original abstract.</p>", "doi": "10.123/a", "pubYear": "2024"
            }]}, "hitCount": 1}).encode())
    page, _ = LiteratureSearch(API()).search("europe_pmc", "paper")
    hit = page.candidates[0]
    assert hit.document_urls == ["https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML"]
    assert hit.abstract == "Original abstract." and hit.year == 2024


def test_openalex_adapter_reconstructs_abstract_and_exposes_open_access_pdf():
    from urllib.parse import parse_qs, urlsplit
    class API:
        def get(self, url, **kwargs):
            self.url = url
            return Fetched(url, "application/json", json.dumps({
                "meta": {"count": 1},
                "results": [{
                    "id": "https://openalex.org/W1", "doi": "https://doi.org/10.123/grg",
                    "title": "Giant radio galaxies in deep fields", "publication_year": 2025,
                    "authorships": [{"author": {"display_name": "A. Astronomer"}}],
                    "abstract_inverted_index": {"Giant": [0], "radio": [1], "galaxies": [2]},
                    "primary_location": {"landing_page_url": "https://example.org/work", "pdf_url": None},
                    "open_access": {"oa_url": "https://arxiv.org/pdf/1234.5678"}, "type": "article",
                }],
            }).encode())

    api = API()
    page, _ = LiteratureSearch(api).search("openalex", "giant radio galaxies", limit=5)
    params = parse_qs(urlsplit(api.url).query)
    assert params["search"] == ["giant radio galaxies"] and "cursor" not in params
    hit = page.candidates[0]
    assert hit.abstract == "Giant radio galaxies" and hit.doi == "10.123/grg"
    assert hit.authors == "A. Astronomer" and hit.document_urls == ["https://arxiv.org/pdf/1234.5678"]


def test_brave_adapter_uses_a_header_for_the_key_and_keeps_snippets_separate(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-secret")
    class API:
        def get(self, url, **kwargs):
            self.url, self.headers = url, kwargs["headers"]
            return Fetched(url, "application/json", b'{"web":{"results":[{"title":"Paper","url":"https://example.com/paper","description":"A snippet"}]}}')
    api = API()
    page, _ = LiteratureSearch(api).search("brave", "paper")
    assert api.headers["X-Subscription-Token"] == "test-secret" and "test-secret" not in api.url
    assert page.candidates[0].snippet == "A snippet" and page.candidates[0].abstract == ""


def test_redirects_revalidate_destinations_and_strip_credentials(monkeypatch):
    import http.client
    requests = []
    destinations = ["https://other.example/document"]
    class Response:
        status = 302
        def getheader(self, name, default=None):
            return destinations[0] if name == "Location" else default
    class OK:
        status = 200
        def getheader(self, name, default=None):
            return "text/plain" if name == "Content-Type" else default
        def read(self, size):
            return b"public document"
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, method, path, headers):
            requests.append(headers)
        def getresponse(self):
            return Response() if len(requests) == 1 else OK()
        def close(self):
            pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    fetched = PublicWeb().get("https://first.example/doc", headers={"X-Subscription-Token": "secret"})
    assert fetched.body == b"public document"
    assert "X-Subscription-Token" not in requests[1]
    requests.clear()
    destinations[0] = "http://169.254.169.254/metadata"
    with pytest.raises(RetrievalError, match="public"):
        PublicWeb().get("https://first.example/doc")


def test_scanned_pdf_is_not_misreported_as_readable_text():
    import io
    from pypdf import PdfWriter
    writer, stream = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    with pytest.raises(RetrievalError, match="OCR"):
        extract_document(Fetched("https://example.com/paper.pdf", "application/pdf", stream.getvalue()))
