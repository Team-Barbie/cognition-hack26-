from __future__ import annotations

from ar.agents import writer
from ar.models import AgentResult, ClaimDraft, EvidenceLink, LineSelector, Requirement, SourceRef
from ar.packet import DependencyItem, Packet

KEY = {"sources": {"paper-c": {"relevant": False}}}


def link(relation: str, source: str) -> EvidenceLink:
    return EvidenceLink(
        relation=relation,
        source_ref=SourceRef(source_id=source),
        locator=LineSelector(start=7, end=7),
        quote="q",
        read_receipt_id=f"rr-{source}",
    )


def claim_dep(cid: str, relation: str, source: str, limitations: list[str] | None = None) -> DependencyItem:
    draft = ClaimDraft(statement=f"{source} says so", evidence_links=[link(relation, source)], limitations=limitations or [])
    return DependencyItem(kind="claim", id=cid, claim=draft)


def packet(deps: list[DependencyItem], feedback: list[str] | None = None) -> Packet:
    return Packet(
        packet_id="p1",
        task_id="t-write",
        attempt_id="a1",
        role="writer",
        objective="Write the report",
        requirements=[
            Requirement(id="R1", source_passage="recommend a baseline", interpretation="Recommend a baseline"),
            Requirement(id="R2", source_passage="exclude short series", interpretation="Check the exclusion rule"),
        ],
        dependencies=deps,
        review_feedback=feedback or [],
    )


DEPS = [claim_dep("c1", "supports", "paper-a", ["Weekly data only"]), claim_dep("c2", "contradicts", "paper-b")]


def test_fake_draft_sections_and_citations():
    result = writer.fake(packet(DEPS), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "produced" and result.model_calls == 0
    draft = writer.Draft.model_validate(result.payload)
    md = draft.content_markdown
    for heading in ("## Supported baseline", "## Contradictions", "## Limitations"):
        assert heading in md
    assert "[claim:c1]" in md and "[claim:c2]" in md
    assert "paper-b contradicts the exclusion rule" in md
    assert "Weekly data only" in md and "paper-c" in md and "off-topic" in md
    assert draft.cited_claim_ids == ["c1", "c2"]
    assert set(draft.coverage) == {"R1", "R2"} and draft.coverage["R2"] == "Contradictions"
    assert draft.limitations_stated
    assert "Revision notes" not in md


def test_fake_cites_only_known_ids():
    result = writer.fake(packet(DEPS, ["Citation [claim:bogus] is wrong"]), KEY)
    draft = writer.Draft.model_validate(result.payload)
    assert set(draft.cited_claim_ids) <= {"c1", "c2"}
    assert "[claim:bogus]" not in draft.content_markdown


def test_fake_revision_notes():
    result = writer.fake(packet(DEPS, ["Cite the contradiction", "State limits"]), KEY)
    md = writer.Draft.model_validate(result.payload).content_markdown
    assert "## Revision notes" in md
    assert "Cite the contradiction" in md and "State limits" in md


def test_fake_no_claims_needs_dependency():
    result = writer.fake(packet([]), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "needs_dependency" and result.payload == {}


def test_user_message_and_system():
    assert "ONE JSON object" in writer.SYSTEM
    msg = writer.user_message(packet(DEPS, ["fix it"]))
    assert "[claim:c1]" in msg and "R1" in msg and "fix it" in msg
