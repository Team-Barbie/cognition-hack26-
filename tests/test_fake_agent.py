from types import ModuleType

import pytest

from ar import agents
from ar.agents import roles
from ar.agents.fake import ANSWER_KEY_PATH, FakeAgent
from ar.models import AgentResult, Record
from ar.packet import Packet


class StubPayload(Record):
    text: str


def make_stub() -> ModuleType:
    mod = ModuleType("stub_writer")
    mod.PAYLOAD = StubPayload
    mod.SYSTEM = "stub"
    mod.user_message = lambda packet: packet.objective

    def fake(packet: Packet, answer_key: dict) -> AgentResult:
        return AgentResult(
            role=packet.role,
            task_id=packet.task_id,
            outcome="produced",
            payload={"text": str(answer_key.get("marker", "none"))},
        )

    mod.fake = fake
    return mod


def packet(role: str = "writer") -> Packet:
    return Packet(packet_id="p1", task_id="t1", attempt_id="a1", role=role, objective="do it")


@pytest.fixture(autouse=True)
def stubs():
    roles.OVERRIDES["writer"] = make_stub()
    yield
    roles.OVERRIDES.clear()
    agents._agent = None


def test_dispatch_passes_packet_and_answer_key():
    result = FakeAgent({"marker": "k"}).run("writer", packet())
    assert result.outcome == "produced"
    assert result.task_id == "t1"
    assert result.payload == {"text": "k"}


def test_role_mismatch_raises():
    with pytest.raises(ValueError):
        FakeAgent({}).run("reviewer", packet("writer"))


def test_configure_run_round_trip():
    agents.configure(FakeAgent({"marker": "rt"}))
    assert agents.run("writer", packet()).payload == {"text": "rt"}


def test_default_answer_key_loads_lazily_from_fixture():
    agent = FakeAgent()
    assert agent._key is None
    assert ANSWER_KEY_PATH.exists()
    assert "sources" in agent.answer_key


def test_unknown_role_rejected():
    with pytest.raises(KeyError):
        roles.get_role("nope")
