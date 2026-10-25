"""One studio session: intake chat, sources, a background research run, and its live event feed.

The run is the real runtime (`controller.run_project`) on a real project directory. The studio
only watches: it wraps the agent to report each step, and polls the database for claims,
rejections, and reviews as Quality commits them. The final report is read from committed state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ar.agents.fake import FakeAgent
from ar.agents.planner import ResearchPlan
from ar.evidence import CITATION
from ar.models import AgentResult, Role
from ar.literature import ReviewConfig, ReviewSpec
from ar.packet import Packet
from ar.studio.intake import Brief, Intake
from ar.web_search import search_crossref

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "baselines"
DEMO_BRIEF = Brief(
    question="Is a seasonal-naive forecast an adequate baseline for weekly demand series?",
    context="Retail demand forecasting; the answer decides which baseline a forecasting study must report.",
    constraints=["Series shorter than 52 weeks are excluded from the study."],
    open_questions=["Does the 52-week exclusion rule hold?"],
)
ROLE_TITLES = {
    "planner": "Planner",
    "source_scout": "Source scout",
    "evidence_reader": "Reader",
    "research_agent": "Research agent",
    "theorist": "Theorist",
    "experiment_runner": "Experiment runner",
    "reproducer": "Reproducer",
    "analyst": "Analyst",
    "synthesis_agent": "Synthesis",
    "adversarial_effect_agent": "Adversarial review",
    "writer": "Writer",
    "reviewer": "Reviewer",
}
MAX_SOURCE_BYTES = 400_000
SHOWCASE_STEP_DELAY = 1.15
PROMPT_DEMO_MODEL_CALLS = 24
PROMPT_DEMO_SECONDS = 180
SCOPE_NOTE = (
    "Scope note: research agents may make evidence claims only from supplied or retrieved document snapshots. "
    "Literature specialists acquire sources and provide accepted claims with access limitations. "
    "Metadata-only search candidates are not evidence. The agents cannot fetch images or data, "
    "and the user cannot supply more during the run. Never answer needs_dependency because data is missing: do your part "
    "with what the sources support. For any requirement that needs data work, say what the sources establish, "
    "label the rest UNVERIFIED, and list the missing data work as concrete next steps under Limitations."
)
RETRY_NUDGE = (
    "Your previous reply asked for inputs that cannot be supplied. Write the full report now from the claims given: "
    "cover every requirement with a section, state what the claims support, and put the missing data work under "
    "Limitations and Next steps."
)
ROLES_WITH_SCOPE = {"planner", "writer", "reviewer"}
EVIDENCE_SPECIALISTS = {"research_agent", "synthesis_agent"}
BOUNDED_RESEARCH_NOTE = (
    "Complete a preliminary assessment from the accepted claims and delivered source passages in this packet. "
    "Missing experiments, datasets, full papers or optional specialist outputs are limitations and next steps; "
    "they do not block this bounded task when usable evidence is present. Never invent evidence."
)
SCOPE_CONSTRAINT = (
    "Use only supplied or retrieved document snapshots as evidence. "
    "Answer what the sources support, and list any data work the request needs as concrete next steps."
)


class ResearchRunTimeout(RuntimeError):
    """The Studio's hosted run budget expired before another model call could start."""


def packet_view(packet: Packet) -> dict[str, Any]:
    """A JSON-safe audit view of the exact context dispatched to one agent attempt."""
    return packet.audit_view()


def _slug(name: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", name.strip())
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:48] or "source"


