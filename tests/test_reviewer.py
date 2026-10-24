from __future__ import annotations

from ar.agents import reviewer, writer
from ar.models import AgentResult, ReviewResult
from ar.packet import DependencyItem, Packet
from tests.test_writer import DEPS, KEY
from tests.test_writer import packet as writer_packet


def packet(content: str | None, deps: list[DependencyItem] = DEPS) -> Packet:
    items = list(deps)
    if content is not None:
        items.append(DependencyItem(kind="artifact", id="draft-1", revision=3, content=content))
    return Packet(
        packet_id="p2", task_id="t-review", attempt_id="a1", role="reviewer",
        objective="Review the draft", dependencies=items,
    )


def review(content: str | None) -> tuple[AgentResult, ReviewResult]:
    result = reviewer.fake(packet(content), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.model_calls == 0
    return result, ReviewResult.model_validate(result.payload)


def test_good_draft_accepted():
    result, rr = review("Baseline [claim:c1]. Contradiction [claim:c2].")
    assert result.outcome == "produced"
    assert rr.disposition == "accept" and all(rr.checks.values())
    assert (rr.reviewed_artifact_id, rr.reviewed_revision) == ("draft-1", 3)
    assert not any(f.severity == "blocking" for f in rr.findings)


def test_unknown_citation_blocking():
    _, rr = review("Baseline [claim:c1] [claim:zzz] [claim:c2].")
    assert rr.disposition == "revise" and not rr.checks["citations_resolve"]
    assert [f.claim_id for f in rr.findings if f.severity == "blocking"] == ["zzz"]


def test_missing_contradiction_citation():
    _, rr = review("Baseline [claim:c1] only.")
    assert rr.disposition == "revise" and not rr.checks["contradictions_addressed"]
    assert rr.checks["citations_resolve"]
    assert any(f.severity == "blocking" and f.claim_id == "c2" for f in rr.findings)


def test_empty_draft():
    _, rr = review("   ")
    assert rr.disposition == "revise" and not rr.checks["non_empty"]
    assert any(f.severity == "blocking" for f in rr.findings)


def test_no_artifact_needs_dependency():
    result = reviewer.fake(packet(None), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "needs_dependency" and result.payload == {}


def test_writer_output_accepted_by_reviewer():
    draft = writer.fake(writer_packet(DEPS), KEY)
    md = writer.Draft.model_validate(draft.payload).content_markdown
    _, rr = review(md)
    assert rr.disposition == "accept" and all(rr.checks.values())


def test_writer_revision_accepted_after_feedback():
    _, rr = review("Baseline [claim:c1] only.")
    feedback = [f.message for f in rr.findings if f.severity == "blocking"]
    draft = writer.fake(writer_packet(DEPS, feedback), KEY)
    md = writer.Draft.model_validate(draft.payload).content_markdown
    _, rr2 = review(md)
    assert rr2.disposition == "accept"


def test_system_and_user_message():
    assert "ONE JSON object" in reviewer.SYSTEM
    assert reviewer.PAYLOAD is ReviewResult
    msg = reviewer.user_message(packet("hello"))
    assert "draft-1" in msg and "hello" in msg
