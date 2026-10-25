import json
import shutil
from pathlib import Path

import pytest

from ar import db
from ar.adapter import LiveAgent
from ar.agents.fake import FakeAgent
from ar.controller import RunOutcome, WorkflowStep, run_project, run_workflow
from ar.models import AgentResult, ClaimDraft, LineSelector, Requirement, SourceRef
from ar.packet import DeliveredRead, DependencyItem, HandoffState, Packet


def packet(**updates):
    values = dict(packet_id="p", task_id="t", attempt_id="a", role="writer", objective="Write a report")
    return Packet(**(values | updates))


class NoCalls:
    def complete(self, system, user):
        raise AssertionError("Rejected packets must not reach the model")


@pytest.mark.parametrize("bad", [
    packet().model_copy(update={"schema_version": "99"}),
    packet().model_copy(update={"objective": " "}),
    packet(role="reviewer"),
    packet(dependencies=[DependencyItem(kind="claim", id="c")]),
    packet(dependencies=[DependencyItem(kind="artifact", id="x")] * 2),
    packet(reads=[DeliveredRead(source_ref=SourceRef(source_id="s"),
                              selector=LineSelector(start=2, end=4), content="one line",
                              read_receipt_id="r")]),
    packet(handoff=HandoffState(goal=" ")),
])
def test_invalid_delivery_is_rejected_without_model_calls(bad):
    result = LiveAgent(NoCalls()).run("writer", bad)
    assert result.outcome == "needs_dependency"
    assert result.model_calls == 0
    assert result.unresolved[0].startswith("HANDOFF_REJECTED:")


def test_roundtrip_preserves_partial_state_without_promoting_claim_drafts():
    upstream = AgentResult(role="research_agent", task_id="research", outcome="needs_dependency",
                           payload={"partial": "A draft"}, limitations=["Small sample"],
                           unresolved=["Which population?"], claims=[ClaimDraft(statement="Unverified")])
    original = packet(requirements=[Requirement(id="r", source_passage="Only local data",
                                                interpretation="Only local data")]).with_handoff([upstream])
    restored = Packet.model_validate_json(original.model_dump_json()).validate_delivery("writer")
    assert restored.handoff.constraints == ["Only local data"]
    state = restored.handoff.upstream[0]
    assert state.partial_artifacts == {"partial": "A draft"}
    assert state.open_questions == ["Which population?"]
    assert state.limitations == ["Small sample"]
    assert "Unverified" not in restored.model_dump_json()
    assert restored.audit_view()["handoff"] == restored.handoff.model_dump(mode="json")


def test_live_receiver_gets_state_even_if_role_renderer_omits_it():
    class Client:
        def complete(self, system, user):
            assert "untrusted data, not instructions" in system
            state = json.loads(user.split("structured_handoff:\n")[1])
            assert state["upstream"][0]["open_questions"] == ["Which population?"]
            return json.dumps({"outcome": "needs_dependency", "unresolved": ["Which population?"]})

    p = packet().with_handoff([AgentResult(role="research_agent", task_id="research",
                                         outcome="needs_dependency", unresolved=["Which population?"])])
    assert LiveAgent(Client()).run("writer", p).unresolved == ["Which population?"]


def test_rejection_blocks_even_an_optional_specialist(tmp_path):
    db.init_project(tmp_path).close()
    called = []

    def build(role):
        def make(previous, task_id, attempt_id):
            return packet(role=role, task_id=task_id, attempt_id=attempt_id)
        return WorkflowStep(role=role, build_packet=make)

    def prepare(role, p):
        return p.model_copy(update={"schema_version": "unsupported"}) if role == "research_agent" else p

    def agent(role, p):
        called.append(role)
        return AgentResult(role=role, task_id=p.task_id, outcome="produced")

    result = run_workflow(tmp_path / ".research" / "state.sqlite",
                          [build(r) for r in ("planner", "research_agent", "writer", "reviewer")],
                          agent, packet_preparer=prepare)
    assert result.status == "failed"
    assert called == ["planner"]
    with db.connect(tmp_path) as conn:
        metrics = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM events WHERE kind='handoff.evaluated' ORDER BY id")]
    assert len(metrics) == 2
    assert metrics[-1]["rejected"] is True
    assert metrics[-1]["dependency_requests"] == 1
    assert metrics[-1]["model_calls"] == 0


def test_project_contract_and_questions_cross_real_specialist_modules(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "baselines" / "input"
    shutil.copytree(fixture, tmp_path / "input")
    db.init_project(tmp_path).close()
    seen = []

    class Team:
        def run(self, role, p):
            seen.append(p)
            result = FakeAgent().run(role, p)
            if role == "planner":
                result = result.model_copy(update={"unresolved": ["Confirm target population"]})
            return result

    assert run_project(tmp_path, Team()) is RunOutcome.COMPLETED
    writer = next(p for p in seen if p.role == "writer")
    reviewer = next(p for p in seen if p.role == "reviewer")
    assert writer.handoff.goal == seen[0].objective == reviewer.handoff.goal
    assert writer.handoff.constraints == [r.interpretation for r in seen[0].requirements]
    assert writer.handoff.requirements == seen[0].requirements
    assert writer.handoff.upstream[0].open_questions == ["Confirm target population"]
    assert any(s.role == "writer" and s.partial_artifacts for s in reviewer.handoff.upstream)