class StageAgent:
    """Wraps the real agent and reports each call to the session feed."""

    def __init__(self, inner, session: Session) -> None:
        self.inner = inner
        self.session = session
        self.writer_passes = 0

    def _apply_run_deadline(self) -> None:
        remaining = self.session.remaining_run_seconds
        if remaining is None:
            return
        if remaining <= 2:
            raise ResearchRunTimeout("research exceeded the hosted run time budget")
        # A role may use one call plus one schema-repair call. Keep either call from
        # consuming the entire request, so the run can finish or fail cleanly.
        client = getattr(self.inner, "client", None)
        if client is not None and hasattr(client, "timeout"):
            client.timeout = min(float(client.timeout), max(2.0, remaining / 2))

    def _card(self, role: Role, packet: Packet) -> tuple[str, str]:
        if role == "evidence_reader":
            label = packet.reads[0].label if packet.reads else "source"
            return f"reader:{label}", f"Reader {label}"
        if role == "writer":
            self.writer_passes += 1
            suffix = "" if self.writer_passes == 1 else f" rewrite {self.writer_passes - 1}"
            return f"writer:{self.writer_passes}", f"Writer{suffix}"
        return role, ROLE_TITLES.get(role, role)

    def _start_detail(self, role: Role, packet: Packet) -> str:
        if role == "planner":
            return f"Planning: {len(packet.requirements)} requirements, {len(packet.available_sources)} sources"
        if role == "evidence_reader" and packet.reads:
            r = packet.reads[0]
            return f"Reading lines {r.selector.start}-{r.selector.end} (receipt {r.read_receipt_id})"
        if role == "writer":
            n = len(packet.claim_items())
            extra = f", addressing {len(packet.review_feedback)} review findings" if packet.review_feedback else ""
            return f"Drafting from {n} claims{extra}"
        if role == "source_scout":
            return f"Screening {len(packet.available_sources)} candidate sources"
        if role == "theorist":
            return f"Building hypotheses from {len(packet.claim_items())} accepted claims"
        if role == "research_agent":
            return f"Integrating {len(packet.claim_items())} claims into a nine-check research assessment"
        if role in {"experiment_runner", "reproducer"}:
            return "Running the configured experiment" if role == "experiment_runner" else "Repeating the configured experiment"
        if role == "analyst":
            return "Interpreting recorded experiment outputs"
        if role == "synthesis_agent":
            return f"Synthesizing {len(packet.claim_items())} accepted claims and upstream research"
        if role == "adversarial_effect_agent":
            return "Challenging the research for overreach, counterarguments and missing tests"
        if role == "reviewer":
            return "Checking the draft"
        return "Working"

    def prepare_packet(self, role: Role, packet: Packet) -> Packet:
        """Apply Studio scope guidance before the controller journals the dispatch."""
        notes = []
        if role in ROLES_WITH_SCOPE:
            notes.append(SCOPE_NOTE)
        if role in EVIDENCE_SPECIALISTS:
            notes.append(BOUNDED_RESEARCH_NOTE)
        if role == "planner" and self.session.test_run:
            notes.append("This is a workflow test. Include research_agent and synthesis_agent after evidence reading, "
                         "then finish with writer and reviewer so their evidence handoffs are exercised.")
        return packet.model_copy(update={"objective": "\n\n".join([packet.objective, *notes])}) if notes else packet

    @staticmethod
    def _done_detail(role: Role, result: AgentResult) -> str:
        p = result.payload
        if result.outcome != "produced":
            return f"Stopped: {result.outcome.replace('_', ' ')}"
        if role == "planner":
            readers = sum(1 for t in result.proposed_tasks if t.role == "evidence_reader")
            return f"Proposed {len(result.proposed_tasks)} tasks ({readers} readers)"
        if role == "evidence_reader":
            if p.get("relevant") is False:
                return "Not relevant, no claims"
            return f"Proposed {len(result.claims)} claim{'s' if len(result.claims) != 1 else ''}"
        if role == "source_scout":
            screened = p.get("screened", [])
            return f"Screened {len(screened)} source{'s' if len(screened) != 1 else ''}"
        if role == "theorist":
            hypotheses = p.get("hypotheses", [])
            return f"Proposed {len(hypotheses)} testable hypothes{'is' if len(hypotheses) == 1 else 'es'}"
        if role == "research_agent":
            checks = p.get("checks", [])
            blocked = [c for c in checks if c.get("status") == "blocked"]
            return f"Completed {len(checks)} research checks" + (f", {len(blocked)} blocked" if blocked else "")
        if role in {"experiment_runner", "reproducer"}:
            record = p.get("analysis_record", {})
            return f"{record.get('run_id', 'Run')} {record.get('status', 'recorded')}"
        if role == "analyst":
            observations = p.get("observations", [])
            return f"Interpreted {len(observations)} observation{'s' if len(observations) != 1 else ''}"
        if role == "synthesis_agent":
            return f"Synthesized {len(p.get('themes', []))} themes, {len(p.get('gaps', []))} gaps"
        if role == "adversarial_effect_agent":
            return f"Risk: {p.get('decision_risk', 'unknown')}; {len(p.get('adverse_effects', []))} adverse effects"
        if role == "writer":
            cited = len(set(CITATION.findall(p.get("content_markdown", ""))))
            return f"Draft ready, {cited} claims cited"
        if role == "reviewer":
            blocking = [f for f in p.get("findings", []) if f.get("severity") == "blocking"]
            verdict = p.get("disposition", "?")
            return f"Verdict: {verdict}" + (f", {len(blocking)} blocking" if blocking else "")
        return "Done"

    def run(self, role: Role, packet: Packet) -> AgentResult:
        _, title = self._card(role, packet)
        try:
            self._apply_run_deadline()
            # The deterministic demo is otherwise over before the browser can paint a
            # single running card. Showcase mode deliberately gives each stage one
            # visible beat; normal demo and live runs remain unthrottled.
            if self.session.showcase_mode:
                time.sleep(SHOWCASE_STEP_DELAY)
            result = self.inner.run(role, packet)
            if any(item.startswith("HANDOFF_REJECTED:") for item in result.unresolved):
                # A malformed delivery cannot be repaired by retrying the same packet,
                # or relabeled as a successfully skipped evidence source.
                return result
            if role == "planner" and result.outcome != "produced" and result.proposed_tasks:
                # A plan was proposed; what it still wants (data, catalogues, an audience) are questions for
                # the user, which the cards and the chat already show. They do not stop the run.
                plan = ResearchPlan(rationale="Plan proposed. Inputs the planner asked for are listed as open questions.")
                result = result.model_copy(update={"outcome": "produced", "payload": plan.model_dump()})
            remaining = None if packet.model_call_budget is None else max(0, packet.model_call_budget - result.model_calls)
            if result.outcome != "produced" and remaining != 0:  # one retry inside the caller's allowance
                again = packet
                if role == "writer":
                    again = packet.model_copy(update={"review_feedback": [*packet.review_feedback, RETRY_NUDGE]})
                elif role in EVIDENCE_SPECIALISTS and (packet.claim_items() or packet.reads):
                    again = packet.model_copy(update={"review_feedback": [*packet.review_feedback,
                        "The previous response requested: " + "; ".join(result.unresolved), BOUNDED_RESEARCH_NOTE]})
                if remaining is not None:
                    again = again.model_copy(update={"model_call_budget": remaining})
                self._apply_run_deadline()
                retry = self.inner.run(role, again)
                result = retry.model_copy(update={"model_calls": retry.model_calls + result.model_calls})
        except Exception:
            raise
        skipped = role == "evidence_reader" and result.outcome != "produced" and not packet.review_context
        if skipped:
            # A reader that still cannot finish is skipped, not fatal: the run continues without that source.
            label = packet.reads[0].label if packet.reads else "source"
            reason = "; ".join(u for u in result.unresolved if u.strip())[:300] or result.outcome.replace("_", " ")
            note = f"The reader for {label} could not complete after two attempts, so that source was not used ({reason})."
            self.session.skipped.append(note)
            result = AgentResult(role=role, task_id=packet.task_id, outcome="produced", model_calls=result.model_calls,
                                 payload={"source_id": label, "relevant": False, "summary": f"Skipped: {reason}"},
                                 limitations=[note], unresolved=result.unresolved)
        questions = [q for q in result.unresolved if q.strip()][:4]
        for q in questions[:2]:
            self.session.open_questions.setdefault(q, title)
        return result


