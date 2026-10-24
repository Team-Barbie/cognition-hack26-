"""Research studio: intake, a full demo run through the real runtime, and the local HTTP API."""

import json
import threading
import time
import urllib.request

import pytest

from ar.studio.intake import Brief, Intake
from ar.studio.server import SlidingWindowLimiter, Studio, event_stream, handle, serve
from ar.models import LineSelector, Requirement, SourceRef
from ar.packet import DeliveredRead, Packet
from ar.studio.session import Session, packet_view


@pytest.fixture(autouse=True)
def no_external_web_search(monkeypatch):
    """Studio tests exercise orchestration without depending on Crossref's network."""
    monkeypatch.setattr("ar.studio.session.search_crossref", lambda query, limit: [])


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def test_sliding_window_limiter_blocks_then_recovers():
    now = [100.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])
    rules = [("client-a", "api", 2, 10.0)]
    assert limiter.check(rules) is None
    assert limiter.check(rules) is None
    assert limiter.check(rules) == 10
    now[0] += 10
    assert limiter.check(rules) is None


def test_sliding_window_limiter_bounds_identity_buckets_without_weakening_active_rules():
    limiter = SlidingWindowLimiter(max_buckets=2)
    assert limiter.check([("old", "api", 1, 60.0)]) is None
    rules = [("new", "api", 1, 60.0), ("new", "run", 1, 60.0)]
    assert limiter.check(rules) is None
    assert limiter.check(rules) == 60
    assert len(limiter._events) == 2


def test_run_endpoint_has_a_per_client_limit(tmp_path, monkeypatch):
    import ar.studio.server as server

    monkeypatch.setattr(server, "_RATE_LIMITER", SlidingWindowLimiter())
    studio = server.Studio(tmp_path, fake=True)
    for _ in range(6):
        status, _, _ = handle(studio, "POST", "/api/run", "", b"{}", client_id="192.0.2.1")
        assert status == 400  # Invalid sessions are still attempts and must be limited.
    status, _, payload = handle(studio, "POST", "/api/run", "", b"{}", client_id="192.0.2.1")
    assert status == 429
    assert json.loads(payload)["retry_after"] > 0
    other_status, _, _ = handle(studio, "POST", "/api/run", "", b"{}", client_id="192.0.2.2")
    assert other_status == 400


def test_packet_view_exposes_the_exact_bounded_context():
    packet = Packet(
        packet_id="packet-1", task_id="task-1", attempt_id="attempt-1", role="evidence_reader",
        objective="Check the exclusion rule",
        requirements=[Requirement(id="req-1", source_passage="Constraint: 52 weeks",
                                  interpretation="Assess the 52-week cutoff")],
        reads=[DeliveredRead(source_ref=SourceRef(source_id="paper-b", revision=2),
                             selector=LineSelector(start=7, end=8), content="first\nsecond",
                             read_receipt_id="read-7", label="paper-b")],
    )
    view = packet_view(packet)
    assert (view["id"], view["task"], view["attempt"]) == ("packet-1", "task-1", "attempt-1")
    assert view["reads"] == [{"label": "paper-b", "source": "paper-b", "revision": 2,
                               "start": 7, "end": 8, "receipt": "read-7",
                               "lines": [{"n": 7, "text": "first"}, {"n": 8, "text": "second"}]}]


def test_session_events_are_durable_and_sse_resumable(tmp_path):
    session = Session(tmp_path, None)
    session.emit("runtime", event="packet.dispatched", task="task-1")
    events = session.since(0)
    assert session.event_database.is_file()
    assert events[-1]["event"] == "packet.dispatched"
    frame = next(event_stream(session, events[-2]["seq"]))
    assert f"id: {events[-1]['seq']}\n".encode() in frame
    assert b'"event":"packet.dispatched"' in frame


def test_scripted_intake_collects_every_field_and_writes_requirements():
    intake = Intake()
    intake.reply("Does remote work raise productivity?")
    intake.reply("For a 200-person software company deciding a policy.")
    intake.reply("Only peer-reviewed studies; test the claim that hybrid beats fully remote")
    last = intake.reply("What about junior staff?\nHow is productivity measured?")
    assert intake.ready and "papers" in last.lower()
    md = intake.brief.to_markdown()
    assert md.startswith("# Brief\n\nQuestion: Does remote work raise productivity? Context: For a 200-person")
    assert "Constraint: Only peer-reviewed studies" in md
    assert "Open question: What about junior staff?" in md and "Open question: How is productivity measured?" in md
    assert "Deliverable:" in md


def test_none_answers_add_nothing():
    intake = Intake()
    for msg in ("Q?", "ctx", "none", "none"):
        intake.reply(msg)
    assert intake.brief.constraints == [] and intake.brief.open_questions == []


