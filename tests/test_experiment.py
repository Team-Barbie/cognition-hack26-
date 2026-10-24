import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from ar import experiment
from ar.experiment import ExperimentSpec, accept_result, reconcile, run_experiment
from ar.snapshots import BlobStore, BrokerError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "experiment"
INPUTS = ["fixtures/experiment/data.csv", "fixtures/experiment/baseline_mae.py"]


@pytest.fixture
def project(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "fixtures" / "experiment")
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    return tmp_path, conn


def spec(**kw) -> ExperimentSpec:
    base = dict(
        id="spec-1", question="Seasonal-naive MAE on weekly demand", input_files=INPUTS,
        command=["python", "baseline_mae.py", "--seed", "7"], working_dir="fixtures/experiment",
        seed=7, metrics=["mae"], success_criteria=["mae computed"], expected_outputs=["results.json"],
        timeout_seconds=60,
    )
    return ExperimentSpec(**(base | kw))


def inline(code: str, **kw) -> ExperimentSpec:
    return spec(input_files=[], command=["python", "-c", code], working_dir=".", **kw)


def test_seeded_run_is_recorded_and_captured(project):
    root, conn = project
    run = run_experiment(root, conn, spec())
    assert (run.outcome, run.exit_status, run.executor) == ("succeeded", 0, "runtime")
    assert run.result_acceptable and run.deviations == []
    accept_result(run)
    store = BlobStore(root)
    assert b'"mae": 6.875' in store.get(run.output_hashes["results.json"])
    assert b"seasonal-naive MAE" in store.get(run.stdout_sha256)
    assert store.get(run.stderr_sha256) == b""
    assert run.environment["python"] and run.environment["platform"]
    assert "NOT an OS sandbox" in run.assurance and "working-directory jail" in run.assurance
    assert (root / ".research" / "workspaces" / run.id / "fixtures" / "experiment" / "results.json").is_file()
    assert not (root / "fixtures" / "experiment" / "results.json").exists()  # the real tree is untouched
    assert experiment.get_run(conn, run.id) == run
    assert [r.id for r in experiment.list_runs(conn)] == [run.id]


def test_same_seed_gives_identical_output_hashes(project):
    root, conn = project
    first, second = run_experiment(root, conn, spec()), run_experiment(root, conn, spec())
    assert first.id != second.id
    assert first.output_hashes == second.output_hashes
    other = run_experiment(root, conn, spec(seed=8, command=["python", "baseline_mae.py", "--seed", "8"]))
    assert other.output_hashes != first.output_hashes


@pytest.mark.parametrize("change", [
    {"input_files": ["../outside.txt"]},
    {"input_files": ["C:/Windows/win.ini"]},
    {"input_files": ["/etc/passwd"]},
    {"input_files": [".research/state.sqlite"]},
    {"working_dir": ".."},
    {"working_dir": "/tmp"},
    {"expected_outputs": ["../escape.json"]},
    {"expected_outputs": ["/abs/results.json"]},
    {"command": ["python", "baseline_mae.py", "--out", "../escape.json"]},
])
def test_unsafe_paths_are_rejected_before_anything_runs(project, change):
    root, conn = project
    with pytest.raises(BrokerError) as exc:
        run_experiment(root, conn, spec(**change))
    assert exc.value.code == "ACCESS_DENIED"
    assert experiment.list_runs(conn) == []
    assert not (root / ".research" / "workspaces").exists()


def test_symlink_input_escape_is_rejected(project, tmp_path_factory):
    root, conn = project
    outside = tmp_path_factory.mktemp("outside") / "secret.csv"
    outside.write_text("x")
    try:
        os.symlink(outside, root / "fixtures" / "experiment" / "link.csv")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(BrokerError) as exc:
        run_experiment(root, conn, spec(input_files=["fixtures/experiment/link.csv"]))
    assert exc.value.code == "ACCESS_DENIED"


