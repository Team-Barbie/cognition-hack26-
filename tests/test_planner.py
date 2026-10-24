from pathlib import Path

from ar.agents import planner
from ar.agents.fake import load_answer_key
from ar.models import AgentResult, Requirement, SourceRef, LineSelector
from ar.packet import DeliveredRead, Packet, SourceInfo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"
KEY = load_answer_key()
SOURCES = ["paper-a", "paper-b", "paper-c"]


def brief_read() -> DeliveredRead:
    text = (FIXTURE / "input" / "brief.md").read_text()
    return DeliveredRead(
        source_ref=SourceRef(source_id="brief"),
        selector=LineSelector(start=1, end=len(text.splitlines())),
        content=text,
        read_receipt_id="rr-brief",
        label="brief",
    )


def packet(reads: list[DeliveredRead]) -> Packet:
    return Packet(
        packet_id="p1",
        task_id="plan-1",
        attempt_id="a1",
        role="planner",
        objective="Plan the research run.",
        requirements=[
            Requirement(id="req-1", source_passage="cites the literature", interpretation="cite papers"),
            Requirement(id="req-2", source_passage="exclusion rule", interpretation="judge the exclusion"),
        ],
        reads=reads,
        available_sources=[SourceInfo(source_id=s, path=f"papers/{s}.md") for s in SOURCES],
    )


def test_fake_plan_shape():
    result = planner.fake(packet([brief_read()]), KEY)
    AgentResult.model_validate(result.model_dump())
    plan = planner.PAYLOAD.model_validate(result.payload)
    assert result.outcome == "produced"
    assert result.model_calls == 0
    assert [t.role for t in result.proposed_tasks] == ["evidence_reader"] * 3 + ["writer", "reviewer"]
    assert [t.source_ids for t in result.proposed_tasks[:3]] == [[s] for s in SOURCES]
    assert all("seasonal-naive" in t.objective for t in result.proposed_tasks[:3])
    assert set(plan.coverage) == {"req-1", "req-2"}
    assert all(plan.coverage[r] for r in plan.coverage)


def test_fake_needs_brief():
    result = planner.fake(packet([]), KEY)
    AgentResult.model_validate(result.model_dump())
    planner.PAYLOAD.model_validate(result.payload)
    assert result.outcome == "needs_dependency"
    assert result.unresolved
    assert not result.proposed_tasks
    assert result.model_calls == 0


def test_user_message_frames_documents_and_numbers_lines():
    msg = planner.user_message(packet([brief_read()]))
    assert "<document" in msg and "</document>" in msg
    assert "3: Question: Is a seasonal-naive" in msg
    assert "req-1" in msg and "paper-b" in msg


def test_user_message_includes_review_feedback():
    p = packet([brief_read()]).model_copy(update={"review_feedback": ["cover req-2"]})
    assert "cover req-2" in planner.user_message(p)


def test_system_demands_one_json_object():
    assert "ONE JSON object" in planner.SYSTEM
    assert "coverage" in planner.SYSTEM
