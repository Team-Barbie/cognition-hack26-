import sqlite3
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ar.cli import PROJECT_DIRECTORIES, app
from ar.controller import TaskState, WorkflowStep, require_transition, run_workflow
from ar.db import connect, latest_active_run, read_status
from ar.models import AgentResult
from ar.packet import Packet


runner = CliRunner()
FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"


def test_init_creates_project_and_is_idempotent(tmp_path):
    first = runner.invoke(app, ["init", str(tmp_path)])
    assert first.exit_code == 0, first.output
    database = tmp_path / ".research" / "state.sqlite"
    assert database.is_file()
    assert all((tmp_path / directory).is_dir() for directory in PROJECT_DIRECTORIES)

    with sqlite3.connect(database) as connection:
        first_id = connection.execute("SELECT id FROM project").fetchone()[0]

    second = runner.invoke(app, ["init", str(tmp_path)])
    assert second.exit_code == 0, second.output
    with sqlite3.connect(database) as connection:
        second_id = connection.execute("SELECT id FROM project").fetchone()[0]
    assert first_id == second_id


def test_status_reports_empty_initialized_project(tmp_path):
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 0
    assert "Runs: 0" in result.output
    assert "Tasks: 0" in result.output


def test_status_requires_an_initialized_project(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 1
    assert "ar init" in result.output


def test_task_state_transitions_are_explicit():
    require_transition(TaskState.PENDING, TaskState.READY)
    require_transition(TaskState.RUNNING, TaskState.ACCEPTED)
    with pytest.raises(ValueError, match="invalid task transition"):
        require_transition(TaskState.ACCEPTED, TaskState.RUNNING)


def _step(role):
    def packet(previous, task_id, attempt_id):
        return Packet(
            packet_id=f"packet-{role}",
            task_id=task_id,
            attempt_id=attempt_id,
            role=role,
            objective="test the Wave 1 order",
        )

    return WorkflowStep(role=role, build_packet=packet)


def test_wave_one_controller_runs_fixed_order_and_saves_status(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])
    seen = []

    def fake_runner(role, packet):
        seen.append(role)
        return AgentResult(role=role, task_id=packet.task_id, outcome="produced")

    steps = [_step("planner"), _step("evidence_reader"), _step("writer"), _step("reviewer")]
    result = run_workflow(
        tmp_path / ".research" / "state.sqlite", steps, fake_runner
    )

    assert result.status == "completed"
    assert seen == ["planner", "evidence_reader", "writer", "reviewer"]
    status = read_status(tmp_path / ".research" / "state.sqlite")
    assert status.latest_run_status == "completed"
    assert status.task_states == {"accepted": 4}


def test_wave_one_controller_stops_after_failed_agent_result(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])

    def fake_runner(role, packet):
        outcome = "needs_dependency" if role == "evidence_reader" else "produced"
        return AgentResult(role=role, task_id=packet.task_id, outcome=outcome)

    steps = [_step("planner"), _step("evidence_reader"), _step("writer"), _step("reviewer")]
    result = run_workflow(
        tmp_path / ".research" / "state.sqlite", steps, fake_runner
    )

    assert result.status == "failed"
    status = read_status(tmp_path / ".research" / "state.sqlite")
    assert status.task_states == {"accepted": 1, "failed": 1}


def test_wave_one_controller_rejects_wrong_order_before_creating_run(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])

    with pytest.raises(ValueError, match="start with one planner"):
        run_workflow(
            tmp_path / ".research" / "state.sqlite",
            [_step("writer"), _step("reviewer")],
            lambda role, packet: AgentResult(
                role=role, task_id=packet.task_id, outcome="produced"
            ),
        )

    assert read_status(tmp_path / ".research" / "state.sqlite").runs == 0


def test_interrupted_run_skips_accepted_tasks_and_starts_new_attempt(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])
    database = tmp_path / ".research" / "state.sqlite"
    steps = [_step("planner"), _step("evidence_reader"), _step("writer"), _step("reviewer")]
    first_seen = []

    def crashing_runner(role, packet):
        first_seen.append(role)
        if role == "writer":
            raise KeyboardInterrupt
        return AgentResult(role=role, task_id=packet.task_id, outcome="produced")

    with pytest.raises(KeyboardInterrupt):
        run_workflow(database, steps, crashing_runner)

    run_id = latest_active_run(database)
    assert run_id is not None
    resumed_seen = []

    def resumed_runner(role, packet):
        resumed_seen.append(role)
        return AgentResult(role=role, task_id=packet.task_id, outcome="produced")

    result = run_workflow(
        database, steps, resumed_runner, resume_run_id=run_id
    )

    assert result.status == "completed"
    assert first_seen == ["planner", "evidence_reader", "writer"]
    assert resumed_seen == ["writer", "reviewer"]
    with connect(database) as connection:
        tasks = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        writer_attempts = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id = ?",
            (f"{run_id}-task-3",),
        ).fetchone()[0]
    assert tasks == 4
    assert writer_attempts == 2


def test_model_call_budget_stops_before_next_dispatch(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])
    database = tmp_path / ".research" / "state.sqlite"
    seen = []

    def one_call_runner(role, packet):
        seen.append(role)
        return AgentResult(
            role=role, task_id=packet.task_id, outcome="produced", model_calls=1
        )

    result = run_workflow(
        database,
        [_step("planner"), _step("evidence_reader"), _step("writer"), _step("reviewer")],
        one_call_runner,
        max_model_calls=2,
    )

    assert result.status == "budget_exhausted"
    assert result.model_calls == 2
    assert seen == ["planner", "evidence_reader"]
    status = read_status(database)
    assert status.latest_run_status == "budget_exhausted"
    assert status.task_states == {"accepted": 2}


def test_fake_cli_run_exports_reviewed_report(tmp_path):
    shutil.copytree(FIXTURE / "input", tmp_path / "input")

    result = runner.invoke(app, ["run", str(tmp_path), "--fake"])

    assert result.exit_code == 0, result.output
    assert "Status: completed" in result.output
    report = (tmp_path / "output" / "report.md").read_text(encoding="utf-8")
    assert "Seasonal naive" in report
    assert "paper-b contradicts" in report
    assert "[^claim-1]" in report and "[^claim-2]" in report
    provenance = json.loads((tmp_path / "output" / "provenance.json").read_text())
    assert len(provenance["claims"]) == 2
