import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ar.cli as cli
from ar.agents.fake import FakeAgent
from ar.controller import RunOutcome, run_project
from ar.db import (
    accept_attempt,
    connect,
    create_run,
    create_task,
    init_project,
    read_status,
    recover_expired_leases,
    recover_interrupted_task,
    request_pause,
    start_attempt,
)
from ar.models import AgentResult, ReviewFinding, ReviewResult


FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"
runner = CliRunner()


def fixture_project(tmp_path):
    shutil.copytree(FIXTURE / "input", tmp_path / "input")
    conn = init_project(tmp_path)
    conn.close()
    return tmp_path


class RevisionAgent:
    def __init__(self):
        self.fake = FakeAgent()
        self.reviews = 0

    def run(self, role, packet):
        if role == "reviewer" and self.reviews == 0:
            self.reviews += 1
            artifact = next(item for item in packet.dependencies if item.kind == "artifact")
            review = ReviewResult(
                reviewed_artifact_id=artifact.id,
                reviewed_revision=artifact.revision,
                findings=[ReviewFinding(severity="blocking", message="Add a revision note.")],
                disposition="revise",
            )
            return AgentResult(
                role="reviewer",
                task_id=packet.task_id,
                outcome="produced",
                payload=review.model_dump(mode="json"),
            )
        if role == "reviewer":
            self.reviews += 1
        return self.fake.run(role, packet)


def test_reviewer_can_force_exactly_one_writer_revision(tmp_path):
    project = fixture_project(tmp_path)

    outcome = run_project(project, RevisionAgent())

    assert outcome is RunOutcome.COMPLETED
    conn = connect(project)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE role='writer'").fetchone()[0] == 2
        revisions = conn.execute(
            "SELECT revision FROM artifacts ORDER BY revision"
        ).fetchall()
        assert [row["revision"] for row in revisions] == [1, 2]
    finally:
        conn.close()
    assert "Revision notes" in (project / "output" / "report.md").read_text()


class PauseAfterPlanner:
    def __init__(self, database):
        self.database = database
        self.fake = FakeAgent()

    def run(self, role, packet):
        result = self.fake.run(role, packet)
        if role == "planner":
            request_pause(self.database)
        return result


def test_pause_then_resume_reuses_accepted_tasks(tmp_path):
    project = fixture_project(tmp_path)
    database = project / ".research" / "state.sqlite"

    assert run_project(project, PauseAfterPlanner(database)) is RunOutcome.CANCELLED
    assert read_status(database).latest_run_status == "paused"
    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED

    conn = connect(project)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        planner = conn.execute("SELECT attempt_count FROM tasks WHERE role='planner'").fetchone()
        assert planner["attempt_count"] == 1
    finally:
        conn.close()


def test_input_change_marks_old_tasks_stale_and_starts_new_run(tmp_path):
    project = fixture_project(tmp_path)
    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED
    paper = project / "input" / "papers" / "paper-c.md"
    paper.write_text(paper.read_text() + "\nAdditional off-topic note.\n")

    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED

    status = read_status(project / ".research" / "state.sqlite")
    assert status.runs == 2
    assert status.stale_tasks == 6


def test_fencing_rejects_old_worker_and_run_creation_is_idempotent(tmp_path):
    project = fixture_project(tmp_path)
    database = project / ".research" / "state.sqlite"
    run_id = create_run(database, 10, idempotency_key="same-request")
    assert create_run(database, 10, idempotency_key="same-request") == run_id
    task_id = f"{run_id}-task-1"
    create_task(database, run_id, task_id, "planner")
    first = start_attempt(database, task_id, "attempt-old")
    recover_interrupted_task(database, task_id)
    second = start_attempt(database, task_id, "attempt-new")
    result = AgentResult(role="planner", task_id=task_id, outcome="produced")

    assert second.fencing_token > first.fencing_token
    with pytest.raises(RuntimeError, match="stale fencing token"):
        accept_attempt(
            database,
            task_id,
            "attempt-new",
            result,
            fencing_token=first.fencing_token,
        )


