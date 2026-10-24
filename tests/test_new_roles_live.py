import json

from ar import agents
from ar.adapter import LiveAgent
from ar.agents.fake import FakeAgent
from ar.agents.roles import ROLE_MODULES, get_role
from ar.models import ClaimDraft
from ar.packet import DependencyItem, Packet, SourceInfo


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.replies.pop(0)


def _packet(role, **kw) -> Packet:
    return Packet(packet_id="p", task_id="t", attempt_id="a", role=role, objective="o", **kw)


def _hyp(**kw):
    h = {"id": "h-1", "statement": "s", "competing_explanations": ["c"], "falsifiers": ["f"], "proposed_test": "t"}
    return {**h, **kw}


def test_registry_and_fake_dispatch():
    assert {"source_scout", "theorist", "analyst"} <= set(ROLE_MODULES)
    for role in ("source_scout", "theorist", "analyst"):
        mod = get_role(role)
        assert callable(mod.fake) and callable(mod.user_message) and mod.SYSTEM and mod.PAYLOAD
    assert FakeAgent({}).run("analyst", _packet("analyst")).role == "analyst"


def test_live_theorist_repairs_missing_falsifier():
    missing = {k: v for k, v in _hyp().items() if k != "falsifiers"}
    bad = {"outcome": "produced", "payload": {"hypotheses": [missing]}}
    good = {"outcome": "produced", "payload": {"hypotheses": [_hyp()], "experiment_spec": None}}
    client = Scripted([json.dumps(bad), json.dumps(good)])
    claim = DependencyItem(kind="claim", id="c1", claim=ClaimDraft(statement="x"))
    res = LiveAgent(client).run("theorist", _packet("theorist", dependencies=[claim], skills=["hypothesis_design"]))
    assert res.outcome == "produced" and res.model_calls == 2
    assert res.payload["hypotheses"][0]["falsifiers"] == ["f"]
    assert "falsifiers" in client.calls[1][1] and "Method (no extra capabilities)" in client.calls[0][1]


def test_live_theorist_gives_up_after_one_repair():
    bad = json.dumps({"payload": {"hypotheses": [_hyp(falsifiers=[])]}})
    res = LiveAgent(Scripted([bad, bad])).run("theorist", _packet("theorist"))
    assert res.outcome == "unable_to_complete" and res.model_calls == 2


def test_live_scout_and_analyst_validate_payloads():
    screened = {"source_id": "a", "decision": "include", "reason": "r", "basis": "metadata"}
    scout = {"payload": {"query": "q", "screened": [screened]}}
    sources = [SourceInfo(source_id="a", path="a")]
    res = LiveAgent(Scripted([json.dumps(scout)])).run("source_scout", _packet("source_scout", available_sources=sources))
    assert res.payload["screened"][0]["basis"] == "metadata"
    bad = {"payload": {"summary": "s", "observations": [{"experiment_run_id": "r", "metric": "m"}],
                       "limitations": [], "failure_modes": []}}
    res = LiveAgent(Scripted([json.dumps(bad), json.dumps(bad)])).run("analyst", _packet("analyst"))
    assert res.outcome == "unable_to_complete"


def test_agents_run_entry_point_dispatches_new_roles():
    agents.configure(FakeAgent({}))
    assert agents.run("theorist", _packet("theorist")).outcome == "unable_to_complete"
