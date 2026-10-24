from pathlib import Path

import pytest

from ar.agents import reader
from ar.agents.fake import load_answer_key
from ar.models import AgentResult, LineSelector, SourceRef
from ar.packet import DeliveredRead, Packet

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"
KEY = load_answer_key()


def read_of(source_id: str, content: str | None = None, start: int = 1) -> DeliveredRead:
    if content is None:
        content = (FIXTURE / "input" / "papers" / f"{source_id}.md").read_text()
    return DeliveredRead(
        source_ref=SourceRef(source_id=source_id),
        selector=LineSelector(start=start, end=start + len(content.splitlines()) - 1),
        content=content,
        read_receipt_id=f"rr-{source_id}",
        label=source_id,
    )


def packet(*reads: DeliveredRead) -> Packet:
    return Packet(
        packet_id="p1",
        task_id="read-1",
        attempt_id="a1",
        role="evidence_reader",
        objective="Extract claims on seasonal-naive baselines.",
        reads=list(reads),
    )


def check(result: AgentResult) -> reader.EvidenceExtraction:
    AgentResult.model_validate(result.model_dump())
    assert result.model_calls == 0
    return reader.PAYLOAD.model_validate(result.payload)


@pytest.mark.parametrize("source_id", ["paper-a", "paper-b"])
def test_relevant_papers_yield_expected_claim_drafts(source_id):
    entry = KEY["sources"][source_id]
    result = reader.fake(packet(read_of(source_id)), KEY)
    payload = check(result)
    assert result.outcome == "produced"
    assert payload.relevant and payload.source_id == source_id
    claim = result.claims[0]
    [link] = claim.evidence_links
    assert claim.statement == entry["statement"]
    assert link.relation == entry["relation"]
    assert link.quote == entry["quote"]
    assert link.locator == LineSelector(**entry["locator"])
    assert link.read_receipt_id == f"rr-{source_id}"
    assert link.source_ref.source_id == source_id
    if rejected := entry.get("rejected_claim"):
        assert len(result.claims) == 2
        rejected_draft = result.claims[1]
        assert rejected_draft.statement == rejected["statement"]
        assert rejected_draft.evidence_links[0].quote == rejected["quote"]
    else:
        assert len(result.claims) == 1


def test_off_topic_paper_produces_no_claims():
    result = reader.fake(packet(read_of("paper-c")), KEY)
    payload = check(result)
    assert result.outcome == "produced"
    assert not payload.relevant
    assert result.claims == []
    assert any("off-topic" in lim for lim in result.limitations)


def test_no_reads_needs_dependency():
    result = reader.fake(packet(), KEY)
    check(result)
    assert result.outcome == "needs_dependency"
    assert result.unresolved and not result.claims


def test_quote_not_at_key_lines_is_unable_to_complete():
    text = (FIXTURE / "input" / "papers" / "paper-a.md").read_text().splitlines()
    text[6] = "A different sentence sits where the recommendation was."
    result = reader.fake(packet(read_of("paper-a", "\n".join(text))), KEY)
    check(result)
    assert result.outcome == "unable_to_complete"
    assert result.claims == []
    assert result.unresolved


def test_quote_on_wrong_line_is_not_cited():
    text = (FIXTURE / "input" / "papers" / "paper-a.md").read_text().splitlines()
    text[6], text[5] = text[5], text[6]
    result = reader.fake(packet(read_of("paper-a", "\n".join(text))), KEY)
    assert result.outcome == "unable_to_complete"
    assert result.claims == []


def test_uncovered_range_is_unable_to_complete():
    text = (FIXTURE / "input" / "papers" / "paper-a.md").read_text().splitlines()
    head = read_of("paper-a", "\n".join(text[:4]))
    result = reader.fake(packet(head), KEY)
    check(result)
    assert result.outcome == "unable_to_complete"
    assert result.claims == []
    assert "not delivered" in result.unresolved[0]


def test_selector_offset_is_honoured():
    text = (FIXTURE / "input" / "papers" / "paper-a.md").read_text().splitlines()
    tail = read_of("paper-a", "\n".join(text[5:]), start=6)
    result = reader.fake(packet(tail), KEY)
    assert result.outcome == "produced"
    assert result.claims[0].evidence_links[0].locator.start == 7


def test_selector_claiming_more_than_content_is_not_covered():
    short = read_of("paper-a", "only one line").model_copy(update={"selector": LineSelector(start=1, end=8)})
    result = reader.fake(packet(short), KEY)
    assert result.outcome == "unable_to_complete"
    assert result.claims == []


def test_unknown_source_is_unable_to_complete():
    result = reader.fake(packet(read_of("paper-z", "text")), KEY)
    check(result)
    assert result.outcome == "unable_to_complete"


def test_user_message_numbers_lines_and_frames_data():
    read = read_of("paper-a", "alpha\nbeta", start=6)
    msg = reader.user_message(packet(read))
    assert "6: alpha\n7: beta" in msg
    assert "<document" in msg and "rr-paper-a" in msg
    assert "Extract claims on seasonal-naive" in msg


def test_system_describes_claims_format():
    assert "ONE JSON object" in reader.SYSTEM
    assert "evidence_links" in reader.SYSTEM
    assert "read_receipt_id" in reader.SYSTEM