MAX_RESTORED_SOURCES = 40


class Session:
    def __init__(self, workdir: Path, make_agent: Callable[[], Any] | None, client=None, *,
                 make_test_agent: Callable[[], Any] | None = None, test_model: str | None = None,
                 run_timeout_seconds: float | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.root = workdir / self.id
        self.root.mkdir(parents=True, exist_ok=True)
        self.event_database = self.root / "studio-events.sqlite"
        self.make_agent = make_agent  # None means demo mode with the fake agent
        self.make_test_agent = make_test_agent
        self.test_model = test_model
        self.run_timeout_seconds = run_timeout_seconds
        self._run_deadline: float | None = None
        self.test_run = False
        self.client = client  # live chat client for intake and the scout; None in demo mode
        self.intake = Intake(client=client)
        self.sources: dict[str, str] = {}
        self.web_results: list[dict[str, Any]] = []
        self.literature_reviews: list[ReviewSpec] = []
        self.events: list[dict[str, Any]] = []
        self.cards: dict[str, dict[str, Any]] = {}
        self.open_questions: dict[str, str] = {}
        self.skipped: list[str] = []  # readers that could not finish; reported as limitations
        self.status = "intake"
        self.report: dict[str, Any] | None = None
        self.runs = 0
        self.showcase_mode = False
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._seen = (0, 0, 0, 0)
        self._events_ready = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._init_event_journal()
        if make_agent is not None:
            opening = "Ask any research question. I'll search the web, read the available sources, and return a cited report."
        else:
            opening = self.intake.opening()
            opening += " Demo mode is active; choose Load demo to try the full workflow."
        self.emit("chat", role="assistant", who="Intake", text=opening)


    # feed

    def _init_event_journal(self) -> None:
        conn = sqlite3.connect(self.event_database)
        try:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS studio_events (
                           seq INTEGER PRIMARY KEY AUTOINCREMENT,
                           emitted_at REAL NOT NULL,
                           payload_json TEXT NOT NULL
                       )"""
                )
        finally:
            conn.close()

    def emit(self, kind: str, /, **data: Any) -> None:
        emitted_at = round(time.time(), 3)
        payload = {"t": emitted_at, "type": kind, **data}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._events_ready:
            conn = sqlite3.connect(self.event_database)
            try:
                with conn:
                    cursor = conn.execute(
                        "INSERT INTO studio_events (emitted_at, payload_json) VALUES (?, ?)",
                        (emitted_at, encoded),
                    )
                    seq = int(cursor.lastrowid)
            finally:
                conn.close()
            event = {"seq": seq, **payload}
            self.events.append(event)
            self._events_ready.notify_all()

    def agent(self, card: str, **fields: Any) -> None:
        with self._lock:
            current = self.cards.setdefault(card, {"id": card})
            current.update({k: v for k, v in fields.items() if v is not None})
            snapshot = dict(current)
        self.emit("agent", **snapshot)

    def since(self, after: int) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.event_database)
        try:
            rows = conn.execute(
                "SELECT seq, payload_json FROM studio_events WHERE seq > ? ORDER BY seq",
                (after,),
            ).fetchall()
        finally:
            conn.close()
        return [{"seq": int(seq), **json.loads(payload)} for seq, payload in rows]

    def wait_since(self, after: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        """Wait for the next durable events; used by the SSE transport."""
        with self._events_ready:
            events = self.since(after)
            if events:
                return events
            self._events_ready.wait(timeout)
        return self.since(after)

    # intake and sources

    def chat(self, message: str) -> None:
        message = message.strip()
        if not message:
            return
        self.emit("chat", role="user", who="You", text=message)
        if self.status in ("done", "blocked", "failed") and self.open_questions:
            self.intake.brief.context = (self.intake.brief.context + " Answers from the user: " + message).strip()
            self.emit("chat", role="assistant", who="Intake",
                      text="Noted. Run again to use it.")
            return
        if not self.intake.brief.question:
            # A research question is sufficient to start. Optional context can be
            # added later, but never make the user repeat the same question.
            self.intake.brief.merge({"question": message})
            self.intake.resume(True)
            reply = ("Question captured. Starting from this exact question is enough; add optional context or run research."
                     if self.make_agent is not None else
                     "Question captured. This server is in offline fixture mode; configure OPENAI_API_KEY to research it on the web.")
            self.emit("chat", role="assistant", who="Intake", text=reply)
            self.emit("brief", brief=self.intake.brief.as_dict(), ready=True)
            return
        reply = self.intake.reply(message)
        self.emit("chat", role="assistant", who="Intake", text=reply)
        self.emit("brief", brief=self.intake.brief.as_dict(), ready=self.intake.ready)

    def add_source(self, name: str, text: str) -> str:
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(f"{name} is larger than {MAX_SOURCE_BYTES // 1000} KB")
        if not text.strip():
            raise ValueError(f"{name} is empty")
        sid = _slug(name)
        n = 2
        while sid in self.sources and self.sources[sid] != text:
            sid = f"{_slug(name)}-{n}"
            n += 1
        self.sources[sid] = text.replace("\r\n", "\n")
        self.emit("sources", sources=self.source_list())
        return sid

    def restore(self, state: dict[str, Any]) -> None:
        """Rebuild a session another server instance lost, from what the page remembers.

        Serverless hosts keep sessions in memory per instance, so a later request can land
        on an instance that has never seen this one."""
        if isinstance(state.get("brief"), dict):
            self.intake.brief.merge(state["brief"])
        if isinstance(state.get("sources"), dict):
            for name, text in list(state["sources"].items())[:MAX_RESTORED_SOURCES]:
                self.add_source(str(name)[:120], str(text))
        if state.get("demo"):
            self.use_demo()
        if isinstance(state.get("test_run"), bool):
            self.test_run = state["test_run"]
            self.emit("run_config", test_run=self.test_run, model=self.test_model)
        if "literature_reviews" in state:
            self.set_literature_reviews(state["literature_reviews"])
        self.intake.resume(bool(state.get("ready")))

    def use_demo(self, *, showcase: bool = False, prompt: str = "") -> None:
        if self.running:
            raise RuntimeError("wait for the run to finish before loading the demo")
        self.showcase_mode = showcase

        prompt = prompt.strip()
        if prompt:
            if len(prompt) > 2000:
                raise ValueError("demo prompt must be at most 2000 characters")
            if self.make_agent is None:
                raise ValueError("This server is in offline fixture mode. Configure OPENAI_API_KEY to research a custom prompt.")
            self.emit("chat", role="user", who="You", text=prompt)
            self.intake.brief = Brief(question=prompt)
            self.intake.ready = True

        # A live demo may research any question. Do not quietly answer it from the
        # unrelated forecasting fixture: give it a small, bounded literature review
        # so it comfortably fits inside a hosted request.
        custom_question = self.intake.brief.question.strip()
        if self.make_agent is not None and custom_question and custom_question != DEMO_BRIEF.question:
            # Arbitrary research uses the configured research model. The fixture/test
            # model is intentionally reserved for the fixed showcase workflow.
            self.test_run = False
            fixture_ids = {path.stem for path in (FIXTURE / "input" / "papers").glob("*.md")}
            self.sources = {sid: text for sid, text in self.sources.items() if sid not in fixture_ids}
            backends = ["crossref", "europe_pmc"]
            if os.environ.get("BRAVE_SEARCH_API_KEY"):
                backends.append("brave")
            self.literature_reviews = [ReviewSpec(
                id="prompt-research",
                question=custom_question,
                subquestions=self.intake.brief.open_questions[:4],
                inclusion=self.intake.brief.constraints[:8],
                backends=backends,
                max_searches=6,
                max_model_calls=12,
                max_documents=6,
                results_per_search=6,
                max_rounds=2,
                max_seconds=PROMPT_DEMO_SECONDS,
                max_read_chunks=12,
            )]
            self.emit("literature_config", reviews=[spec.model_dump(mode="json") for spec in self.literature_reviews])
            self.emit("chat", role="assistant", who="Intake",
                      text="Prompt loaded. The demo will find and read research for this question, then write a cited report.")
        else:
            self.test_run = True
            for path in sorted((FIXTURE / "input" / "papers").glob("*.md")):
                self.sources[path.stem] = path.read_text(encoding="utf-8")
            self.literature_reviews = []
            self.intake.brief = Brief(**DEMO_BRIEF.as_dict())
            self.intake.ready = True
            self.emit("chat", role="assistant", who="Intake",
                      text="Demo papers and question loaded.")
        self.emit("run_config", test_run=self.test_run, model=self.test_model if self.test_run else None)
        self.emit("brief", brief=self.intake.brief.as_dict(), ready=True)
        self.emit("sources", sources=self.source_list())

    def remove_source(self, sid: str) -> None:
        """Drop an uploaded source before a run. A running team keeps reading what it was given."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("wait for the run to finish before removing sources")
        if sid not in self.sources:
            raise ValueError(f"no source named {sid}")
        del self.sources[sid]
        self.emit("sources", sources=self.source_list())

    def source_list(self) -> list[dict[str, Any]]:
        return [{"id": k, "lines": len(v.splitlines())} for k, v in self.sources.items()]

    def search_web(self, query: str, limit: int = 6, *, announce: bool = True) -> list[dict[str, Any]]:
        """Find scholarly candidates without pretending their metadata is full-text evidence."""
        results = [result.as_dict() for result in search_crossref(query, limit)]
        self.web_results = results
        self.emit("web_results", query=query, results=results)
        if announce:
            self.emit("chat", role="assistant", who="Web search",
                      text=(f"Found {len(results)} paper candidate{'s' if len(results) != 1 else ''}. "
                            "Choose Use in research to have the team retrieve, read and cross-reference selected papers. "
                            "Catalogue details alone are not evidence."))
        return results

    def use_search_paper(self, doi: str, *, remove: bool = False) -> list[dict[str, Any]]:
        """Queue selected catalogue results for the real literature evidence pipeline."""
        if self.running:
            raise RuntimeError("wait for the run to finish before changing selected papers")
        doi = doi.strip().lower()
        current = next((spec for spec in self.literature_reviews if spec.id == "searched-papers"), None)
        selected = list(current.seed_dois) if current else []
        if remove:
            selected = [value for value in selected if value != doi]
        else:
            if not any(item.get("doi", "").lower() == doi and doi for item in self.web_results):
                raise ValueError("search for this paper before selecting it")
            if doi not in selected:
                selected.append(doi)
            if len(selected) > 6:
                raise ValueError("choose up to 6 papers per research run")
        reviews = [spec.model_dump(mode="json") for spec in self.literature_reviews if spec.id != "searched-papers"]
        if selected:
            question = self.intake.brief.question or "Compare the findings of the selected research papers."
            reviews.append(ReviewSpec(
                id="searched-papers", question=question, seed_dois=selected,
                inclusion=["Prioritize the selected papers when relevant; explain any exclusions.",
                           "Cross-reference findings across papers: identify agreement, contradictions, study differences and gaps."],
                max_searches=8 + len(selected),
            ).model_dump(mode="json"))
        self.set_literature_reviews(reviews)
        return [spec.model_dump(mode="json") for spec in self.literature_reviews]

    def _search_web_for_run(self) -> None:
        """Run the non-blocking research-candidate search beside the evidence pipeline."""
        brief = self.intake.brief
        query = " ".join(x for x in (brief.question, brief.context) if x).strip()[:500]
        self.agent("scout", role="scout", title="Web Scout", status="running",
                   detail="Searching for related research while readers analyse the uploaded papers")
        try:
            results = self.search_web(query, announce=False)
        except Exception as exc:
            self.agent("scout", status="failed", detail=f"Web search unavailable: {exc}")
            self.emit("chat", role="assistant", who="Web Scout",
                      text="The parallel web search was unavailable, but the uploaded-paper analysis continued.")
            return
        self.agent("scout", status="done", detail=f"Found {len(results)} related paper candidates")
        self.emit("chat", role="assistant", who="Web Scout",
                  text=(f"Found {len(results)} related paper candidate{'s' if len(results) != 1 else ''} while the readers worked. "
                        "Choose Use in research after this run to have the team retrieve and compare them in the next run."))

    def set_literature_reviews(self, reviews: list[dict]) -> None:
        if self.running:
            raise RuntimeError("wait for the run to finish before changing literature reviews")
        if self.make_agent is None and reviews:
            raise ValueError("literature review requires a live model; demo mode only uses its built-in papers")
        self.literature_reviews = ReviewConfig.model_validate({"reviews": reviews}).reviews
        self.emit("literature_config", reviews=[spec.model_dump(mode="json") for spec in self.literature_reviews])

    # run

    def start(self, max_model_calls: int = 24, *, test_run: bool | None = None) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("a run is already in progress")
        if test_run is not None:
            if not isinstance(test_run, bool):
                raise ValueError("test_run must be a boolean")
            self.test_run = test_run
        self.emit("run_config", test_run=self.test_run, model=self.test_model if self.test_run else None)
        if not self.intake.brief.question:
            raise ValueError("ask your question in the chat first")
        if not self.sources and self.make_agent is None:
            if self.intake.brief.question != DEMO_BRIEF.question:
                raise ValueError("This server is in offline fixture mode and cannot research an arbitrary question. "
                                 "Configure OPENAI_API_KEY and restart it.")
            self.use_demo()  # the explicit fixture question can use the fixture papers
        if self.make_agent is None and set(self.sources) - {p.stem for p in (FIXTURE / "input" / "papers").glob("*.md")}:
            raise ValueError("Demo mode only reads the demo papers. Add OPENAI_API_KEY to .env to use your own.")
        if not self.sources and not self.literature_reviews:
            self.set_literature_reviews([ReviewSpec(id="literature", question=self.intake.brief.question,
                                                  subquestions=self.intake.brief.open_questions[:12],
                                                  inclusion=self.intake.brief.constraints[:20]).model_dump()])
        if any(spec.id == "prompt-research" for spec in self.literature_reviews):
            max_model_calls = min(max_model_calls, PROMPT_DEMO_MODEL_CALLS)
        self.runs += 1
        self.status = "running"
        self.report = None
        self.cards.clear()
        self.open_questions.clear()
        self.skipped.clear()
        self.web_results = []
        project = self.root / f"run-{self.runs}"
        self._run_deadline = (time.monotonic() + self.run_timeout_seconds
                              if self.run_timeout_seconds is not None else None)
        self._thread = threading.Thread(target=self._run, args=(project, max_model_calls), daemon=True)
        self._thread.start()


    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def remaining_run_seconds(self) -> float | None:
        return None if self._run_deadline is None else self._run_deadline - time.monotonic()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _write_project(self, project: Path) -> None:
        papers = project / "input" / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        for old in papers.glob("*.md"):
            if old.stem not in self.sources:
                old.unlink()
        (project / "input" / "brief.md").write_text(self.intake.brief.to_markdown(), encoding="utf-8")
        for sid, text in self.sources.items():
            (papers / f"{sid}.md").write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        if self.literature_reviews:
            # The brief can change after selection; bind the comparison to the final question.
            self.literature_reviews = [spec.model_copy(update={"question": self.intake.brief.question})
                                       if spec.id == "searched-papers" else spec for spec in self.literature_reviews]
            (project / "input" / "literature.yaml").write_text(yaml.safe_dump(
                {"reviews": [spec.model_dump(mode="json") for spec in self.literature_reviews]}, sort_keys=False), encoding="utf-8")
        elif (project / "input" / "literature.yaml").exists():
            (project / "input" / "literature.yaml").unlink()

    def _run(self, project: Path, max_model_calls: int) -> None:
        from ar import db
        from ar.controller import run_project

        self.emit("run", status="started", run=self.runs, project=str(project))
        web_scout = None
        if self.make_agent is not None and self.sources and not self.literature_reviews:
            web_scout = threading.Thread(target=self._search_web_for_run, daemon=True)
            web_scout.start()
        if SCOPE_CONSTRAINT not in self.intake.brief.constraints:
            self.intake.brief.constraints.append(SCOPE_CONSTRAINT)
            self.emit("brief", brief=self.intake.brief.as_dict(), ready=True)
        stop = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(project, stop), daemon=True)
        try:
            self._write_project(project)
            db.init_project(project).close()
            factory = (self.make_test_agent or self.make_agent) if self.test_run else self.make_agent
            agent = StageAgent(factory() if factory else FakeAgent(), self)
            watcher.start()
            outcome = run_project(project, agent, max_model_calls=max_model_calls)
            stop.set()
            watcher.join(2)
            self._poll(project)
            status = str(outcome.value)
        except Exception as exc:  # surface any runtime failure in the feed
            stop.set()
            status = "timed_out" if isinstance(exc, ResearchRunTimeout) else "failed"
            message = ("The hosted time budget was reached. Partial findings were preserved; run again to continue."
                       if status == "timed_out" else f"The run stopped: {exc}")
            self.emit("chat", role="assistant", who="Orchestrator", text=message)
        if web_scout is not None:
            web_scout.join(14)
        self.report = build_report(project, status, self.open_questions)
        if status == "blocked" and not self.report["markdown"]:
            discovery = build_discovery_report(project, self.intake.brief.question)
            if discovery is not None:
                # Discovery completed, but no source passage passed the evidence
                # gate. Deliver the real search ledger as an honest report instead
                # of reducing the entire run to an orchestrator error.
                markdown, discovery_limitations = discovery
                status = "completed"
                self.report["outcome"] = status
                self.report["markdown"] = markdown
                self.report["discovery_only"] = True
                self.report["limitations"] = [*discovery_limitations, *self.report["limitations"]]
                self.report["open_questions"] = []
        self.report["web_results"] = self.web_results
        self.report["limitations"] = [*self.skipped, *self.report["limitations"]]
        self.status = "done" if status == "completed" else status
        self.emit("run", status=status, run=self.runs)
        self.emit("report", report=self.report)
        self.emit("chat", role="assistant", who="Orchestrator", text=_closing(self.report))
        self._run_deadline = None

    # quality feed

    def _poll(self, project: Path) -> None:
        # The watcher and final flush can overlap when a database read outlasts join().
        # Advance the shared cursor and publish its batch under one lock.
        with self._poll_lock:
            self._poll_locked(project)

    def _poll_locked(self, project: Path) -> None:
        database = project / ".research" / "state.sqlite"
        if not database.is_file():
            return
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            c0, r0, v0, e0 = self._seen
            claims = _rows(conn, "SELECT id, statement, origin_task FROM claims ORDER BY rowid")
            rejected = _rows(conn, "SELECT id, code, reason, hint FROM claim_rejections ORDER BY seq")
            reviews = _rows(conn, "SELECT revision, disposition, decision FROM reviews ORDER BY id")
            runtime_events = _rows(
                conn,
                "SELECT id, run_id, task_id, kind, payload_json, created_at FROM events WHERE id > ? ORDER BY id",
                (e0,),
            )
            for event in runtime_events:
                self.emit("runtime", event=event["kind"], run=event["run_id"], task=event["task_id"],
                          payload=json.loads(event["payload_json"]), at=event["created_at"])
            for c in claims[c0:]:
                self.emit("quality", kind="accepted", text=f"{c['id']} accepted: {c['statement']}")
            for r in rejected[r0:]:
                text = f"{r['id']} rejected ({r['code']}): {r['reason']}"
                self.emit("quality", kind="rejected", text=text + (f" {r['hint']}" if r["hint"] else ""))
            for v in reviews[v0:]:
                decision = "accepted" if v["decision"] == "accept" else str(v["decision"]).replace("_", " ")
                self.emit("quality", kind="review", text=f"Review {v['revision']}: {decision}")
            last_event = int(runtime_events[-1]["id"]) if runtime_events else e0
            self._seen = (len(claims), len(rejected), len(reviews), last_event)
        finally:
            conn.close()

    def _watch(self, project: Path, stop: threading.Event) -> None:
        self._seen = (0, 0, 0, 0)
        while not stop.wait(0.4):
            try:
                self._poll(project)
            except sqlite3.Error:
                continue


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:  # table not created yet
        return []


