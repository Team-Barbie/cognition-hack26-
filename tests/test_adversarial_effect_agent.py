from __future__ import annotations

import json

from ar.adapter import LiveAgent
from ar.agents import adversarial_effect_agent
from ar.agents.fake import FakeAgent
from ar.agents.roles import ROLE_MODULES, get_role
from ar.models import AgentResult
from ar.packet import DependencyItem, Packet
from tests.test_writer import DEPS, KEY


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.replies.pop(0)


def packet(content: str | None, deps: list[DependencyItem] | None = None) -> Packet:
    items = list(DEPS if deps is None else deps)
    if content is not None:
        items.append(DependencyItem(kind="artifact", id="draft-1", revision=3, content=content))
    return Packet(
        packet_id="p-effect",
        task_id="t-effect",
        attempt_id="a1",
        role="adversarial_effect_agent",
        objective="Rank the review effect",
        dependencies=items,
    )


def effect(content: str | None, deps: list[DependencyItem] | None = None):
    result = adversarial_effect_agent.fake(packet(content, deps), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.model_calls == 0
    payload = adversarial_effect_agent.ReviewEffect.model_validate(result.payload)
    return result, payload


def _payload(**kw):
    base = {
        "central_claim": "c",
        "decision_risk": "note",
        "confidence_after_review": "high",
        "claim_audit": [],
        "counterarguments": [],
        "missing_tests": ["No decision-changing missing test was detected from the supplied packet."],
        "fastest_path_to_acceptance": ["Accept."],
        "disposition": "accept",
    }
    return {**base, **kw}


def test_effect_agent_registered_and_fake_dispatches():
    assert ROLE_MODULES["adversarial_effect_agent"] == "ar.agents.adversarial_effect_agent"
    mod = get_role("adversarial_effect_agent")
    assert mod.PAYLOAD is adversarial_effect_agent.ReviewEffect
    res = FakeAgent(KEY).run(
        "adversarial_effect_agent",
        packet("Baseline [claim:c1]. Falsifier: contrary data would weaken this.", [DEPS[0]]),
    )
    assert res.outcome == "produced"
    assert res.role == "adversarial_effect_agent"


def test_accepts_narrow_source_backed_claim_with_falsifier():
    _, payload = effect("Baseline [claim:c1]. Falsifier: contrary data would weaken this.", [DEPS[0]])
    assert payload.disposition == "accept"
    assert payload.decision_risk == "note"
    assert payload.confidence_after_review == "high"
    assert payload.adverse_effects[0].route_to == "reviewer"
    assert payload.claim_audit[0].status == "direct_evidence"


def test_scope_leap_changes_decision_to_major_revision():
    result, payload = effect("Seasonal naive is best [claim:c1]. Falsifier: contrary data would weaken this.", [DEPS[0]])
    assert payload.disposition == "revise"
    assert payload.decision_risk == "major"
    assert payload.claim_audit[0].status == "scope_leap"
    assert any(effect.route_to == "writer" for effect in payload.adverse_effects)
    assert any(task.role == "writer" for task in result.proposed_tasks)


def test_unknown_citation_is_fatal():
    _, payload = effect("Baseline [claim:zzz]. Falsifier: contrary data would weaken this.", [DEPS[0]])
    assert payload.disposition == "reject"
    assert payload.decision_risk == "fatal"
    assert any(a.claim_id == "zzz" and a.status == "unsupported" for a in payload.claim_audit)
    assert any(effect.route_to == "evidence_reader" for effect in payload.adverse_effects)


def test_missing_falsifier_adds_theorist_task():
    result, payload = effect("Baseline [claim:c1].", [DEPS[0]])
    assert payload.disposition == "revise"
    assert payload.missing_tests
    assert any(task.role == "theorist" for task in result.proposed_tasks)


def test_no_artifact_needs_dependency():
    result = adversarial_effect_agent.fake(packet(None), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "needs_dependency" and result.payload == {}


def test_user_message_includes_autoresearch_methods_and_feedback():
    pkt = packet("hello")
    pkt.review_feedback.append("Narrow the scope.")
    msg = adversarial_effect_agent.user_message(pkt)
    assert '<artifact id="draft-1" revision="3">' in msg
    assert "<feedback>Narrow the scope.</feedback>" in msg
    for name in adversarial_effect_agent.DEFAULT_SKILLS:
        assert f'<skill name="{name}"' in msg
    assert "Method (no extra capabilities)" in msg


def test_live_effect_agent_repairs_invalid_payload():
    bad = {"outcome": "produced", "payload": {"central_claim": "c"}}
    good = {"outcome": "produced", "payload": _payload()}
    client = Scripted([json.dumps(bad), json.dumps(good)])
    res = LiveAgent(client).run("adversarial_effect_agent", packet("x [claim:c1]", [DEPS[0]]))
    assert res.outcome == "produced" and res.model_calls == 2
    assert res.payload["decision_risk"] == "note"
    assert "fastest_path_to_acceptance" in client.calls[1][1]
