import json

import pytest

from ar.adapter import LiveAgent
from ar.agents import research_agent
from ar.agents.fake import FakeAgent
from ar.agents.roles import ROLE_MODULES, get_role
from ar.models import ClaimDraft, LineSelector, SourceRef
from ar.packet import DeliveredRead, DependencyItem, Packet, SourceInfo


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.replies.pop(0)


def _packet(**kw) -> Packet:
    return Packet(packet_id="p", task_id="t", attempt_id="a", role="research_agent", objective="o", **kw)


def _hyp(**kw):
    h = {
        "statement": "s",
        "competing_explanations": ["c"],
        "falsifiers": ["f"],
        "proposed_test": "t",
    }
    return {**h, **kw}


def _checks():
    return [
        {"id": check_id, "status": "pass", "note": label}
        for check_id, label in research_agent.RESEARCH_CHECKS
    ]


def test_research_agent_registered_and_fake_dispatches():
    assert ROLE_MODULES["research_agent"] == "ar.agents.research_agent"
    mod = get_role("research_agent")
    assert mod.PAYLOAD is research_agent.ResearchAssessment
    res = FakeAgent({}).run("research_agent", _packet(available_sources=[SourceInfo(source_id="s1", path="p")]))
    assert res.outcome == "produced"
    assert res.payload["hypotheses"][0]["falsifiers"]
    assert [check["id"] for check in res.payload["checks"]] == [c[0] for c in research_agent.RESEARCH_CHECKS]
    assert res.proposed_tasks[0].role == "source_scout"


def test_research_agent_adds_default_methods():
    msg = research_agent.user_message(_packet())
    for name in research_agent.DEFAULT_SKILLS:
        assert f'<skill name="{name}"' in msg
    assert "Method (no extra capabilities)" in msg


def test_research_agent_packet_renders_sources_claims_and_documents():
    read = DeliveredRead(
        source_ref=SourceRef(source_id="paper-a"),
        selector=LineSelector(start=2, end=2),
        content="Seasonal naive is adequate.",
        read_receipt_id="rr",
    )
    claim = ClaimDraft(statement="Short histories can be useful.")
    msg = research_agent.user_message(
        _packet(
            reads=[read],
            dependencies=[DependencyItem(kind="claim", id="c1", claim=claim)],
        )
    )
    assert '<document label="" source_id="paper-a"' in msg
    assert "2: Seasonal naive is adequate." in msg
    assert '<claim id="c1"' in msg


@pytest.mark.parametrize("field", ["competing_explanations", "falsifiers"])
def test_research_hypothesis_requires_non_blank_alternatives(field):
    with pytest.raises(ValueError):
        research_agent.ResearchHypothesis(**_hyp(**{field: [""]}))


def test_research_assessment_requires_all_nine_checks():
    with pytest.raises(ValueError):
        research_agent.ResearchAssessment(answer="a", checks=_checks()[:-1])


def test_live_research_agent_repairs_missing_falsifier():
    bad = {
        "outcome": "produced",
        "payload": {
            "answer": "a",
            "checks": _checks(),
            "hypotheses": [{k: v for k, v in _hyp().items() if k != "falsifiers"}],
        },
    }
    good = {
        "outcome": "produced",
        "payload": {"answer": "a", "checks": _checks(), "hypotheses": [_hyp()]},
    }
    client = Scripted([json.dumps(bad), json.dumps(good)])
    res = LiveAgent(client).run("research_agent", _packet())
    assert res.outcome == "produced" and res.model_calls == 2
    assert res.payload["hypotheses"][0]["falsifiers"] == ["f"]
    assert "falsifiers" in client.calls[1][1]
