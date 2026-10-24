"""Exercise the complete research team through the controller and Studio event feed."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ar import codex_runtime, db
from ar.agents.fake import FakeAgent
from ar.controller import RESEARCH_ORDER, RunOutcome, _planned_middle, run_project
from ar.models import ProposedTask
from ar.packet import DependencyItem, Packet
from ar.skills import load_skill
from ar.studio.session import Session

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ResearchTeam:
    def __init__(self, *, experiments=False):
        self.fake = FakeAgent()
        self.packets = {}
        self.experiments = experiments

    def run(self, role, packet):
        self.packets.setdefault(role, []).append(packet)
        result = self.fake.run(role, packet)
        if role == "planner":
            tasks = [task for task in result.proposed_tasks if task.role == "evidence_reader"]
            tasks += [ProposedTask(role=name, objective=f"Do the {name} assignment.")
                      for name in ("source_scout", "research_agent", "theorist", "synthesis_agent", "adversarial_effect_agent")]
            if self.experiments:
                tasks += [ProposedTask(role=name, objective=f"Execute {name}.", source_ids=["spec-1"])
                          for name in ("experiment_runner", "reproducer")]
                tasks.append(ProposedTask(role="analyst", objective="Compare the experiment outputs."))
            result = result.model_copy(update={"proposed_tasks": tasks})
        return result


def forbid_codex(*args, **kwargs):
    raise AssertionError("A research run must not start Codex")


def test_every_research_specialist_executes_and_hands_results_to_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runtime.CodexRuntime, "__init__", forbid_codex)
    shutil.copytree(FIXTURES / "baselines" / "input", tmp_path / "input")
    shutil.copytree(FIXTURES / "experiment", tmp_path / "fixtures" / "experiment")
    (tmp_path / "input" / "experiment.yaml").write_text(
        "id: spec-1\nquestion: Compare the baseline\nstatus: pilot\n"
        "input_files: [fixtures/experiment/data.csv, fixtures/experiment/baseline_mae.py]\n"
        "command: [python, baseline_mae.py, --seed, '7']\nworking_dir: fixtures/experiment\n"
        "seed: 7\nmetrics: [mae]\nsuccess_criteria: [mae computed]\n"
        "expected_outputs: [results.json]\ntimeout_seconds: 60\n", encoding="utf-8",
    )
    db.init_project(tmp_path).close()
    team = ResearchTeam(experiments=True)
    assert run_project(tmp_path, team, max_tasks=16) is RunOutcome.COMPLETED
    # Experiment execution belongs to the controller; those steps do not call a model.
    assert set(RESEARCH_ORDER) - {"experiment_runner", "reproducer"} <= team.packets.keys()
    with db.connect(tmp_path) as conn:
        rows = conn.execute("SELECT role, state FROM tasks ORDER BY rowid").fetchall()
        assert all(row["state"] == "accepted" for row in rows)
        roles = [row["role"] for row in rows]
        assert set(RESEARCH_ORDER) <= set(roles)
        assert roles.index("analyst") < roles.index("synthesis_agent") < roles.index("adversarial_effect_agent") < roles.index("writer")
    for name in ("synthesis_agent", "adversarial_effect_agent"):
        packet = team.packets[name][0]
        assert packet.claim_items()
        assert f"Do the {name} assignment." in packet.objective
    upstream = [json.loads(dep.content)["role"] for dep in team.packets["writer"][0].dependencies
                if dep.kind == "artifact" and '"role"' in dep.content]
    assert {"research_agent", "theorist", "analyst", "synthesis_agent", "adversarial_effect_agent"} <= set(upstream)
    synth_id = team.packets["synthesis_agent"][0].task_id
    assert team.packets["adversarial_effect_agent"][0].dependencies[0].id == synth_id


def test_studio_stream_contains_real_synthesis_and_adversarial_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runtime.CodexRuntime, "__init__", forbid_codex)
    monkeypatch.setattr("ar.studio.session.search_crossref", lambda *args: [])
    team = ResearchTeam()
    session = Session(tmp_path, lambda: team)
    session.use_demo()
    session.start()
    session.wait(60)
    assert session.status == "done", session.report
    events = [event for event in session.since(0) if event["type"] == "runtime"]
    for role in ("research_agent", "theorist", "synthesis_agent", "adversarial_effect_agent"):
        dispatched = next(event for event in events if event["event"] == "packet.dispatched" and event["payload"]["packet"]["role"] == role)
        accepted = next(event for event in events if event["event"] == "attempt.accepted" and event["payload"]["role"] == role)
        assert dispatched["seq"] < accepted["seq"]
        assert accepted["payload"]["outcome"] == "produced"
    assert session.report["cited"] == ["claim-1", "claim-2"]


def test_unavailable_planner_roles_are_reported_instead_of_silently_dropped():
    from ar.models import AgentResult
    planner = AgentResult(role="planner", task_id="plan", outcome="produced",
                          proposed_tasks=[ProposedTask(role="literature_review", objective="Review independently")])
    _, rejected = _planned_middle(planner, ["paper-a"], experiment_spec=None, capacity=9)
    assert rejected[0]["task"]["role"] == "literature_review"
    assert "not available" in rejected[0]["reason"]


def test_reviewer_keeps_one_candidate_when_given_specialist_context():
    from ar.agents.reviewer import user_message
    packet = Packet(packet_id="review", task_id="review", attempt_id="one", role="reviewer", objective="Review the answer",
                    dependencies=[DependencyItem(kind="artifact", id="report", content="The candidate answer"),
                                  DependencyItem(kind="artifact", id="synthesis", content="Research synthesis"),
                                  DependencyItem(kind="artifact", id="adverse", content="Adverse-effect findings")])
    message = user_message(packet)
    assert message.count("Candidate draft (data)") == 1
    assert "Candidate draft (data): artifact_id=report" in message
    assert "Upstream research (data): artifact_id=synthesis" in message
    assert "Upstream research (data): artifact_id=adverse" in message


def test_concurrent_studio_poll_and_final_flush_publish_each_event_once(tmp_path):
    session = Session(tmp_path, None)
    session.use_demo()
    project = session.root / "poll-test"
    session._write_project(project)
    db.init_project(project).close()
    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: session._poll(project), range(16)))
    events = session.since(0)
    runtime = [event for event in events if event["type"] == "runtime"]
    keys = [(event["event"], event["run"], event["task"], json.dumps(event["payload"], sort_keys=True)) for event in runtime]
    assert len(keys) == len(set(keys))
    assert len([event for event in events if event["type"] == "quality" and event["kind"] == "accepted"]) == 2
    assert len([event for event in events if event["type"] == "quality" and event["kind"] == "review"]) == 1


@pytest.mark.parametrize("path", sorted((Path(__file__).resolve().parents[1] / "skills").glob("*.md")))
def test_installed_package_contains_each_research_method(path):
    packaged = Path(__file__).resolve().parents[1] / "src" / "ar" / "methods"
    assert load_skill(path.stem, packaged) == load_skill(path.stem, path.parent)