def test_live_intake_merges_the_models_brief():
    client = Scripted(
        {"reply": "What context?", "ready": False, "brief": {"question": "Is X safe?"}},
        {"reply": "Great, ready.", "ready": True,
         "brief": {"context": "Clinic use", "constraints": ["Adults only"], "open_questions": ["Dose?"]}},
    )
    intake = Intake(client=client)
    assert intake.reply("Is X safe?") == "What context?" and not intake.ready
    reply = intake.reply("For a clinic")
    assert intake.ready and "papers" in reply.lower()
    assert intake.brief.as_dict() == {"question": "Is X safe?", "context": "Clinic use",
                                      "constraints": ["Adults only"], "open_questions": ["Dose?"]}
    assert "Conversation (data)" in client.calls[1]


def test_live_intake_falls_back_to_the_script_when_the_model_fails():
    intake = Intake(client=Scripted(RuntimeError("offline")))
    reply = intake.reply("Is X safe?")
    assert intake.client is None and intake.brief.question == "Is X safe?"
    assert reply.startswith("What's it for")


def test_sources_are_named_validated_and_deduplicated(tmp_path):
    s = Session(tmp_path, None)
    assert s.add_source("Smith 2024.md", "a\nb") == "smith-2024"
    assert s.add_source("Smith 2024.md", "a\nb") == "smith-2024"  # same text, same source
    assert s.add_source("smith 2024.txt", "different") == "smith-2024-2"
    with pytest.raises(ValueError, match="empty"):
        s.add_source("x.md", "   ")
    with pytest.raises(ValueError, match="larger"):
        s.add_source("big.md", "x" * 500_001)


def test_a_wrongly_uploaded_source_can_be_removed(tmp_path):
    s = Session(tmp_path, None)
    s.add_source("right.md", "keep me")
    s.add_source("wrong.md", "oops")
    s.remove_source("wrong")
    assert list(s.sources) == ["right"]
    assert s.events[-1]["type"] == "sources" and [x["id"] for x in s.events[-1]["sources"]] == ["right"]
    with pytest.raises(ValueError, match="no source"):
        s.remove_source("wrong")


def test_sources_cannot_be_removed_mid_run(tmp_path):
    s = Session(tmp_path, None)
    s.use_demo()
    s.start()
    try:
        if s._thread.is_alive():
            with pytest.raises(RuntimeError, match="wait for the run"):
                s.remove_source("paper-a")
    finally:
        s.wait(60)
    s.remove_source("paper-a")  # allowed again once the run is over
    assert "paper-a" not in s.sources


def test_demo_mode_refuses_sources_the_fake_agent_cannot_read(tmp_path):
    s = Session(tmp_path, None)
    s.use_demo()
    s.add_source("mine.md", "my own paper")
    with pytest.raises(ValueError, match="Demo mode"):
        s.start()


def test_demo_run_shows_every_agent_and_a_verified_report(tmp_path):
    s = Session(tmp_path, None)
    s.use_demo()
    s.start()
    s.wait(60)
    assert s.status == "done", [e for e in s.events if e["type"] == "chat"][-1]
    scout = [e for e in s.events if e["type"] == "agent" and e["id"] == "scout"]
    assert [e["status"] for e in scout] == ["running", "done"]
    assert any(e["type"] == "quality" and e["kind"] == "accepted" for e in s.events)
    runtime = {e["event"] for e in s.events if e["type"] == "runtime"}
    assert {"run.started", "task.created", "attempt.started", "packet.dispatched",
            "attempt.accepted", "run.finished"} <= runtime
    dispatched = [e for e in s.events if e["type"] == "runtime" and e["event"] == "packet.dispatched"]
    accepted = [e for e in s.events if e["type"] == "runtime" and e["event"] == "attempt.accepted"]
    assert next(e for e in dispatched if e["payload"]["packet"]["role"] == "planner")["payload"]["packet"]["reads"][0]["label"] == "brief"
    assert len(next(e for e in dispatched if e["payload"]["packet"]["role"] == "writer")["payload"]["packet"]["dependencies"]) == 2
    assert next(e for e in accepted if e["payload"]["role"] == "reviewer")["payload"]["result"]["payload"]["disposition"] == "accept"

    r = s.report
    assert r["outcome"] == "completed" and r["cited"] == ["claim-1", "claim-2"]
    assert "[claim:claim-2]" in r["markdown"]
    proof = next(c for c in r["claims"] if c["id"] == "claim-2")["links"][0]
    assert (proof["source"], proof["relation"], proof["start"]) == ("paper-b", "contradicts", 7)
    assert any(row["n"] == 7 and "Excluding short series" in row["text"] for row in proof["window"])
    assert proof["receipt"]["id"].startswith("read-") and proof["receipt"]["start"] == 1
    assert {x["id"]: x["cited"] for x in r["sources"]} == {"paper-a": True, "paper-b": True, "paper-c": False}
    assert s.events[-1]["type"] == "chat" and s.events[-1]["text"] == "Done. The report uses 2 verified claims."


