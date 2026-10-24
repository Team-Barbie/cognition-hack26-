import sqlite3

import pytest
from typer.testing import CliRunner

from ar.cli import PROJECT_DIRECTORIES, app
from ar.controller import TaskState, require_transition


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
