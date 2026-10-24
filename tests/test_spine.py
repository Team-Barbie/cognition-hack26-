import sqlite3

import pytest
from typer.testing import CliRunner

from ar.cli import PROJECT_DIRECTORIES, app
from ar.controller import TaskState, WorkflowStep, require_transition, run_workflow
from ar.db import read_status
from ar.models import AgentResult
from ar.packet import Packet


runner = CliRunner()


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
    def packet(previous):
        return Packet(
            packet_id=f"packet-{role}",
            task_id="assigned-by-controller",
            attempt_id="assigned-by-controller",
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
