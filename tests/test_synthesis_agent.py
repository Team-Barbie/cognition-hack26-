from __future__ import annotations

import json

from ar.adapter import LiveAgent
from ar.agents import synthesis_agent
from ar.agents.fake import FakeAgent
from ar.agents.roles import ROLE_MODULES, get_role
from ar.models import AgentResult, LineSelector, SourceRef
from ar.packet import DeliveredRead, DependencyItem, Packet, SourceInfo
from tests.test_writer import DEPS, KEY


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.replies.pop(0)


def packet(**kw) -> Packet:
    return Packet(packet_id="p-synth", task_id="t-synth", attempt_id="a1", role="synthesis_agent", objective="Synthesize the run", **kw)


def read() -> DeliveredRead:
    return DeliveredRead(
        source_ref=SourceRef(source_id="paper-a"),
        selector=LineSelector(start=4, end=5),
        content="Seasonal naive is adequate.\nShort histories remain uncertain.",
        read_receipt_id="rr-paper-a",
    )


def _payload(**kw):
    base = {
        "thesis": "Bounded synthesis.",
        "themes": [],
        "tensions": [],
        "gaps": [],
        "writer_brief": "Write carefully.",
        "handoff_tasks": [],
    }
    return {**base, **kw}


def synth(pkt: Packet):
    result = synthesis_agent.fake(pkt, KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.model_calls == 0
    return result, synthesis_agent.SynthesisOutput.model_validate(result.payload)


def test_synthesis_agent_registered_and_fake_dispatches():
    assert ROLE_MODULES["synthesis_agent"] == "ar.agents.synthesis_agent"
    mod = get_role("synthesis_agent")
    assert mod.PAYLOAD is synthesis_agent.SynthesisOutput
    result = FakeAgent(KEY).run("synthesis_agent", packet(dependencies=DEPS))
    assert result.outcome == "produced"
    assert result.role == "synthesis_agent"


def test_synthesizes_claims_and_routes_to_writer_reviewer_and_adverse_effects():
    result, payload = synth(packet(dependencies=DEPS))
    assert payload.thesis
    assert any(theme.title == "Accepted evidence" for theme in payload.themes)
    assert any("c2" in tension for tension in payload.tensions)
    roles = {task.role for task in result.proposed_tasks}
    assert {"adversarial_effect_agent", "writer", "reviewer"} <= roles
    assert result.proposed_tasks == payload.handoff_tasks


def test_reads_without_claims_route_to_evidence_reader():
    result, payload = synth(packet(reads=[read()]))
    assert any(theme.title == "Delivered source context" for theme in payload.themes)
    assert payload.gaps
    assert any(task.role == "evidence_reader" and task.source_ids == ["paper-a"] for task in result.proposed_tasks)


def test_available_sources_without_evidence_route_to_scout_and_research_agent():
    result, payload = synth(packet(available_sources=[SourceInfo(source_id="s1", path="input/s1.md")]))
    assert payload.themes[0].basis == "gap"
    roles = {task.role for task in result.proposed_tasks}
    assert {"source_scout", "research_agent"} <= roles


def test_no_material_needs_dependency():
    result = synthesis_agent.fake(packet(), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "needs_dependency" and result.payload == {}


def test_user_message_includes_synthesis_and_literature_methods():
    msg = synthesis_agent.user_message(packet(dependencies=DEPS, reads=[read()], review_feedback=["fix scope"]))
    assert '<claim id="c1"' in msg
    assert '<document label="" source_id="paper-a"' in msg
    assert "<feedback>fix scope</feedback>" in msg
    for name in synthesis_agent.DEFAULT_SKILLS:
        assert f'<skill name="{name}"' in msg
    assert "Method (no extra capabilities)" in msg


def test_live_synthesis_agent_repairs_missing_writer_brief():
    bad = {"outcome": "produced", "payload": {"thesis": "x"}}
    good = {"outcome": "produced", "payload": _payload()}
    client = Scripted([json.dumps(bad), json.dumps(good)])
    res = LiveAgent(client).run("synthesis_agent", packet(dependencies=DEPS))
    assert res.outcome == "produced" and res.model_calls == 2
    assert res.payload["writer_brief"] == "Write carefully."
    assert "writer_brief" in client.calls[1][1]