def test_http_api_runs_a_demo_end_to_end(tmp_path):
    httpd = serve(0, fake=True, workdir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer 4444"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            page = res.read().decode()
        assert "<title>Res Loop</title>" in page
        assert "https://" not in page and "http://" not in page  # no external requests
        assert "/api/events/stream" in page

        status, s = post("/api/session", {})
        assert status == 200 and s["mode"] == "demo · fake agent"
        assert post("/api/run", {"id": s["id"]})[0] == 400  # nothing to run yet
        assert post("/api/chat", {"id": "nope", "message": "hi"})[0] == 400
        assert post("/api/demo", {"id": s["id"]}) == (200, {"ok": True})
        assert post("/api/source", {"id": s["id"], "name": "oops.md", "text": "wrong file"}) == (200, {"ok": True, "id": "oops"})
        assert post("/api/source/remove", {"id": s["id"], "source": "oops"}) == (200, {"ok": True})
        assert post("/api/source/remove", {"id": s["id"], "source": "oops"})[0] == 400
        assert post("/api/run", {"id": s["id"]}) == (200, {"ok": True})

        events, after, deadline = [], 0, time.time() + 60
        while time.time() < deadline and not any(e["type"] == "report" for e in events):
            request = urllib.request.Request(f"{base}/api/events?id={s['id']}&after={after}",
                                             headers={"Authorization": "Bearer 4444"})
            with urllib.request.urlopen(request, timeout=10) as res:
                batch = json.loads(res.read())["events"]
            events += batch
            after = batch[-1]["seq"] if batch else after
            time.sleep(0.2)
        report = next(e["report"] for e in events if e["type"] == "report")
        assert report["outcome"] == "completed" and len(report["cited"]) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_brief_markdown_keeps_each_item_on_one_line():
    b = Brief(question="Q\nwith break", context="c", constraints=["a\nb"], open_questions=[])
    lines = b.to_markdown().splitlines()
    assert lines[2] == "Question: Q with break Context: c" and lines[3] == "Constraint: a b"


def test_filler_items_from_the_model_are_dropped():
    b = Brief()
    b.merge({"question": "Q", "constraints": ["Only 2020+ studies", "No other constraints specified.", "none"],
             "open_questions": ["No further questions", "What horizon?"]})
    assert b.constraints == ["Only 2020+ studies"] and b.open_questions == ["What horizon?"]


def test_scout_writes_labelled_notes_when_no_sources_are_given(tmp_path):
    notes = {"notes": [
        {"title": "Catalogues", "lines": ["Fact one.", "Fact two.", "Fact three (uncertain)."]},
        {"title": "Too short", "lines": ["only one"]},
    ]}
    s = Session(tmp_path, make_agent=lambda: None, client=Scripted(notes))
    s.intake.brief = Brief(question="Are there new giant radio galaxies in LoTSS DR3?")
    s._scout()
    assert list(s.sources) == ["notes-catalogues"]
    text = s.sources["notes-catalogues"]
    assert text.splitlines()[0] == "# Catalogues" and "Model-generated background notes" in text
    assert any("model-generated background notes" in c for c in s.intake.brief.constraints)
    assert "model-generated background notes" in s.intake.brief.to_markdown()
    cards = {e["id"]: e for e in s.events if e["type"] == "agent"}
    assert cards["scout"]["status"] == "done"


def test_scout_with_nothing_usable_stops_honestly(tmp_path):
    s = Session(tmp_path, make_agent=lambda: None, client=Scripted({"notes": []}))
    s.intake.brief = Brief(question="Q?")
    s.start()
    s.wait(30)
    assert s.status == "failed" and not s.sources
    assert "couldn't write notes" in [e for e in s.events if e["type"] == "chat"][-1]["text"]


def test_demo_mode_without_sources_loads_the_demo_papers(tmp_path):
    s = Session(tmp_path, None)
    s.intake.brief = Brief(question="Q?")
    s.start()
    s.wait(60)
    assert set(s.sources) == {"paper-a", "paper-b", "paper-c"} and s.status == "done"


def test_a_reader_that_balks_is_retried_then_skipped_without_killing_the_run(tmp_path):
    from ar.agents.fake import FakeAgent
    from ar.models import AgentResult

    class Flaky:
        """paper-c's reader balks twice; paper-a's balks once and then works."""
        def __init__(self):
            self.inner, self.calls = FakeAgent(), {}

        def run(self, role, packet):
            label = packet.reads[0].label if role == "evidence_reader" and packet.reads else role
            self.calls[label] = self.calls.get(label, 0) + 1
            if label == "paper-c" or (label == "paper-a" and self.calls[label] == 1):
                return AgentResult(role=role, task_id=packet.task_id, outcome="unable_to_complete", unresolved=["model hiccup"])
            return self.inner.run(role, packet)

    flaky = Flaky()
    s = Session(tmp_path, make_agent=lambda: flaky, client=None)
    s.use_demo()
    s.start()
    s.wait(60)
    assert s.status == "done" and flaky.calls["paper-a"] == 2 and flaky.calls["paper-c"] == 2
    accepted = [e for e in s.events if e["type"] == "runtime" and e["event"] == "attempt.accepted"]
    skipped = next(e for e in accepted if e["payload"]["result"]["payload"].get("source_id") == "paper-c")
    assert skipped["payload"]["result"]["payload"]["summary"].startswith("Skipped:")
    assert any("paper-c could not complete" in lim for lim in s.report["limitations"])
    assert len(s.report["cited"]) == 2


def test_a_planner_that_proposes_a_plan_but_wants_more_inputs_does_not_stop_the_run(tmp_path):
    from ar.agents.fake import FakeAgent
    from ar.models import AgentResult

    class WantsData:
        def __init__(self):
            self.inner, self.planner_calls = FakeAgent(), 0

        def run(self, role, packet):
            result = self.inner.run(role, packet)
            if role == "planner":
                self.planner_calls += 1
                return result.model_copy(update={"outcome": "needs_dependency", "payload": {},
                                                 "unresolved": ["Supply the LoTSS DR3 catalogue.", "Who is the audience?"]})
            return result

    agent = WantsData()
    s = Session(tmp_path, make_agent=lambda: agent, client=None)
    s.use_demo()
    s.start()
    s.wait(60)
    assert s.status == "done" and agent.planner_calls == 1  # no wasted retry
    planner = next(e for e in s.events if e["type"] == "runtime" and e["event"] == "attempt.accepted"
                   and e["payload"]["role"] == "planner")
    assert planner["payload"]["result"]["unresolved"] == ["Supply the LoTSS DR3 catalogue.", "Who is the audience?"]
    assert "Supply the LoTSS DR3 catalogue." in s.open_questions


def test_writer_gets_the_scope_note_and_a_nudge_when_it_balks(tmp_path):
    from ar.agents.fake import FakeAgent
    from ar.models import AgentResult

    class Balky:
        """Writer answers needs_dependency unless it was told the run cannot fetch data, or nudged to proceed."""
        def __init__(self):
            self.inner, self.seen = FakeAgent(), {}

        def run(self, role, packet):
            if role in ("planner", "writer", "reviewer"):
                self.seen.setdefault(role, []).append(packet)
            if role == "writer" and "Scope note" not in packet.objective:
                return AgentResult(role=role, task_id=packet.task_id, outcome="needs_dependency", unresolved=["Need data"])
            if role == "writer" and len(self.seen["writer"]) == 1 and not packet.review_feedback:
                return AgentResult(role=role, task_id=packet.task_id, outcome="needs_dependency", unresolved=["Still need data"])
            return self.inner.run(role, packet)

    agent = Balky()
    s = Session(tmp_path, make_agent=lambda: agent, client=None)
    s.use_demo()
    s.start()
    s.wait(60)
    assert s.status == "done"
    assert all("Scope note" in p.objective for ps in agent.seen.values() for p in ps)
    writers = agent.seen["writer"]
    assert len(writers) == 2 and "previous reply asked for inputs" in writers[1].review_feedback[-1]
    dispatched = [e for e in s.events if e["type"] == "runtime" and e["event"] == "packet.dispatched"]
    assert all("Scope note" in e["payload"]["packet"]["objective"] for e in dispatched
               if e["payload"]["packet"]["role"] in ("planner", "writer", "reviewer"))
    assert not any("Scope note" in e.get("text", "") for e in s.events if e["type"] == "chat")  # the user never sees it


def test_the_page_has_a_tab_icon_served_by_the_app(tmp_path):
    studio = Studio(tmp_path, fake=True)
    page = handle(studio, "GET", "/", "", b"")[2].decode()
    assert '<link rel="icon" href="/favicon.svg"' in page
    assert "http://" not in page and "https://" not in page  # the page itself still makes no external requests
    for path in ("/favicon.svg", "/favicon.ico"):
        status, kind, body = handle(studio, "GET", path, "", b"")
        assert (status, kind) == (200, "image/svg+xml") and body.startswith(b"<svg")
