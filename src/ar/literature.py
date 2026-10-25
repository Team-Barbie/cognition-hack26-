"""Resumable, bounded literature-review instances with an auditable search ledger.

Each specification has its own namespace, budget, decisions and evidence membership.
Multiple instances can run in one project; SQLite leases prevent duplicate execution
of the same instance. Neither search snippets nor model-authored notes become evidence.
"""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

import yaml
from pydantic import Field, field_validator, model_validator

from ar import db
from ar.agents.literature_review import ReviewAction, SearchQuery
from ar.broker import Broker
from ar.evidence import CITATION, accept_claims, get_claim
from ar.literature_search import Candidate, LiteratureSearch, SearchPage
from ar.models import AgentResult, ClaimDraft, Record, SourceRef
from ar.packet import DependencyItem, Packet
from ar.permissions import load_policy
from ar.retrieval import PublicWeb, RetrievalError, extract_document
from ar.snapshots import atomic_write, sha256_text


class ReviewSpec(Record):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$")
    question: str = Field(min_length=1, max_length=2000)
    subquestions: list[str] = Field(default_factory=list, max_length=12)
    inclusion: list[str] = Field(default_factory=list, max_length=20)
    exclusion: list[str] = Field(default_factory=list, max_length=20)
    seed_dois: list[str] = Field(default_factory=list, max_length=6)
    backends: list[Literal["crossref", "europe_pmc", "brave"]] = Field(default_factory=lambda: ["crossref", "europe_pmc"], min_length=1)
    year_from: int | None = Field(default=None, ge=1000, le=9999)
    year_to: int | None = Field(default=None, ge=1000, le=9999)
    max_searches: int = Field(default=8, ge=1, le=60)
    max_model_calls: int = Field(default=24, ge=0, le=200)
    max_documents: int = Field(default=8, ge=1, le=40)
    results_per_search: int = Field(default=8, ge=1, le=20)
    pages_per_query: int = Field(default=1, ge=1, le=5)
    max_rounds: int = Field(default=2, ge=1, le=5)
    max_seconds: int = Field(default=240, ge=10, le=900)
    max_download_bytes: int = Field(default=5_000_000, ge=10_000, le=15_000_000)
    lines_per_read: int = Field(default=100, ge=10, le=250)
    max_read_chunks: int = Field(default=16, ge=1, le=100)

    @field_validator("question", "subquestions")
    @classmethod
    def clean_questions(cls, value):
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("question cannot be blank")
            return value.strip()
        if any(not question.strip() or len(question) > 2000 for question in value):
            raise ValueError("subquestions must be nonempty and at most 2000 characters")
        return list(dict.fromkeys(question.strip() for question in value))

    @model_validator(mode="after")
    def dates(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")
        self.backends = list(dict.fromkeys(self.backends))
        self.seed_dois = list(dict.fromkeys(doi.strip().lower() for doi in self.seed_dois))
        if any(not doi.startswith("10.") or "/" not in doi or len(doi) > 240 or any(c.isspace() for c in doi)
               for doi in self.seed_dois):
            raise ValueError("seed_dois must contain valid DOI identifiers")
        if self.seed_dois and "crossref" not in self.backends:
            raise ValueError("selected papers require the crossref backend")
        return self

    @property
    def questions(self) -> list[str]:
        return self.subquestions or [self.question]

    @property
    def key(self) -> str:
        protocol = self.model_dump()
        if not self.seed_dois:
            protocol.pop("seed_dois")  # Preserve journals created before paper selection existed.
        fingerprint = sha256_text(json.dumps(protocol, sort_keys=True))[:16]
        return f"{self.id}-{fingerprint}"


class ReviewConfig(Record):
    reviews: list[ReviewSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({review.id for review in self.reviews}) != len(self.reviews):
            raise ValueError("each literature review must have a unique id")
        return self


def load_reviews(root: Path) -> list[ReviewSpec]:
    path = root / "input" / "literature.yaml"
    return ReviewConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).reviews if path.is_file() else []


class ReviewResult(Record):
    id: str
    key: str
    question: str
    status: Literal["finished", "partial", "failed"]
    stop_reason: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    coverage: dict[str, list[str]] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    summary_markdown: str = ""
    model_calls: int = 0
    searches: int = 0
    documents: int = 0
    fetches: int = 0
    read_chunks: int = 0
    report_path: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS literature_jobs (
    key TEXT PRIMARY KEY, review_id TEXT NOT NULL, spec_json TEXT NOT NULL,
    status TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, searches INTEGER NOT NULL DEFAULT 0,
    documents INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
    owner TEXT, lease_until REAL, result_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS literature_operations (
    job_key TEXT NOT NULL, op TEXT NOT NULL, state TEXT NOT NULL, data_json TEXT,
    PRIMARY KEY(job_key, op)
);
CREATE TABLE IF NOT EXISTS literature_candidates (
    job_key TEXT NOT NULL, id TEXT NOT NULL, data_json TEXT NOT NULL,
    decision TEXT, reason TEXT, basis TEXT, source_id TEXT,
    PRIMARY KEY(job_key, id)
);
CREATE TABLE IF NOT EXISTS literature_sources (
    source_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL
);
"""


class _Stop(RuntimeError):
    pass


class _ReviewExecution:
    """One independently reusable agent instance; no process-global mutable state.

    `agent` implements the ordinary run(role, packet) protocol. Search/fetch can be
    injected for reproducible offline evaluations; production defaults use real APIs.
    """
    def __init__(self, root: Path, agent, *, search: LiteratureSearch | None = None,
                 web: PublicWeb | None = None, on_event: Callable[[dict], None] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.root, self.agent = Path(root).resolve(), agent
        self.web = web or PublicWeb()
        self.search = search or LiteratureSearch(self.web)
        self.on_event = on_event
        self.should_stop = should_stop

    def run(self, spec: ReviewSpec, *, call_limit: int | None = None) -> ReviewResult:
        self.call_limit = spec.max_model_calls
        conn = db.connect(self.root)
        conn.executescript(SCHEMA)
        policy_path = self.root / "policies" / "permissions.yaml"
        broker = Broker(conn, self.root, load_policy(policy_path) if policy_path.is_file() else None)
        owner = uuid4().hex
        acquired = False
        terminal = False
        result = ReviewResult(id=spec.id, key=spec.key, question=spec.question, status="partial",
                              stop_reason="unfinished", coverage={q: [] for q in spec.questions})
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM literature_jobs WHERE key=?", (spec.key,)).fetchone()
                if call_limit is not None:
                    self.call_limit = min(spec.max_model_calls, (row["calls"] if row else 0) + max(0, call_limit))
                if row and row["result_json"]:
                    return ReviewResult.model_validate_json(row["result_json"])
                if row and row["owner"] and row["lease_until"] > time.time():
                    raise RuntimeError(f"literature review {spec.id} is already running")
                if row is None:
                    conn.execute("INSERT INTO literature_jobs(key,review_id,spec_json,status,created_at) VALUES(?,?,?,?,?)",
                                 (spec.key, spec.id, spec.model_dump_json(), "running", datetime.now(UTC).isoformat()))
                conn.execute("UPDATE literature_jobs SET owner=?,lease_until=?,status='running' WHERE key=?",
                             (owner, time.time() + spec.max_seconds + 180, spec.key))
            acquired = True
            self._execute(conn, broker, spec, result)
            terminal = True
        except _Stop as exc:
            result.stop_reason = str(exc)
            result.limitations.append(str(exc))
            terminal = True
        except (RetrievalError, ValueError) as exc:
            result.status, result.stop_reason = "failed", str(exc)
            result.limitations.append(str(exc))
            terminal = True
        finally:
            if acquired and terminal:
                try:
                    counts = conn.execute("SELECT * FROM literature_jobs WHERE key=?", (spec.key,)).fetchone()
                    result.model_calls, result.searches = counts["calls"], counts["searches"]
                    result.fetches, result.read_chunks = counts["documents"], counts["chunks"]
                    # Restore evidence membership even after a controlled stop during a round.
                    refs = conn.execute("SELECT DISTINCT source_id FROM literature_candidates WHERE job_key=? AND source_id IS NOT NULL",
                                        (spec.key,)).fetchall()
                    result.source_refs = [SourceRef(source_id=r[0]) for r in refs]
                    result.documents = len(result.source_refs)
                    prefix = f"lit:{spec.key}:read:"
                    result.claim_ids = [r[0] for r in conn.execute("SELECT id FROM claims WHERE substr(origin_task,1,?)=? ORDER BY rowid",
                                                                  (len(prefix), prefix))]
                    result.limitations = list(dict.fromkeys(result.limitations))
                    result.report_path = str(self._export(conn, broker, spec, result).relative_to(self.root))
                    with conn:
                        conn.execute("UPDATE literature_jobs SET status=?,result_json=?,owner=NULL,lease_until=NULL WHERE key=? AND owner=?",
                                     (result.status, result.model_dump_json(), spec.key, owner))
                    self._event(conn, spec, "finished", result=result.model_dump(mode="json"))
                finally:
                    with conn:
                        conn.execute("UPDATE literature_jobs SET owner=NULL,lease_until=NULL WHERE key=? AND owner=?", (spec.key, owner))
            if acquired and not terminal:
                with conn:
                    conn.execute("UPDATE literature_jobs SET status='interrupted',owner=NULL,lease_until=NULL WHERE key=? AND owner=?", (spec.key, owner))
            conn.close()
        return result

    def _event(self, conn, spec, phase, **data):
        event = {"review_id": spec.id, "key": spec.key, "phase": phase, **data}
        with conn:
            conn.execute("INSERT INTO events(kind,payload_json,created_at) VALUES(?,?,?)",
                         ("literature_review", json.dumps(event), datetime.now(UTC).isoformat()))
        if self.on_event:
            self.on_event(event)

    def _operation(self, conn, spec, op, category, perform):
        if self.should_stop and self.should_stop():
            raise InterruptedError("literature review interrupted before the next operation")
        cached = conn.execute("SELECT state,data_json FROM literature_operations WHERE job_key=? AND op=?", (spec.key, op)).fetchone()
        if cached:
            if cached[0] == "started":
                raise _Stop(f"operation {op} has an unknown outcome after interruption; create a new review id to retry")
            return json.loads(cached[1])
        if time.monotonic() > self.deadline:
            raise _Stop("review time budget exhausted")
        column, limit = {"model": ("calls", self.call_limit), "search": ("searches", spec.max_searches),
                         "fetch": ("documents", spec.max_documents * 3), "read": ("chunks", spec.max_read_chunks)}[category]
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute(f"SELECT {column} FROM literature_jobs WHERE key=?", (spec.key,)).fetchone()[0]
            if used >= limit:
                raise _Stop(f"review {category} budget exhausted")
            conn.execute(f"UPDATE literature_jobs SET {column}={column}+1 WHERE key=?", (spec.key,))
            conn.execute("INSERT INTO literature_operations VALUES(?,?,?,NULL)", (spec.key, op, "started"))
        # Charge before dispatch. Interrupted operations cannot silently spend again on resume.
        try:
            data = perform()
        except _Stop:
            raise
        except Exception as exc:
            data = {"error": f"{type(exc).__name__}: {exc}"}
        with conn:
            conn.execute("UPDATE literature_operations SET state='finished',data_json=? WHERE job_key=? AND op=?",
                         (json.dumps(data), spec.key, op))
        return data

    def _model(self, conn, broker, spec, op, phase, context=None, dependencies=()):
        context = dict(context or {})
        for attempt in range(2):
            try:
                action = self._model_once(conn, broker, spec, op if attempt == 0 else op + ":repair",
                                          phase, context, dependencies)
                if phase == "plan":
                    self._queries(spec, action, planning=True)
                elif phase == "screen":
                    candidates = {candidate["id"]: candidate for candidate in context["candidates"]}
                    ids = [decision.candidate_id for decision in action.screening]
                    if len(ids) != len(set(ids)) or set(ids) != set(candidates):
                        raise ValueError("screening must decide every supplied candidate exactly once")
                    if any(decision.basis == "abstract" and not candidates[decision.candidate_id]["abstract"]
                           for decision in action.screening):
                        raise ValueError("screening claimed to read an unavailable abstract")
                else:
                    allowed = {dep.id for dep in dependencies}
                    if set(action.coverage) != set(spec.questions) or any(set(ids) - allowed for ids in action.coverage.values()):
                        raise ValueError("coverage must map every subquestion to only this review's accepted claim IDs")
                    self._queries(spec, action)
                    if phase == "synthesize":
                        if not action.summary_markdown.strip():
                            raise ValueError("literature synthesis must contain a summary")
                        if set(CITATION.findall(action.summary_markdown)) - allowed:
                            raise ValueError("literature synthesis cites a claim outside this review")
                return action
            except ValueError as exc:
                if attempt:
                    raise
                context["validation_feedback"] = str(exc)[:2000]
        raise AssertionError("unreachable")

    def _model_once(self, conn, broker, spec, op, phase, context=None, dependencies=()):
        task = f"lit:{spec.key}:{op}"
        packet = Packet(packet_id=f"packet:{task}", task_id=task, attempt_id=f"attempt:{task}",
                        role="literature_review", objective=spec.question, model_call_budget=1,
                        skills=["literature_review"], dependencies=list(dependencies),
                        review_context={"phase": phase, "protocol": spec.model_dump(mode="json"),
                                        "subquestions": spec.questions, **(context or {})})
        if phase in {"reflect", "synthesize"}:
            studies = []
            for row in conn.execute("SELECT data_json FROM literature_operations WHERE job_key=? AND op LIKE 'model:read:%' AND state='finished'", (spec.key,)):
                data = json.loads(row[0])
                payload = data.get("payload", {})
                if payload.get("study"):
                    studies.append({"source_id": payload.get("source_id"), "study": payload["study"]})
            packet.review_context["study_characteristics"] = studies[:spec.max_documents]
            overview = []
            for row in conn.execute("SELECT data_json,source_id FROM literature_candidates WHERE job_key=? AND source_id IS NOT NULL", (spec.key,)):
                candidate = json.loads(row[0])
                overview.append({key: candidate.get(key) for key in ("title", "authors", "year", "doi", "publication_type")} | {"source_id": row[1]})
            packet.review_context["source_overview"] = overview[:spec.max_documents]
        self._event(conn, spec, phase, task=task, packet=packet.audit_view())
        data = self._operation(conn, spec, op, "model", lambda: self.agent.run("literature_review", packet).model_dump(mode="json"))
        if "error" in data:
            raise _Stop(f"review model call failed: {data['error']}")
        response = AgentResult.model_validate(data)
        if response.role != "literature_review" or response.task_id != task:
            raise ValueError("review response does not belong to this task")
        if response.model_calls > 1:
            raise _Stop("agent exceeded the per-dispatch model allowance")
        if response.outcome != "produced":
            if any("model reply" in message for message in response.unresolved):
                raise ValueError("; ".join(response.unresolved))
            raise _Stop("review model could not complete: " + "; ".join(response.unresolved))
        return ReviewAction.model_validate(response.payload)

    @staticmethod
    def _queries(spec, action, *, planning=False):
        if planning and not action.queries:
            raise ValueError("search plan contains no queries")
        for query in action.queries:
            if query.backend not in spec.backends or query.question not in spec.questions:
                raise ValueError("search plan references an unconfigured backend or subquestion")
        if planning:
            if set(q.question for q in action.queries) != set(spec.questions):
                raise ValueError("search plan must cover every subquestion")
            if not any(q.intent == "contradictory" for q in action.queries):
                raise ValueError("search plan must include a contradictory-evidence query")
            if {q.backend for q in action.queries} != set(spec.backends):
                raise ValueError("search plan must use each configured backend")
        return action.queries

    def _execute(self, conn, broker, spec, result):
        self.deadline = time.monotonic() + spec.max_seconds
        for cap in ("network.search", "network.fetch", "files.read"):
            broker.check_capability("literature_review", [cap], cap)
        plan = self._model(conn, broker, spec, "plan", "plan")
        queries = self._queries(spec, plan, planning=True)
        # Resolve the user's exact selections before broader discovery. Metadata still
        # passes screening, acquisition and receipt-backed reading before becoming evidence.
        queries = [SearchQuery(query=doi, backend="crossref", question=spec.questions[0], intent="citation")
                   for doi in spec.seed_dois] + list(queries)
        known: dict[str, Candidate] = {}
        source_refs: list[SourceRef] = []
        claims: list[str] = []
        chunks_used = 0
        search_exhausted = False
        for round_no in range(spec.max_rounds):
            pending = deque((q, None, 1) for q in queries)
            while pending:
                query, cursor, page_no = pending.popleft()
                op = "search:" + sha256_text(query.model_dump_json() + str(cursor))[:24]

                def do_search(query=query, cursor=cursor):
                    broker.check_capability("literature_review", ["network.search"], "network.search")
                    page, raw = self.search.search(query.backend, query.query, cursor=cursor, limit=spec.results_per_search,
                                                  year_from=spec.year_from, year_to=spec.year_to,
                                                  **({"doi": query.query} if query.backend == "crossref" and query.query in spec.seed_dois else {}))
                    broker.blobs.put(raw.body)
                    return {"query": query.model_dump(), "cursor": cursor, "page": page.model_dump(mode="json"),
                            "retrieved_at": datetime.now(UTC).isoformat()}

                try:
                    data = self._operation(conn, spec, op, "search", do_search)
                except _Stop as exc:
                    if "search budget" not in str(exc):
                        raise
                    search_exhausted = True
                    result.limitations.append(str(exc) + "; some planned queries were not executed.")
                    pending.clear()
                    break
                if "error" in data:
                    result.limitations.append(f"Search failed on {query.backend} for {query.query!r}: {data['error']}")
                    continue
                page = SearchPage.model_validate(data["page"])
                if query.backend == "crossref" and query.query in spec.seed_dois:
                    page.candidates = [hit for hit in page.candidates if hit.doi.lower().strip() == query.query]
                    page.next_cursor = None
                    if not page.candidates:
                        result.limitations.append(f"Selected paper {query.query} could not be resolved in Crossref.")
                for hit in page.candidates:
                    if hit.id in known:
                        # Enrich metadata-only duplicates with an abstract / open-access link.
                        old = known[hit.id]
                        old.abstract = old.abstract or hit.abstract
                        old.abstract_source_sha256 = old.abstract_source_sha256 or hit.abstract_source_sha256
                        old.document_urls = list(dict.fromkeys(old.document_urls + hit.document_urls))
                    else:
                        known[hit.id] = hit
                if page.next_cursor and page.next_cursor != cursor:
                    if page_no < spec.pages_per_query:
                        pending.append((query, page.next_cursor, page_no + 1))
                    else:
                        result.limitations.append(f"Additional result pages were not searched for {query.query!r} ({query.backend}).")
                self._event(conn, spec, "search", backend=query.backend, query=query.query, found=len(page.candidates))

            # Screening batches keep prompts bounded even when many query variants return results.
            unscreened = [hit for hit in known.values() if not conn.execute(
                "SELECT decision FROM literature_candidates WHERE job_key=? AND id=?", (spec.key, hit.id)).fetchone()]
            for start in range(0, len(unscreened), 15):
                batch = unscreened[start:start + 15]
                views = [{**h.model_dump(), "abstract": h.abstract[:12000], "snippet": h.snippet[:2000],
                          "abstract_truncated_for_screening": len(h.abstract) > 12000} for h in batch]
                action = self._model(conn, broker, spec, f"screen:{round_no}:{start}", "screen",
                                     {"candidates": views})
                ids = [screen.candidate_id for screen in action.screening]
                if len(ids) != len(set(ids)) or set(ids) != {h.id for h in batch}:
                    raise ValueError("screening must decide every supplied candidate exactly once")
                decisions = {screen.candidate_id: screen for screen in action.screening}
                for hit in batch:
                    decision = decisions[hit.id]
                    if decision.basis == "abstract" and not hit.abstract:
                        raise ValueError("screening claimed to read an unavailable abstract")
                    outside = hit.year and ((spec.year_from and hit.year < spec.year_from) or (spec.year_to and hit.year > spec.year_to))
                    if outside:
                        decision.decision, decision.reason = "exclude", "Outside the configured publication date range."
                    with conn:
                        conn.execute("INSERT INTO literature_candidates VALUES(?,?,?,?,?,?,NULL)",
                                     (spec.key, hit.id, hit.model_dump_json(), decision.decision, decision.reason, decision.basis))

            # Resume every included/uncertain candidate, including those screened before an interruption.
            rows = conn.execute("SELECT * FROM literature_candidates WHERE job_key=? AND decision!='exclude' ORDER BY rowid", (spec.key,)).fetchall()
            for row in rows:
                if row["source_id"] and any(ref.source_id == row["source_id"] for ref in source_refs):
                    continue
                hit = known.get(row["id"], Candidate.model_validate_json(row["data_json"]))
                if len(source_refs) >= spec.max_documents:
                    result.limitations.append("Document budget left included candidates unread.")
                    break
                ref, kind = self._acquire(conn, broker, spec, hit, result)
                if ref is None:
                    continue
                if ref in source_refs:
                    continue
                source_refs.append(ref)
                count = broker.source_line_count(ref)
                for lo in range(1, count + 1, spec.lines_per_read):
                    if chunks_used >= spec.max_read_chunks:
                        result.limitations.append("Read budget left some document lines unread.")
                        break
                    op = f"read:{hit.id}:{lo}"
                    task, attempt = f"lit:{spec.key}:{op}", f"attempt:lit:{spec.key}:{op}"

                    def read_claims(lo=lo, task=task, attempt=attempt, ref=ref, hit=hit, kind=kind):
                        broker.check_capability("literature_review", ["files.read"], "files.read")
                        read = broker.read(ref=ref, start=lo, end=min(count, lo + spec.lines_per_read - 1),
                                           task_id=task, attempt_id=attempt, packet_id=f"packet:{task}", label=hit.title)
                        packet = Packet(packet_id=f"packet:{task}", task_id=task, attempt_id=attempt,
                                        role="evidence_reader", objective=spec.question, reads=[read], model_call_budget=1,
                                        skills=["literature_extraction"], review_context={"document_kind": kind, "review_id": spec.id})
                        response_data = self._operation(conn, spec, "model:" + op, "model",
                                                        lambda: self.agent.run("evidence_reader", packet).model_dump(mode="json"))
                        if "error" in response_data:
                            return response_data
                        response = AgentResult.model_validate(response_data)
                        if response.model_calls > 1:
                            raise _Stop("reader exceeded the per-dispatch model allowance")
                        if response.role != "evidence_reader" or response.task_id != task:
                            raise ValueError("reader response does not belong to this task")
                        if response.outcome != "produced":
                            return {"error": "reader could not extract evidence"}
                        for draft in response.claims:
                            if kind != "full_text":
                                draft.limitations.append(f"Evidence came from {kind}; full paper reading is not established.")
                        # The reader is confined to this source and this delivered window by the broker.
                        accepted = accept_claims(conn, broker, task_id=task, attempt_id=attempt, drafts=response.claims)
                        return {"claim_ids": [claim.id for claim in accepted.accepted],
                                "rejected": len(accepted.rejected), "source_id": ref.source_id}

                    data = self._operation(conn, spec, op, "read", read_claims)
                    chunks_used += 1
                    if "error" in data:
                        result.limitations.append(f"Could not read {hit.title}: {data['error']}")
                    else:
                        claims.extend(cid for cid in data["claim_ids"] if cid not in claims)
                        if data["rejected"]:
                            result.limitations.append(f"{data['rejected']} proposed claims from {hit.title} failed evidence validation.")

            deps = self._dependencies(conn, claims)
            reflection = self._model(conn, broker, spec, f"reflect:{round_no}", "reflect",
                                     {"limitations": result.limitations, "searched_candidates": len(known),
                                      "citation_candidates": [{"title": hit.title, "doi": hit.doi, "references": hit.references}
                                                              for hit in list(known.values())[:15]]}, deps)
            self._coverage(spec, reflection, claims, result)
            queries = self._queries(spec, reflection)
            if search_exhausted:
                result.stop_reason = "review search budget exhausted"
                break
            if not queries:
                result.status, result.stop_reason = "finished", "bounded search strategy exhausted; completeness is not guaranteed"
                break
            if round_no == spec.max_rounds - 1:
                result.limitations.append("Follow-up queries remain after the configured round limit.")
                result.stop_reason = "review round budget exhausted"
        result.source_refs, result.claim_ids = source_refs, claims
        if not claims:
            result.status, result.stop_reason = "partial", "no source-supported claims were accepted"
        synthesis = self._model(conn, broker, spec, "synthesize", "synthesize",
                               {"limitations": result.limitations, "stop_reason": result.stop_reason}, self._dependencies(conn, claims))
        self._coverage(spec, synthesis, claims, result)
        if set(CITATION.findall(synthesis.summary_markdown)) - set(claims):
            raise ValueError("literature synthesis cites a claim outside this review")
        result.summary_markdown = synthesis.summary_markdown
        if any(not ids for ids in result.coverage.values()) or result.limitations:
            result.status = "partial"

    @staticmethod
    def _dependencies(conn, ids):
        return [DependencyItem(kind="claim", id=cid, claim=ClaimDraft.model_validate(
                    get_claim(conn, cid).model_dump(exclude={"id", "origin_task"}))) for cid in ids]

    @staticmethod
    def _coverage(spec, action, claims, result):
        if set(action.coverage) != set(spec.questions) or any(set(ids) - set(claims) for ids in action.coverage.values()):
            raise ValueError("coverage must map every subquestion to only this review's accepted claim IDs")
        result.coverage = action.coverage
        result.limitations.extend(action.gaps)

    def _acquire(self, conn, broker, spec, hit, result):
        op = f"acquire:{hit.id}"
        saved = conn.execute("SELECT data_json FROM literature_operations WHERE job_key=? AND op=? AND state='finished'", (spec.key, op)).fetchone()
        if saved:
            data = json.loads(saved[0])
            return (SourceRef(source_id=data["source_id"]), data["kind"]) if "source_id" in data else (None, "")
        found = None
        urls = deque(dict.fromkeys([*hit.document_urls, hit.url]))
        seen = set()
        while urls and len(seen) < 3:
            url = urls.popleft()
            if not url or url in seen:
                continue
            seen.add(url)
            broker.check_capability("literature_review", ["network.fetch"], "network.fetch")

            def fetch_one(url=url):
                raw = self.web.get(url, max_bytes=spec.max_download_bytes, timeout=min(20, max(1, self.deadline - time.monotonic())))
                extracted = extract_document(raw)
                return {"raw_sha256": broker.blobs.put(raw.body), "url": raw.url, "content_type": raw.content_type,
                        "text": extracted.text, "kind": extracted.kind, "parser": extracted.parser,
                        "links": list(extracted.citation_urls), "retrieved_at": datetime.now(UTC).isoformat()}

            data = self._operation(conn, spec, "fetch:" + hit.id + ":" + sha256_text(url)[:12], "fetch", fetch_one)
            if "error" in data:
                result.limitations.append(f"Could not acquire {hit.title}: {data['error']}")
                continue
            if data["kind"] == "web_page" and data["links"]:
                urls.extendleft(reversed(data["links"][:2]))
            if data["text"].strip() and (found is None or data["kind"] == "full_text"):
                found = data
            if data["kind"] == "full_text":
                break
        if (found is None or found["kind"] == "web_page") and hit.abstract:
            # Actual indexed abstract, never the discovery snippet or a model summary.
            found = {"text": hit.abstract, "kind": "abstract", "parser": "indexed-abstract", "url": hit.url,
                     "raw_sha256": hit.abstract_source_sha256, "content_type": "text/plain", "retrieved_at": datetime.now(UTC).isoformat()}
        if found is None:
            with conn:
                conn.execute("INSERT OR REPLACE INTO literature_operations VALUES(?,?,?,?)", (spec.key, op, "finished", '{"unavailable":true}'))
            result.limitations.append(f"No readable text or abstract was available for {hit.title}.")
            return None, ""
        text = found.pop("text").replace("\r\n", "\n").replace("\r", "\n")
        if len(text.encode()) > spec.max_download_bytes:
            raise _Stop("extracted text exceeds the document size budget")
        source_id = "lit-" + sha256_text(hit.id + text)[:24]
        target = self.root / ".research" / "literature" / "sources" / f"{source_id}.txt"
        atomic_write(target, text.encode("utf-8"))
        ref = broker.register_source(source_id, target)
        metadata = {**found, "candidate": hit.model_dump(), "source_id": source_id,
                    "representation_sha256": sha256_text(text), "provenance": "runtime_retrieved"}
        with conn:
            conn.execute("INSERT OR IGNORE INTO literature_sources VALUES(?,?)", (source_id, json.dumps(metadata)))
            conn.execute("UPDATE literature_candidates SET source_id=? WHERE job_key=? AND id=?", (source_id, spec.key, hit.id))
            conn.execute("INSERT OR REPLACE INTO literature_operations VALUES(?,?,?,?)", (spec.key, op, "finished", json.dumps({"source_id": source_id, "kind": found["kind"]})))
        if found["kind"] != "full_text":
            result.limitations.append(f"{hit.title} was read as {found['kind']}; full-text access was not established.")
        self._event(conn, spec, "acquired", source_id=source_id, title=hit.title, kind=found["kind"])
        return ref, found["kind"]

    def _export(self, conn, broker, spec, result):
        folder = self.root / "output" / "literature" / spec.key
        folder.mkdir(parents=True, exist_ok=True)
        result.report_path = str((folder / "report.md").relative_to(self.root))
        rows = conn.execute("SELECT * FROM literature_candidates WHERE job_key=? ORDER BY rowid", (spec.key,)).fetchall()
        operations = conn.execute("SELECT op,state,data_json FROM literature_operations WHERE job_key=? ORDER BY rowid", (spec.key,)).fetchall()
        sources = [json.loads(row[0]) for ref in result.source_refs for row in conn.execute(
            "SELECT metadata_json FROM literature_sources WHERE source_id=?", (ref.source_id,))]
        ledger = {"protocol": spec.model_dump(), "result": result.model_dump(),
                  "candidates": [dict(row) | {"data_json": json.loads(row["data_json"])} for row in rows],
                  "operations": [dict(row) | {"data_json": json.loads(row["data_json"]) if row["data_json"] else None} for row in operations],
                  "sources": sources}
        lines = [f"# Literature review: {spec.id}", "", spec.question, "", f"Status: {result.status}",
                 f"Stopped: {result.stop_reason}", "", "> This is a bounded review. Search coverage is not proof of exhaustiveness.", "",
                 result.summary_markdown, "", "## Evidence", ""]
        for cid in result.claim_ids:
            claim = get_claim(conn, cid)
            lines += [f"- [claim:{cid}] {claim.statement}"]
            for link in claim.evidence_links:
                lines += [f"  - {link.source_ref.source_id} r{link.source_ref.revision}, lines {link.locator.start}-{link.locator.end}: {link.quote}"]
        lines += ["", "## Coverage", "", *[f"- {q}: {', '.join(ids) or 'no accepted evidence'}" for q, ids in result.coverage.items()],
                  "", "## Limitations", "", *[f"- {item}" for item in result.limitations]]
        lines += ["", "## Retrieved sources", ""]
        for source in sources:
            candidate = source["candidate"]
            lines += [f"- {candidate['title']} ({candidate.get('year') or 'date unknown'}). {candidate.get('authors', '')}",
                      f"  - {source['url']} — {source['kind']}; retrieved {source['retrieved_at']}; source {source['source_id']}"]
        target = folder / "report.md"
        atomic_write(target, ("\n".join(lines) + "\n").encode())
        atomic_write(folder / "search-ledger.json", json.dumps(ledger, indent=2, ensure_ascii=False).encode())
        return target


class LiteratureReviewAgent:
    """Reusable literature-review agent factory.

    Every run gets isolated execution state. Different ReviewSpec ids can execute
    concurrently through separate database connections; the same id/spec is leased.
    """
    def __init__(self, root: Path, agent, *, search: LiteratureSearch | None = None,
                 web: PublicWeb | None = None, on_event: Callable[[dict], None] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.root, self.agent = Path(root).resolve(), agent
        self.web = web or PublicWeb()
        self.search = search or LiteratureSearch(self.web)
        self.on_event = on_event
        self.should_stop = should_stop

    def run(self, spec: ReviewSpec, *, call_limit: int | None = None) -> ReviewResult:
        return _ReviewExecution(self.root, self.agent, search=self.search, web=self.web,
                                on_event=self.on_event, should_stop=self.should_stop).run(spec, call_limit=call_limit)


def list_reviews(conn) -> list[ReviewResult]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='literature_jobs'").fetchone():
        return []
    return [ReviewResult.model_validate_json(row[0]) for row in conn.execute("SELECT result_json FROM literature_jobs WHERE result_json IS NOT NULL ORDER BY rowid")]