def test_missing_input_is_source_not_found(project):
    root, conn = project
    with pytest.raises(BrokerError) as exc:
        run_experiment(root, conn, spec(input_files=["fixtures/experiment/nope.csv"]))
    assert exc.value.code == "SOURCE_NOT_FOUND"


def test_exit_zero_with_missing_output_is_not_acceptable(project):
    root, conn = project
    run = run_experiment(root, conn, inline("print('done')"))
    assert run.outcome == "succeeded" and run.exit_status == 0
    assert run.result_acceptable is False
    assert "missing expected output: results.json" in run.deviations
    assert run.output_hashes == {}
    with pytest.raises(BrokerError) as exc:
        accept_result(run)
    assert exc.value.code == "RESULT_NOT_ACCEPTABLE"
    assert experiment.get_run(conn, run.id).result_acceptable is False


def test_nonzero_exit_is_failed(project):
    root, conn = project
    run = run_experiment(root, conn, inline("import sys; sys.exit(3)", expected_outputs=[]))
    assert (run.outcome, run.exit_status, run.result_acceptable) == ("failed", 3, False)
    with pytest.raises(BrokerError):
        accept_result(run)


def test_timeout_is_enforced(project):
    root, conn = project
    run = run_experiment(root, conn, inline("import time; time.sleep(30)", expected_outputs=[], timeout_seconds=1))
    assert run.outcome == "timed_out" and run.exit_status is None and not run.result_acceptable


def test_unstartable_command_is_failed(project):
    root, conn = project
    run = run_experiment(root, conn, spec(input_files=[], command=["no-such-binary-xyz"], working_dir=".",
                                          expected_outputs=[]))
    assert run.outcome == "failed" and "could not start" in run.deviations[0]


def test_environment_is_minimal_and_cwd_is_the_scratch_dir(project, monkeypatch):
    root, conn = project
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    code = "import os; print(sorted(os.environ)); print(os.getcwd()); print(os.environ.get('AR_SEED'))"
    run = run_experiment(root, conn, inline(code, expected_outputs=[]))
    out = BlobStore(root).get(run.stdout_sha256).decode()
    assert "SECRET_TOKEN" not in out and "hunter2" not in out
    scratch = (root / ".research" / "workspaces" / run.id).resolve()
    assert str(scratch) in out
    assert out.strip().splitlines()[-1] == "7"
    assert "PYTHONHASHSEED" in out


def test_started_run_without_finish_is_unknown_and_not_rerun(project):
    root, conn = project
    experiment.ensure_schema(conn)
    crashed = experiment.ExperimentRun(
        id="exp-001", spec_id="spec-1", started_at="2026-10-24T00:00:00+00:00", command=["python"], seed=7
    )
    experiment._save(conn, crashed, "started", spec=spec())
    run = reconcile(conn, "exp-001")
    assert run.outcome == "execution_unknown" and not run.result_acceptable
    assert "no finished record" in run.deviations[0]
    assert experiment.get_run(conn, "exp-001").outcome == "execution_unknown"
    assert reconcile(conn, "exp-001").deviations == run.deviations  # idempotent
    with pytest.raises(BrokerError) as exc:
        accept_result(run)
    assert exc.value.code == "RESULT_NOT_ACCEPTABLE"

    with pytest.raises(BrokerError) as exc:
        run_experiment(root, conn, spec())
    assert exc.value.code == "EXECUTION_UNKNOWN" and exc.value.details["run_ids"] == ["exp-001"]
    assert len(experiment.list_runs(conn)) == 1  # nothing was launched

    retried = run_experiment(root, conn, spec(), retry_unknown=True)
    assert retried.outcome == "succeeded" and retried.id == "exp-002"


def test_reconcile_leaves_a_finished_run_alone(project):
    root, conn = project
    run = run_experiment(root, conn, spec())
    assert reconcile(conn, run.id) == run
    with pytest.raises(BrokerError):
        reconcile(conn, "exp-999")
