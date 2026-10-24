"""Research studio: intake, a full demo run through the real runtime, and the local HTTP API."""

import json
import threading
import time
import urllib.request

import pytest

from ar.studio.intake import Brief, Intake
from ar.studio.server import serve
from ar.models import LineSelector, Requirement, SourceRef
from ar.packet import DeliveredRead, Packet
from ar.studio.session import Session, packet_view


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


def test_scripted_intake_collects_every_field_and_writes_requirements():
    intake = Intake()
    intake.reply("Does remote work raise productivity?")
    intake.reply("For a 200-person software company deciding a policy.")
    intake.reply("Only peer-reviewed studies; test the claim that hybrid beats fully remote")
    last = intake.reply("What about junior staff?\nHow is productivity measured?")
    assert intake.ready and "sources" in last.lower()
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
    assert intake.ready and "sources" in reply.lower()
    assert intake.brief.as_dict() == {"question": "Is X safe?", "context": "Clinic use",
                                      "constraints": ["Adults only"], "open_questions": ["Dose?"]}
    assert "Conversation (data)" in client.calls[1]


def test_live_intake_falls_back_to_the_script_when_the_model_fails():
    intake = Intake(client=Scripted(RuntimeError("offline")))
    reply = intake.reply("Is X safe?")
    assert intake.client is None and intake.brief.question == "Is X safe?"
    assert reply.startswith("What's the context")


def test_sources_are_named_validated_and_deduplicated(tmp_path):
    s = Session(tmp_path, None)
    assert s.add_source("Smith 2024.md", "a\nb") == "smith-2024"
    assert s.add_source("Smith 2024.md", "a\nb") == "smith-2024"  # same text, same source
    assert s.add_source("smith 2024.txt", "different") == "smith-2024-2"
    with pytest.raises(ValueError, match="empty"):
        s.add_source("x.md", "   ")
    with pytest.raises(ValueError, match="larger"):
        s.add_source("big.md", "x" * 500_001)


def test_demo_mode_refuses_sources_the_fake_agent_cannot_read(tmp_path):
    s = Session(tmp_path, None)
    s.use_demo()
    s.add_source("mine.md", "my own paper")
    with pytest.raises(ValueError, match="demo mode"):
        s.start()


def test_demo_run_shows_every_agent_and_a_verified_report(tmp_path):
    s = Session(tmp_path, None)
    s.use_demo()
    s.start()
    s.wait(60)
    assert s.status == "done", [e for e in s.events if e["type"] == "chat"][-1]
    cards = {e["id"]: e for e in s.events if e["type"] == "agent"}
    assert {"orchestrator", "planner", "reader:paper-a", "reader:paper-b", "reader:paper-c",
            "writer:1", "reviewer", "quality"} <= set(cards)
    assert all(cards[c]["status"] == "done" for c in cards)
    assert cards["reader:paper-c"]["detail"] == "Off-topic for this question; no claims"
    assert cards["planner"]["packet"]["reads"][0]["label"] == "brief"
    assert cards["reader:paper-b"]["packet"]["reads"][0]["receipt"].startswith("read-")
    assert len(cards["writer:1"]["packet"]["dependencies"]) == 2
    assert cards["reviewer"]["packet"]["dependencies"][0]["kind"] == "artifact"
    assert any(e["type"] == "quality" and e["kind"] == "accepted" for e in s.events)

    r = s.report
    assert r["outcome"] == "completed" and r["cited"] == ["claim-1", "claim-2"]
    assert "[claim:claim-2]" in r["markdown"]
    proof = next(c for c in r["claims"] if c["id"] == "claim-2")["links"][0]
    assert (proof["source"], proof["relation"], proof["start"]) == ("paper-b", "contradicts", 7)
    assert any(row["n"] == 7 and "Excluding short series" in row["text"] for row in proof["window"])
    assert proof["receipt"]["id"].startswith("read-") and proof["receipt"]["start"] == 1
    assert {x["id"]: x["cited"] for x in r["sources"]} == {"paper-a": True, "paper-b": True, "paper-c": False}
    assert s.events[-1]["type"] == "chat" and "Research completed" in s.events[-1]["text"]


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
        assert "<title>ar studio</title>" in page
        assert "https://" not in page and "http://" not in page  # no external requests

        status, s = post("/api/session", {})
        assert status == 200 and s["mode"] == "demo · fake agent"
        assert post("/api/run", {"id": s["id"]})[0] == 400  # nothing to run yet
        assert post("/api/chat", {"id": "nope", "message": "hi"})[0] == 400
        assert post("/api/demo", {"id": s["id"]}) == (200, {"ok": True})
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
