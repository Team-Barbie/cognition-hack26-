import json
import shutil
from types import SimpleNamespace

import pytest

from ar import db
from ar.adapter import LiveAgent
from ar.agents.fake import FakeAgent
from ar.controller import RunOutcome, run_project
from ar.models import AgentResult
from ar.openai_client import DEFAULT_TEST_MODEL
from ar.packet import Packet
from ar.studio import server
from ar.studio.session import BOUNDED_RESEARCH_NOTE, Session, StageAgent
from tests.test_research_team_integration import FIXTURES, ResearchTeam
from tests.test_writer import DEPS


@pytest.mark.parametrize("role", ["research_agent", "synthesis_agent"])
@pytest.mark.parametrize("allowance", [1, 2])
def test_live_specialist_dependency_retry_uses_evidence_and_respects_budget(tmp_path, role, allowance):
    packet = Packet(packet_id="packet", task_id="task", attempt_id="attempt", role=role,
                    objective="Give a bounded assessment", dependencies=DEPS, model_call_budget=allowance)
    produced = FakeAgent().run(role, packet)
    messages = []

    class Client:
        def complete(self, system, user):
            messages.append(user)
            if len(messages) == 1:
                return json.dumps({"outcome": "needs_dependency", "unresolved": ["Need full papers and a new experiment"]})
            assert BOUNDED_RESEARCH_NOTE in user
            assert '"quote"' in user and '"scope"' in user and '"limitations"' in user
            return json.dumps({"outcome": "produced", "payload": produced.payload})

    result = StageAgent(LiveAgent(Client()), Session(tmp_path, None)).run(role, packet)
    assert result.model_calls == allowance and len(messages) == allowance
    assert result.outcome == ("produced" if allowance == 2 else "needs_dependency")


@pytest.mark.parametrize("role", ["research_agent", "synthesis_agent"])
def test_empty_research_packet_is_never_marked_successful(tmp_path, role):
    packet = Packet(packet_id="packet", task_id="task", attempt_id="attempt", role=role, objective="Assess")
    result = StageAgent(FakeAgent(), Session(tmp_path, None)).run(role, packet)
    assert result.outcome == "needs_dependency"


def test_incomplete_specialist_does_not_become_a_synthesis_artifact(tmp_path):
    shutil.copytree(FIXTURES / "baselines" / "input", tmp_path / "input")
    db.init_project(tmp_path).close()

    class NeedsInput(ResearchTeam):
        def run(self, role, packet):
            if role == "research_agent":
                return AgentResult(role=role, task_id=packet.task_id, outcome="needs_dependency", unresolved=["Missing catalogue"])
            return super().run(role, packet)

    team = NeedsInput()
    assert run_project(tmp_path, team) is RunOutcome.COMPLETED
    synth = team.packets["synthesis_agent"][0]
    assert synth.claim_items() and synth.reads
    assert not any('"outcome": "needs_dependency"' in dep.content for dep in synth.dependencies)
    provenance = json.loads((tmp_path / "output" / "provenance.json").read_text())
    assert any("research_agent" in note and "Missing catalogue" in note for note in provenance["limitations"])


def test_demo_and_explicit_workflow_tests_use_nano_without_changing_research_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-token")
    monkeypatch.delenv("STUDIO_TEST_MODEL", raising=False)
    monkeypatch.setattr(server, "make_client", lambda provider, model: SimpleNamespace(model=model or "normal-model"))
    monkeypatch.setattr("ar.studio.session.search_crossref", lambda *args: [])
    seen = []

    def run(root, agent, **kwargs):
        seen.append(agent.inner.client.model)
        return RunOutcome.COMPLETED

    monkeypatch.setattr("ar.controller.run_project", run)
    studio = server.Studio(tmp_path, provider="openai", model="normal-model")
    session = studio.new_session()
    session.use_demo()
    session.start()
    session.wait(60)
    assert seen == [DEFAULT_TEST_MODEL]
    assert session.client.model == "normal-model" and studio.model == "normal-model"
    session.start(test_run=False)
    session.wait(60)
    assert seen == [DEFAULT_TEST_MODEL, "normal-model"]
    session.start(test_run=True)
    session.wait(60)
    assert seen == [DEFAULT_TEST_MODEL, "normal-model", DEFAULT_TEST_MODEL]
    assert any(event["type"] == "run_config" and event["model"] == DEFAULT_TEST_MODEL for event in session.since(0))


def test_demo_without_gpt_credentials_uses_fixture_instead_of_expensive_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server, "make_client", lambda *args: object())
    studio = server.Studio(tmp_path, provider="anthropic")
    session = studio.new_session()
    assert isinstance(session.make_test_agent(), FakeAgent)
    assert session.test_model is None


def test_terra_test_client_uses_low_reasoning_and_normal_client_keeps_its_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-token")
    monkeypatch.delenv("STUDIO_TEST_MODEL", raising=False)
    studio = server.Studio(tmp_path, provider="openai")
    session = studio.new_session()
    assert session.make_test_agent().client.reasoning_effort == "low"
    assert session.make_agent().client.reasoning_effort is None


def test_demo_cannot_replace_sources_during_a_running_job(tmp_path):
    session = Session(tmp_path, None)
    session._thread = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="wait for the run"):
        session.use_demo()
