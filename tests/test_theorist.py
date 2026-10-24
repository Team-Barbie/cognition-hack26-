import pytest
from pydantic import ValidationError

from ar.agents import theorist
from ar.agents.fake import FakeAgent
from ar.models import ClaimDraft
from ar.packet import DependencyItem, Packet


def _packet(**kw) -> Packet:
    return Packet(packet_id="p", task_id="t", attempt_id="a", role="theorist", objective="o", **kw)


def _claims() -> list[DependencyItem]:
    texts = ["Seasonal naive is a default baseline.", "Excluding series shorter than 52 weeks is unnecessary."]
    return [DependencyItem(kind="claim", id=f"c{i}", claim=ClaimDraft(statement=t)) for i, t in enumerate(texts, 1)]


def test_fake_derives_valid_hypothesis():
    res = FakeAgent({}).run("theorist", _packet(dependencies=_claims()))
    assert res.outcome == "produced" and res.model_calls == 0
    hs = theorist.HypothesisSet.model_validate(res.payload)
    h = hs.hypotheses[0]
    assert "short histories" in h.statement and "c2" in h.statement
    assert h.competing_explanations and h.falsifiers
    assert hs.experiment_spec["hypothesis_id"] == h.id


def test_fake_without_claims_is_unable_to_complete():
    res = FakeAgent({}).run("theorist", _packet())
    assert res.outcome == "unable_to_complete" and res.unresolved


@pytest.mark.parametrize("field", ["competing_explanations", "falsifiers"])
def test_hypothesis_requires_alternatives(field):
    base = dict(id="h", statement="s", competing_explanations=["c"], falsifiers=["f"], proposed_test="t")
    theorist.Hypothesis(**base)
    for bad in ([], [" "]):
        with pytest.raises(ValidationError):
            theorist.Hypothesis(**{**base, field: bad})


def test_no_field_for_observed_results():
    with pytest.raises(ValidationError):
        theorist.Hypothesis(id="h", statement="s", competing_explanations=["c"], falsifiers=["f"],
                            proposed_test="t", observed_result="it worked")
    assert not any("observ" in f for f in theorist.Hypothesis.model_fields)


def test_user_message_frames_claims_as_data():
    msg = theorist.user_message(_packet(dependencies=_claims()))
    assert '<claim id="c1"' in msg and "untrusted" in theorist.SYSTEM