def test_expired_lease_returns_task_to_ready(tmp_path):
    project = fixture_project(tmp_path)
    database = project / ".research" / "state.sqlite"
    run_id = create_run(database, 10)
    task_id = f"{run_id}-task-1"
    create_task(database, run_id, task_id, "planner")
    start_attempt(database, task_id, "expired-attempt", lease_seconds=-1)

    assert recover_expired_leases(database) == [task_id]
    conn = connect(project)
    try:
        assert conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "ready"
    finally:
        conn.close()


def test_cli_inspect_export_and_live_agent_selection(tmp_path, monkeypatch):
    project = fixture_project(tmp_path)
    seen = {}

    class Client:
        def __init__(self, model, effort):
            seen.update(model=model, effort=effort)

    monkeypatch.setattr(cli, "AnthropicClient", Client)
    monkeypatch.setattr(cli, "LiveAgent", lambda client: FakeAgent())
    run = runner.invoke(
        cli.app,
        ["run", str(project), "--model", "test-model", "--effort", "high"],
    )
    assert run.exit_code == 0, run.output
    assert seen == {"model": "test-model", "effort": "high"}

    claim = runner.invoke(
        cli.app, ["inspect", "claim", "claim-1", "--project", str(project)]
    )
    assert claim.exit_code == 0, claim.output
    assert "read receipt" in claim.output.lower()

    conn = connect(project)
    try:
        task_id = conn.execute(
            "SELECT id FROM tasks WHERE role='evidence_reader' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    finally:
        conn.close()
    task = runner.invoke(
        cli.app, ["inspect", "task", task_id, "--project", str(project)]
    )
    reads = runner.invoke(
        cli.app, ["inspect", "reads", task_id, "--project", str(project)]
    )
    assert task.exit_code == 0 and "State: accepted" in task.output
    assert reads.exit_code == 0 and "lines" in reads.output

    exported = runner.invoke(cli.app, ["export", str(project)])
    assert exported.exit_code == 0, exported.output
    assert "report.md" in exported.output


def test_planner_proposals_are_recorded_once(tmp_path):
    project = fixture_project(tmp_path)
    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED
    conn = connect(project)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE kind='planner_proposals'"
        ).fetchall()
        assert len(row) == 1
        assert '"clamped": true' in row[0]["payload_json"]
    finally:
        conn.close()


class CountingAgent:
    def __init__(self):
        self.fake = FakeAgent()

    def run(self, role, packet):
        return self.fake.run(role, packet).model_copy(update={"model_calls": 1})


def test_budget_stop_keeps_and_exports_writer_draft(tmp_path):
    project = fixture_project(tmp_path)

    outcome = run_project(project, CountingAgent(), max_model_calls=5)

    assert outcome is RunOutcome.BUDGET_EXHAUSTED
    report = (project / "output" / "report.md").read_text()
    summary = (project / "output" / "run-summary.md").read_text()
    assert "Draft" in report
    assert "budget_exhausted" in summary


class CrashOnceAtWriter:
    def __init__(self):
        self.fake = FakeAgent()
        self.crashed = False

    def run(self, role, packet):
        if role == "writer" and not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt
        return self.fake.run(role, packet)


def test_end_to_end_crash_resume_does_not_duplicate_artifact(tmp_path):
    project = fixture_project(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_project(project, CrashOnceAtWriter())

    assert run_project(project, FakeAgent()) is RunOutcome.COMPLETED

    conn = connect(project)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    finally:
        conn.close()


def test_end_to_end_idempotency_key_reuses_completed_run(tmp_path):
    project = fixture_project(tmp_path)
    assert run_project(project, FakeAgent(), idempotency_key="request-1") is RunOutcome.COMPLETED
    assert run_project(project, FakeAgent(), idempotency_key="request-1") is RunOutcome.COMPLETED

    conn = connect(project)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    finally:
        conn.close()
