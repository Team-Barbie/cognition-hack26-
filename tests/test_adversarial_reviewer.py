from __future__ import annotations

import json

from ar.adapter import LiveAgent
from ar.agents import adversarial_reviewer
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


def packet(content: str | None, deps: list[DependencyItem] = DEPS) -> Packet:
    items = list(deps)
    if content is not None:
        items.append(DependencyItem(kind="artifact", id="draft-1", revision=3, content=content))
    return Packet(
        packet_id="p-adv",
        task_id="t-adv-review",
        attempt_id="a1",
        role="adversarial_reviewer",
        objective="Stress-test the draft before acceptance",
        dependencies=items,
    )


def review(content: str | None) -> tuple[AgentResult, adversarial_reviewer.AdversarialReviewResult]:
    result = adversarial_reviewer.fake(packet(content), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.model_calls == 0
    return result, adversarial_reviewer.AdversarialReviewResult.model_validate(result.payload)


def _payload(**kw):
    base = {
        "reviewed_artifact_id": "draft-1",
        "reviewed_revision": 3,
        "checks": {"non_empty": True},
        "findings": [],
        "stress_tests": ["What would weaken this?"],
        "revision_plan": ["No change."],
        "disposition": "accept",
    }
    return {**base, **kw}


def test_adversarial_reviewer_registered_and_fake_dispatches():
    assert ROLE_MODULES["adversarial_reviewer"] == "ar.agents.adversarial_reviewer"
    mod = get_role("adversarial_reviewer")
    assert mod.PAYLOAD is adversarial_reviewer.AdversarialReviewResult
    res = FakeAgent(KEY).run(
        "adversarial_reviewer",
        packet("Baseline [claim:c1]. Contradiction [claim:c2]. Falsifier: contrary data would weaken this."),
    )
    assert res.outcome == "produced"
    assert res.role == "adversarial_reviewer"


def test_good_draft_accepted_with_falsifier():
    _, rr = review("Baseline [claim:c1]. Contradiction [claim:c2]. Falsifier: contrary data would weaken this.")
    assert rr.disposition == "accept"
    assert rr.checks["citations_resolve"]
    assert rr.checks["contrary_evidence_addressed"]
    assert rr.checks["falsifiable"]
    assert not any(f.severity == "blocking" for f in rr.findings)


def test_missing_contrary_evidence_blocks():
    _, rr = review("Baseline [claim:c1]. Falsifier: contrary data would weaken this.")
    assert rr.disposition == "revise"
    assert not rr.checks["contrary_evidence_addressed"]
    assert any(f.attack == "contrary_evidence" and f.target_id == "c2" for f in rr.findings)


def test_uncited_artifact_with_claims_blocks_as_unsupported():
    _, rr = review("Seasonal naive is best. Falsifier: contrary data would weaken this.")
    assert rr.disposition == "revise"
    assert any(f.attack == "unsupported_claim" for f in rr.findings)
    assert rr.revision_plan


def test_missing_falsifier_blocks_even_when_cited():
    _, rr = review("Baseline [claim:c1]. Contradiction [claim:c2].")
    assert rr.disposition == "revise"
    assert not rr.checks["falsifiable"]
    assert any(f.attack == "missing_falsifier" for f in rr.findings)


def test_no_artifact_needs_dependency():
    result = adversarial_reviewer.fake(packet(None), KEY)
    AgentResult.model_validate(result.model_dump())
    assert result.outcome == "needs_dependency" and result.payload == {}


def test_user_message_includes_autoresearch_methods():
    msg = adversarial_reviewer.user_message(packet("hello"))
    assert '<artifact id="draft-1" revision="3">' in msg
    for name in adversarial_reviewer.DEFAULT_SKILLS:
        assert f'<skill name="{name}"' in msg
    assert "Method (no extra capabilities)" in msg


def test_live_adversarial_reviewer_repairs_invalid_payload():
    bad = {
        "outcome": "produced",
        "payload": {"reviewed_artifact_id": "draft-1", "reviewed_revision": 3, "disposition": "accept"},
    }
    good = {"outcome": "produced", "payload": _payload()}
    client = Scripted([json.dumps(bad), json.dumps(good)])
    res = LiveAgent(client).run("adversarial_reviewer", packet("x [claim:c1]"))
    assert res.outcome == "produced" and res.model_calls == 2
    assert res.payload["stress_tests"] == ["What would weaken this?"]
    assert "stress_tests" in client.calls[1][1]