def _closing(report: dict[str, Any]) -> str:
    if report.get("discovery_only"):
        return ("Done. The search report lists the real candidate literature and retrieval limitations. "
                "No source passage passed verification, so it contains no unsupported claims.")
    n = len(report.get("cited", []))
    head = "Done." if report["outcome"] == "completed" else f"Stopped: {report['outcome'].replace('_', ' ')}."
    parts = [f"{head} The report uses {n} verified claim{'s' if n != 1 else ''}."]
    rejected = len(report.get("rejections", []))
    if rejected:
        parts.append(f"{rejected} quote{'s' if rejected != 1 else ''} rejected.")
    if report.get("open_questions"):
        parts.append("The agents left open questions. Answer them here and run again.")
    return " ".join(parts)


def build_discovery_report(project: Path, question: str) -> tuple[str, list[str]] | None:
    """Build a useful zero-claim report from durable literature search ledgers.

    Candidate metadata is reported as discovery output, never promoted to evidence.
    This lets Studio finish honestly when papers were found but no readable passage
    survived acquisition and claim validation.
    """
    ledgers = sorted((project / "output" / "literature").glob("*/search-ledger.json"))
    if not ledgers:
        return None
    searches = documents = fetches = 0
    backends: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    limitations: list[str] = []
    for path in ledgers:
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        protocol = ledger.get("protocol") if isinstance(ledger.get("protocol"), dict) else {}
        result = ledger.get("result") if isinstance(ledger.get("result"), dict) else {}
        backends.extend(str(item) for item in protocol.get("backends", []) if item)
        searches += int(result.get("searches") or 0)
        documents += int(result.get("documents") or 0)
        fetches += int(result.get("fetches") or 0)
        limitations.extend(str(item).strip() for item in result.get("limitations", []) if str(item).strip())
        reason = str(result.get("stop_reason") or "").strip()
        if reason:
            limitations.append(reason)
        for row in ledger.get("candidates", []):
            if not isinstance(row, dict):
                continue
            data = row.get("data_json", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    continue
            if not isinstance(data, dict):
                continue
            key = str(data.get("doi") or data.get("url") or data.get("id") or data.get("title") or "").strip()
            if key:
                candidates.setdefault(key, data)
    if not candidates and searches == 0 and not limitations:
        return None

    def clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    unique_limits = list(dict.fromkeys(clean(item) for item in limitations if clean(item)))
    lines = [
        f"# Research report: {clean(question)}",
        "",
        "## Result",
        "",
        "The research search completed, but no source passage passed the evidence-verification gate. "
        "A supported answer cannot be stated yet. The discovery results below are real search outputs, "
        "but bibliographic metadata is not treated as evidence.",
        "",
        "## Search performed",
        "",
        f"- Backends: {', '.join(dict.fromkeys(backends)) or 'configured scholarly indexes'}",
        f"- Searches completed: {searches}",
        f"- Documents acquired: {documents}",
        f"- Retrieval attempts: {fetches}",
        f"- Candidate works found: {len(candidates)}",
        "",
        "## Candidate works",
        "",
    ]
    for candidate in list(candidates.values())[:20]:
        title = clean(candidate.get("title")) or "Untitled result"
        year = clean(candidate.get("year")) or "date unknown"
        authors = clean(candidate.get("authors"))
        doi = clean(candidate.get("doi"))
        detail = " · ".join(item for item in (authors, year, f"DOI {doi}" if doi else "") if item)
        lines.append(f"- {title}" + (f" — {detail}" if detail else ""))
    if len(candidates) > 20:
        lines.append(f"- …and {len(candidates) - 20} additional candidates in the audit ledger.")
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in unique_limits] or [
        "- Readable full text or abstracts were unavailable, or extracted claims did not pass quote verification."
    ]
    lines += [
        "",
        "## Next steps",
        "",
        "- Add accessible papers or select candidate DOIs for another run.",
        "- Configure Brave Search for broader web discovery when the scholarly indexes are too narrow.",
        "- The system will only convert retrieved source passages—not search metadata—into verified findings.",
        "",
    ]
    return "\n".join(lines), unique_limits


