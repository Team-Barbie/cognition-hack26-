"""Whole fake loop through agents.run on the real fixture, no model in the loop."""

import json
from pathlib import Path

from ar import agents
from ar.agents.fake import FakeAgent
from ar.models import ClaimDraft, LineSelector, Requirement, SourceRef
from ar.packet import DeliveredRead, DependencyItem, Packet, SourceInfo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"
SOURCES = ["paper-a", "paper-b", "paper-c"]


def _read(label: str, path: Path) -> DeliveredRead:
    text = path.read_text()
    n = len(text.splitlines())
    return DeliveredRead(
        source_ref=SourceRef(source_id=label), selector=LineSelector(start=1, end=n),
        content=text, read_receipt_id=f"read-{label}", label=label,
    )


def _packet(role, task, **kw) -> Packet:
    return Packet(packet_id=f"p-{task}", task_id=task, attempt_id="a1", role=role,
                  objective="fixture", **kw)


def test_fake_loop_matches_answer_key():
    agents.configure(FakeAgent())
    key = json.loads((FIXTURE / "answer_key.json").read_text())
    reqs = [Requirement(id="req-1", source_passage="1-3", interpretation="baseline")]
    brief = _read("brief", FIXTURE / "input" / "brief.md")

    plan = agents.run("planner", _packet(
        "planner", "t-plan", requirements=reqs, reads=[brief],
        available_sources=[SourceInfo(source_id=s, path=f"papers/{s}.md") for s in SOURCES]))
    readers = [t for t in plan.proposed_tasks if t.role == "evidence_reader"]
    assert [t.source_ids[0] for t in readers] == SOURCES

    claims = []
    for t in readers:
        sid = t.source_ids[0]
        res = agents.run("evidence_reader", _packet(
            "evidence_reader", f"t-{sid}",
            reads=[_read(sid, FIXTURE / "input" / "papers" / f"{sid}.md")]))
        assert res.outcome == "produced"
        claims += res.claims
    assert len(claims) == key["expected_citations"]

    deps = [DependencyItem(kind="claim", id=f"claim-{i}", claim=ClaimDraft(**c.model_dump()))
            for i, c in enumerate(claims, 1)]
    draft = agents.run("writer", _packet("writer", "t-write", requirements=reqs, dependencies=deps))
    assert draft.outcome == "produced"
    md = draft.payload["content_markdown"]
    assert "paper-b" in md and md.count("[claim:") == 2

    art = DependencyItem(kind="artifact", id="report", revision=1, content=md)
    review = agents.run("reviewer", _packet("reviewer", "t-review", dependencies=[art, *deps]))
    assert review.payload["disposition"] == "accept"
