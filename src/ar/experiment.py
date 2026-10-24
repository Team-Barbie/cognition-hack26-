"""Seeded experiments run as a jailed subprocess and recorded as an `ExperimentRun`."""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import Field

from ar.clock import utc_now as _now
from ar.models import Record
from ar.snapshots import BlobStore, BrokerError, resolve_inside

ASSURANCE = (
    "Runtime-captured subprocess with a working-directory jail: declared inputs are copied into a fresh "
    "scratch directory, the command runs there with a minimal environment and a timeout. This is NOT an "
    "OS sandbox: the process can still reach anything the user account can, including the network."
)
PYTHON_NAMES = {"python", "python3", "python.exe"}
RunOutcome = Literal["succeeded", "failed", "timed_out", "cancelled", "execution_unknown"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    spec_json TEXT NOT NULL,
    run_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiment_runs_spec ON experiment_runs(spec_id);
"""


class ExperimentSpec(Record):
    id: str
    question: str
    status: Literal["pilot", "confirmatory"] = "pilot"
    input_files: list[str] = Field(default_factory=list)
    command: list[str] = Field(min_length=1)
    working_dir: str = "."
    seed: int
    metrics: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=60, gt=0)


class ExperimentRun(Record):
    id: str
    spec_id: str
    started_at: str
    ended_at: str | None = None
    executor: Literal["runtime"] = "runtime"
    environment: dict[str, str] = Field(default_factory=dict)
    command: list[str]
    seed: int
    exit_status: int | None = None
    outcome: RunOutcome = "execution_unknown"
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    output_hashes: dict[str, str] = Field(default_factory=dict)
    deviations: list[str] = Field(default_factory=list)
    result_acceptable: bool = False
    provenance: Literal["runtime_captured"] = "runtime_captured"
    assurance: str = ASSURANCE


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def _escapes(rel: str) -> str | None:
    """Why a path is unsafe on any OS, or None. Windows forms count everywhere: a drive path or a
    backslash `..\\` is an escape on Windows even though POSIX reads it as an ordinary file name."""
    posix, windows = Path(rel), PureWindowsPath(rel)
    if posix.is_absolute() or posix.drive or posix.root or windows.is_absolute() or windows.drive or windows.root:
        return "must be a relative path"
    if ".." in posix.parts or ".." in windows.parts:
        return "traversal"
    return None


def _relative(rel: str, what: str) -> Path:
    problem = _escapes(rel)
    if problem == "traversal":
        raise BrokerError("ACCESS_DENIED", f"path traversal in {what} {rel!r}")
    if problem:
        raise BrokerError("ACCESS_DENIED", f"{what} {rel!r} {problem}")
    return Path(rel)


def _validate(root: Path, spec: ExperimentSpec) -> list[str]:
    """Reject unsafe paths before anything is created. Returns the checked input files."""
    inputs = []
    for rel in spec.input_files:
        _relative(rel, "input file")
        source = resolve_inside(rel, root)
        if source.relative_to(root.resolve()).parts[:1] == (".research",):
            raise BrokerError("ACCESS_DENIED", f"input file {rel!r} is runtime-owned")
        if not source.is_file():
            raise BrokerError("SOURCE_NOT_FOUND", f"input file {rel!r} does not exist")
        inputs.append(rel)
    _relative(spec.working_dir, "working_dir")
    for rel in spec.expected_outputs:
        _relative(rel, "expected output")
    for arg in spec.command[1:]:
        if _escapes(arg):
            raise BrokerError("ACCESS_DENIED", f"command argument {arg!r} leaves the working directory")
    return inputs


def _environment(seed: int) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    env.update(PYTHONHASHSEED=str(seed % 2**32), AR_SEED=str(seed))
    return env


def _save(conn: sqlite3.Connection, run: ExperimentRun, state: str, *, spec: ExperimentSpec | None = None) -> None:
    with conn:
        if spec is not None:
            conn.execute(
                "INSERT INTO experiment_runs VALUES (?,?,?,?,?,?,?)",
                (run.id, run.spec_id, state, run.started_at, run.ended_at, spec.model_dump_json(),
                 run.model_dump_json()),
            )
        else:
            conn.execute(
                "UPDATE experiment_runs SET state=?, ended_at=?, run_json=? WHERE id=?",
                (state, run.ended_at, run.model_dump_json(), run.id),
            )


def get_run(conn: sqlite3.Connection, run_id: str) -> ExperimentRun | None:
    try:
        row = conn.execute("SELECT run_json FROM experiment_runs WHERE id=?", (run_id,)).fetchone()
    except sqlite3.OperationalError:  # table not created yet
        return None
    return ExperimentRun.model_validate_json(row[0]) if row else None


def list_runs(conn: sqlite3.Connection) -> list[ExperimentRun]:
    try:
        rows = conn.execute("SELECT run_json FROM experiment_runs ORDER BY rowid").fetchall()
    except sqlite3.OperationalError:  # table not created yet
        return []
    return [ExperimentRun.model_validate_json(r[0]) for r in rows]


def reconcile(conn: sqlite3.Connection, run_id: str) -> ExperimentRun:
    """A run found started with no finished record becomes `execution_unknown`. It is never rerun here."""
    row = conn.execute("SELECT state FROM experiment_runs WHERE id=?", (run_id,)).fetchone()
    run = get_run(conn, run_id)
    if row is None or run is None:
        raise BrokerError("SOURCE_NOT_FOUND", f"no experiment run {run_id!r}")
    if row[0] == "started":
        run.outcome = "execution_unknown"
        run.result_acceptable = False
        run.deviations.append("no finished record found; the process may or may not have run to completion")
        _save(conn, run, "unknown")
    return run


def accept_result(run: ExperimentRun) -> None:
    """Process success is not research success: reject a run whose result cannot be accepted."""
    if run.outcome != "succeeded" or not run.result_acceptable:
        raise BrokerError(
            "RESULT_NOT_ACCEPTABLE",
            f"experiment run {run.id} ({run.outcome}) cannot back a result claim",
            {"run_id": run.id, "outcome": run.outcome, "deviations": run.deviations},
        )


def _run_process(
    command: list[str], cwd: Path, env: dict[str, str], timeout: float
) -> tuple[RunOutcome, int | None, bytes, bytes, list[str]]:
    try:
        done = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as exc:
        return "timed_out", None, exc.stdout or b"", exc.stderr or b"", [f"killed after {timeout}s"]
    except OSError as exc:
        return "failed", None, b"", b"", [f"could not start the command: {exc}"]
    outcome: RunOutcome = "succeeded" if done.returncode == 0 else "failed"
    return outcome, done.returncode, done.stdout, done.stderr, []


def run_experiment(
    project_root: Path, conn: sqlite3.Connection, spec: ExperimentSpec, *, retry_unknown: bool = False
) -> ExperimentRun:
    root = Path(project_root).resolve()
    ensure_schema(conn)
    inputs = _validate(root, spec)
    pending = conn.execute(
        "SELECT id FROM experiment_runs WHERE spec_id=? AND state IN ('started', 'unknown')", (spec.id,)
    ).fetchall()
    if pending and not retry_unknown:
        raise BrokerError(
            "EXECUTION_UNKNOWN",
            f"spec {spec.id} has a run with no known outcome; retry needs a deliberate decision",
            {"run_ids": [r[0] for r in pending]},
        )

    count = conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0]
    command = [sys.executable if spec.command[0] in PYTHON_NAMES else spec.command[0], *spec.command[1:]]
    run = ExperimentRun(
        id=f"exp-{count + 1:03d}", spec_id=spec.id, started_at=_now(), command=command, seed=spec.seed,
        environment={"python": platform.python_version(), "platform": platform.platform()},
    )
    scratch = root / ".research" / "workspaces" / run.id
    scratch.mkdir(parents=True, exist_ok=False)
    for rel in inputs:
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
    workdir = resolve_inside(spec.working_dir, scratch)
    workdir.mkdir(parents=True, exist_ok=True)

    _save(conn, run, "started", spec=spec)
    outcome, status, out, err, deviations = _run_process(command, workdir, _environment(spec.seed), spec.timeout_seconds)

    blobs = BlobStore(root)
    run.outcome, run.exit_status, run.deviations = outcome, status, deviations
    run.stdout_sha256, run.stderr_sha256 = blobs.put(out), blobs.put(err)
    for rel in spec.expected_outputs:
        try:
            produced = resolve_inside(rel, workdir)
        except BrokerError:
            run.deviations.append(f"expected output escapes the working directory: {rel}")
            continue
        if produced.is_file():
            run.output_hashes[rel] = blobs.put(produced.read_bytes())
        else:
            run.deviations.append(f"missing expected output: {rel}")
    run.result_acceptable = outcome == "succeeded" and not any(
        d.startswith(("missing expected output", "expected output escapes")) for d in run.deviations
    )
    run.ended_at = _now()
    _save(conn, run, "finished")
    return run