def build_report(project: Path, outcome: str, open_questions: dict[str, str]) -> dict[str, Any]:
    """Everything the final report view needs, read from committed state."""
    out: dict[str, Any] = {"outcome": outcome, "markdown": "", "claims": [], "cited": [], "rejections": [],
                           "sources": [], "problems": [], "limitations": [], "requirements": [],
                           "open_questions": [{"text": q, "by": who} for q, who in open_questions.items()]}
    prov_path = project / "output" / "provenance.json"
    database = project / ".research" / "state.sqlite"
    if prov_path.is_file():
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
        exact = (prov.get("artifact") or {}).get("exact_copy")
        if exact and (project / "output" / exact).is_file():
            out["markdown"] = (project / "output" / exact).read_text(encoding="utf-8")
        out.update({k: prov.get(k, []) for k in ("problems", "limitations", "requirements", "rejections")})
        out["cited"] = prov.get("cited_claim_ids", [])
    if not database.is_file():
        return out
    from ar.broker import Broker
    from ar.evidence import list_claims, source_lines_from

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        broker = Broker(conn, project)
        lines_of = source_lines_from(broker)
        receipts: dict[str, dict] = {}
        for r in _rows(conn, "SELECT * FROM read_receipts"):
            receipts[r["id"]] = {"id": r["id"], "task": r["task_id"], "start": r["sel_start"], "end": r["sel_end"],
                                 "at": r["delivered_at"]}
        for claim in list_claims(conn):
            links = []
            for link in claim.evidence_links:
                lines = lines_of(link.source_ref) or []
                lo, hi = max(1, link.locator.start - 1), min(len(lines), link.locator.end + 1)
                links.append({
                    "relation": link.relation, "source": link.source_ref.source_id,
                    "start": link.locator.start, "end": link.locator.end, "quote": link.quote,
                    "receipt": receipts.get(link.read_receipt_id, {"id": link.read_receipt_id}),
                    "window": [{"n": n, "text": lines[n - 1]} for n in range(lo, hi + 1)] if lines else [],
                })
            out["claims"].append({"id": claim.id, "statement": claim.statement, "status": claim.epistemic_status,
                                  "task": claim.origin_task, "limitations": claim.limitations, "links": links})
        cited_sources = {l["source"] for c in out["claims"] if c["id"] in out["cited"] for l in c["links"]}
        for s in _rows(conn, "SELECT source_id, path, line_count FROM sources WHERE source_id != 'brief' ORDER BY source_id"):
            out["sources"].append({"id": s["source_id"], "lines": s["line_count"], "cited": s["source_id"] in cited_sources})
    finally:
        conn.close()
    return out
