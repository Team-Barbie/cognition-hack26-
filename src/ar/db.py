"""SQLite setup and small read helpers owned by the Spine track."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ar.clock import utc_now as _now
from ar.models import AgentResult, Requirement

DB_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class InitResult:
    database: Path
    project_id: str
    created: bool


@dataclass(frozen=True)
class StatusSummary:
    project_id: str
    runs: int
    tasks: int
    latest_run_status: str | None
    task_states: dict[str, int]
    paused: bool = False
    stale_tasks: int = 0


@dataclass(frozen=True)
class Lease:
    token: str
    fencing_token: int
    expires_at: str


RunStatus = Literal["pending", "running", "completed", "budget_exhausted", "failed"]


def connect(project_root: Path) -> sqlite3.Connection:
    """Open a configured connection to an existing project database."""

    database = (
        project_root
        if project_root.name == "state.sqlite"
        else project_root / ".research" / "state.sqlite"
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Upgrade older Spine databases without deleting recorded work."""

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        with connection:
            if "result_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT")
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
    if version < 3:
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT, task_id TEXT, kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requirements (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    source_passage TEXT NOT NULL, interpretation TEXT NOT NULL,
                    acceptance_checks_json TEXT NOT NULL, deliverable TEXT NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
    if version < 4:
        additions = {
            "runs": {
                "max_tasks": "INTEGER NOT NULL DEFAULT 12",
                "pause_requested": "INTEGER NOT NULL DEFAULT 0",
                "outcome": "TEXT",
                "idempotency_key": "TEXT",
            },
            "tasks": {
                "fencing_token": "INTEGER NOT NULL DEFAULT 0",
                "is_stale": "INTEGER NOT NULL DEFAULT 0",
                "input_fingerprint": "TEXT",
            },
            "attempts": {
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "fencing_token": "INTEGER",
            },
        }
        with connection:
            for table, columns in additions.items():
                present = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, definition in columns.items():
                    if name not in present:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                        )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS input_versions (
                    source_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    scope TEXT NOT NULL, key TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency
                    ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
                """
            )
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")


def initialize(database: Path, project_root: Path) -> InitResult:
    """Create the Wave 0 database. Calling this twice keeps the same project."""

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database)
    try:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'project'"
        ).fetchone()
        if existing:
            _migrate(connection)
            row = connection.execute("SELECT id FROM project LIMIT 1").fetchone()
            if row is None:
                raise RuntimeError("project database is missing its project record")
            return InitResult(database=database, project_id=row["id"], created=False)

        project_id = f"project-{uuid4()}"
        with connection:
            connection.executescript(
                """
                CREATE TABLE project (
                    id TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES project(id),
                    status TEXT NOT NULL CHECK (status IN (
                        'pending', 'running', 'completed', 'budget_exhausted', 'failed'
                    )),
                    max_model_calls INTEGER NOT NULL CHECK (max_model_calls >= 0),
                    model_calls INTEGER NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
                    max_tasks INTEGER NOT NULL DEFAULT 12,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    role TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'pending', 'ready', 'running', 'accepted',
                        'needs_revision', 'failed'
                    )),
                    contract_revision INTEGER NOT NULL DEFAULT 1,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    is_stale INTEGER NOT NULL DEFAULT 0,
                    input_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE attempts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    number INTEGER NOT NULL CHECK (number >= 1),
                    status TEXT NOT NULL CHECK (status IN (
                        'running', 'accepted', 'needs_revision', 'failed'
                    )),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    fencing_token INTEGER,
                    UNIQUE (task_id, number)
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT, task_id TEXT, kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE requirements (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    source_passage TEXT NOT NULL, interpretation TEXT NOT NULL,
                    acceptance_checks_json TEXT NOT NULL, deliverable TEXT NOT NULL
                );
                CREATE TABLE input_versions (
                    source_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE idempotency_records (
                    scope TEXT NOT NULL, key TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                );
                CREATE UNIQUE INDEX runs_idempotency
                    ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
                """
            )
            connection.execute(
                "INSERT INTO project (id, root, created_at) VALUES (?, ?, ?)",
                (project_id, str(project_root.resolve()), _now()),
            )
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        return InitResult(database=database, project_id=project_id, created=True)
    finally:
        connection.close()


def init_project(project_root: Path, source_dir: Path | None = None) -> sqlite3.Connection:
    """Initialize all track schemas and register markdown inputs."""

    root = project_root.resolve()
    if source_dir is not None:
        source_input = source_dir.resolve() / "input"
        if not source_input.is_dir():
            raise FileNotFoundError(source_input)
        shutil.copytree(source_input, root / "input", dirs_exist_ok=True)
    for relative in (
        "input/papers", "input/datasets", "input/notes", "policies", "skills",
        ".research/blobs", ".research/manifests", ".research/workspaces", "output",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    initialize(root / ".research" / "state.sqlite", root)
    connection = connect(root)
    from ar.broker import Broker, ensure_schema as ensure_broker_schema
    from ar.evidence import ensure_schema as ensure_evidence_schema
    from ar.review import ensure_schema as ensure_review_schema

    ensure_broker_schema(connection)
    ensure_evidence_schema(connection)
    ensure_review_schema(connection)
    broker = Broker(connection, root)
    brief = root / "input" / "brief.md"
    if brief.is_file():
        broker.register_source("brief", brief)
        requirements = []
        for number, line in enumerate(brief.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            text = stripped[2:].strip() if stripped.startswith("- ") else stripped
            if ":" not in text and not stripped.startswith("- "):
                continue
            requirements.append((f"req-{number}", 1, stripped, text, "[]", "report.md"))
        with connection:
            connection.executemany(
                """INSERT OR REPLACE INTO requirements
                   (id, revision, source_passage, interpretation, acceptance_checks_json, deliverable)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                requirements,
            )
    for paper in sorted((root / "input" / "papers").glob("*.md")):
        broker.register_source(paper.stem, paper)
    return connection


def read_status(database: Path) -> StatusSummary:
    """Return a compact project status without changing state."""

    if not database.is_file():
        raise FileNotFoundError(database)
    connection = connect(database)
    try:
        project = connection.execute("SELECT id FROM project LIMIT 1").fetchone()
        if project is None:
            raise RuntimeError("project database is missing its project record")
        runs = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
        tasks = connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
        latest = connection.execute(
            "SELECT status, outcome, pause_requested FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        state_rows = connection.execute(
            "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state ORDER BY state"
        ).fetchall()
        return StatusSummary(
            project_id=project["id"],
            runs=runs,
            tasks=tasks,
            latest_run_status=(
                "paused"
                if latest and latest["pause_requested"]
                else (latest["outcome"] or latest["status"] if latest else None)
            ),
            task_states={row["state"]: row["count"] for row in state_rows},
            paused=bool(latest["pause_requested"]) if latest else False,
            stale_tasks=connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE is_stale=1"
            ).fetchone()["count"],
        )
    finally:
        connection.close()


def create_run(
    database: Path,
    max_model_calls: int,
    max_tasks: int = 12,
    idempotency_key: str | None = None,
) -> str:
    """Create and start one run."""

    if max_model_calls < 0:
        raise ValueError("max_model_calls must be non-negative")
    connection = connect(database)
    try:
        if idempotency_key:
            existing = connection.execute(
                "SELECT id FROM runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                return existing["id"]
        project = connection.execute("SELECT id FROM project LIMIT 1").fetchone()
        if project is None:
            raise RuntimeError("project database is missing its project record")
        run_id = f"run-{uuid4()}"
        now = _now()
        with connection:
            connection.execute(
                """INSERT INTO runs
                   (id, project_id, status, max_model_calls, model_calls, max_tasks,
                    idempotency_key, created_at, updated_at)
                   VALUES (?, ?, 'running', ?, 0, ?, ?, ?, ?)""",
                (run_id, project["id"], max_model_calls, max_tasks, idempotency_key, now, now),
            )
            _append_event(connection, "run.started", {
                "max_model_calls": max_model_calls, "max_tasks": max_tasks,
            }, run_id=run_id, created_at=now)
        return run_id
    finally:
        connection.close()


def read_run(database: Path, run_id: str) -> sqlite3.Row:
    """Read a run row for resume and budget decisions."""

    connection = connect(database)
    try:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row
    finally:
        connection.close()


def latest_active_run(database: Path) -> str | None:
    """Return the newest interrupted run, if one exists."""

    connection = connect(database)
    try:
        row = connection.execute(
            "SELECT id FROM runs WHERE status = 'running' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


def create_task(database: Path, run_id: str, task_id: str, role: str) -> None:
    """Persist a ready task before it is dispatched."""

    now = _now()
    connection = connect(database)
    try:
        with connection:
            connection.execute(
                """INSERT INTO tasks
                   (id, run_id, role, state, contract_revision, attempt_count, created_at, updated_at)
                   VALUES (?, ?, ?, 'ready', 1, 0, ?, ?)""",
                (task_id, run_id, role, now, now),
            )
            _append_event(connection, "task.created", {"role": role, "state": "ready"},
                          run_id=run_id, task_id=task_id, created_at=now)
    finally:
        connection.close()


def read_task(database: Path, task_id: str) -> sqlite3.Row | None:
    """Read a task row, returning None when it has not been created yet."""

    connection = connect(database)
    try:
        return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        connection.close()


def recover_interrupted_task(database: Path, task_id: str) -> None:
    """Close a lost running attempt and make the task ready for a new attempt."""

    connection = connect(database)
    try:
        with connection:
            task = connection.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "running":
                raise RuntimeError(f"task {task_id} is not interrupted")
            now = _now()
            connection.execute(
                """UPDATE attempts SET status = 'failed', finished_at = ?
                   WHERE task_id = ? AND status = 'running'""",
                (now, task_id),
            )
            connection.execute(
                "UPDATE tasks SET state = 'ready', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
    finally:
        connection.close()


def recover_expired_leases(database: Path, at: datetime | None = None) -> list[str]:
    """Return expired running tasks to ready while preserving their audit trail."""

    cutoff = (at or datetime.now(UTC)).isoformat()
    connection = connect(database)
    try:
        rows = connection.execute(
            """SELECT DISTINCT task_id FROM attempts
               WHERE status='running' AND lease_expires_at IS NOT NULL
               AND lease_expires_at<=?""",
            (cutoff,),
        ).fetchall()
        task_ids = [row["task_id"] for row in rows]
        with connection:
            for task_id in task_ids:
                connection.execute(
                    """UPDATE attempts SET status='failed', finished_at=?
                       WHERE task_id=? AND status='running'""",
                    (cutoff, task_id),
                )
                connection.execute(
                    """UPDATE tasks SET state='ready', updated_at=?
                       WHERE id=? AND state='running'""",
                    (cutoff, task_id),
                )
        return task_ids
    finally:
        connection.close()


def start_attempt(
    database: Path,
    task_id: str,
    attempt_id: str,
    lease_seconds: int = 300,
) -> Lease:
    """Atomically mark a task running and create its first attempt."""

    connection = connect(database)
    try:
        with connection:
            task = connection.execute(
                "SELECT state, attempt_count, fencing_token FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "ready":
                raise RuntimeError(f"task {task_id} is not ready")
            number = task["attempt_count"] + 1
            now = _now()
            fencing_token = task["fencing_token"] + 1
            lease_token = f"lease-{uuid4()}"
            expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """UPDATE tasks SET state='running', attempt_count=?, fencing_token=?, updated_at=?
                   WHERE id=?""",
                (number, fencing_token, now, task_id),
            )
            connection.execute(
                """INSERT INTO attempts
                   (id, task_id, number, status, started_at, lease_token,
                    lease_expires_at, fencing_token)
                   VALUES (?, ?, ?, 'running', ?, ?, ?, ?)""",
                (attempt_id, task_id, number, now, lease_token, expires_at, fencing_token),
            )
            run_id = connection.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()["run_id"]
            _append_event(connection, "attempt.started", {
                "attempt_id": attempt_id, "number": number, "fencing_token": fencing_token,
            }, run_id=run_id, task_id=task_id, created_at=now)
            return Lease(lease_token, fencing_token, expires_at)
    finally:
        connection.close()


def finish_attempt(
    database: Path,
    task_id: str,
    attempt_id: str,
    state: Literal["accepted", "needs_revision", "failed"],
) -> None:
    """Finish an attempt and move its task to the matching state."""

    connection = connect(database)
    try:
        with connection:
            now = _now()
            updated = connection.execute(
                """UPDATE attempts SET status = ?, finished_at = ?
                   WHERE id = ? AND task_id = ? AND status = 'running'""",
                (state, now, attempt_id, task_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"attempt {attempt_id} is not running")
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (state, now, task_id),
            )
            run_id = connection.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()["run_id"]
            _append_event(connection, "attempt.finished", {
                "attempt_id": attempt_id, "state": state,
            }, run_id=run_id, task_id=task_id, created_at=now)
    finally:
        connection.close()


def accept_attempt(
    database: Path,
    task_id: str,
    attempt_id: str,
    result: AgentResult,
    fencing_token: int | None = None,
) -> None:
    """Atomically accept an attempt and save its typed result for resume."""

    connection = connect(database)
    try:
        with connection:
            now = _now()
            if fencing_token is not None:
                current = connection.execute(
                    "SELECT fencing_token FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if current is None or current["fencing_token"] != fencing_token:
                    raise RuntimeError(f"stale fencing token for task {task_id}")
            updated = connection.execute(
                """UPDATE attempts SET status = 'accepted', finished_at = ?
                   WHERE id = ? AND task_id = ? AND status = 'running'""",
                (now, attempt_id, task_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"attempt {attempt_id} is not running")
            connection.execute(
                """UPDATE tasks SET state = 'accepted', result_json = ?, updated_at = ?
                   WHERE id = ?""",
                (result.model_dump_json(), now, task_id),
            )
            run_id = connection.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()["run_id"]
            _append_event(connection, "attempt.accepted", {
                "attempt_id": attempt_id, "role": result.role, "outcome": result.outcome,
                "model_calls": result.model_calls, "result": result.model_dump(mode="json"),
            }, run_id=run_id, task_id=task_id, created_at=now)
    finally:
        connection.close()


def accepted_result(task: sqlite3.Row) -> AgentResult:
    """Restore the exact typed result saved for an accepted task."""

    raw = task["result_json"]
    if not raw:
        raise RuntimeError(f"accepted task {task['id']} has no saved result")
    return AgentResult.model_validate(json.loads(raw))


def finish_run(
    database: Path,
    run_id: str,
    status: Literal["completed", "budget_exhausted", "failed"],
    model_calls: int,
    outcome: str | None = None,
) -> None:
    """Persist the terminal outcome and model-call usage of a run."""

    connection = connect(database)
    try:
        with connection:
            updated = connection.execute(
                """UPDATE runs SET status=?, outcome=?, model_calls=?, pause_requested=0, updated_at=?
                   WHERE id = ? AND status = 'running'""",
                (status, outcome or status, model_calls, _now(), run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} is not running")
            _append_event(connection, "run.finished", {
                "status": status, "outcome": outcome or status, "model_calls": model_calls,
            }, run_id=run_id)
    finally:
        connection.close()


def record_model_calls(database: Path, run_id: str, model_calls: int) -> None:
    """Checkpoint budget use so an interrupted run resumes with the right count."""

    connection = connect(database)
    try:
        with connection:
            updated = connection.execute(
                """UPDATE runs SET model_calls = ?, updated_at = ?
                   WHERE id = ? AND status = 'running'""",
                (model_calls, _now(), run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} is not running")
    finally:
        connection.close()


def _append_event(
    connection: sqlite3.Connection,
    kind: str,
    payload: dict,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    created_at: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO events (run_id, task_id, kind, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, task_id, kind, json.dumps(payload, sort_keys=True), created_at or _now()),
    )


def record_event(
    database: Path,
    kind: str,
    payload: dict,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> None:
    connection = connect(database)
    try:
        with connection:
            _append_event(connection, kind, payload, run_id=run_id, task_id=task_id)
    finally:
        connection.close()


def request_pause(database: Path) -> str:
    connection = connect(database)
    try:
        with connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE status='running' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("there is no active run to pause")
            connection.execute(
                "UPDATE runs SET pause_requested=1, updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            return row["id"]
    finally:
        connection.close()


def clear_pause(database: Path, run_id: str) -> None:
    connection = connect(database)
    try:
        with connection:
            connection.execute(
                "UPDATE runs SET pause_requested=0, updated_at=? WHERE id=? AND status='running'",
                (_now(), run_id),
            )
    finally:
        connection.close()


def pause_requested(database: Path, run_id: str) -> bool:
    return bool(read_run(database, run_id)["pause_requested"])


def save_idempotent(database: Path, scope: str, key: str, response: dict) -> None:
    connection = connect(database)
    try:
        with connection:
            connection.execute(
                """INSERT OR IGNORE INTO idempotency_records
                   (scope, key, response_json, created_at) VALUES (?, ?, ?, ?)""",
                (scope, key, json.dumps(response, sort_keys=True), _now()),
            )
    finally:
        connection.close()


def load_idempotent(database: Path, scope: str, key: str) -> dict | None:
    connection = connect(database)
    try:
        row = connection.execute(
            "SELECT response_json FROM idempotency_records WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None
    finally:
        connection.close()


def update_input_versions(database: Path, versions: dict[str, str]) -> bool:
    """Record current inputs and mark historical tasks stale when any input changed."""

    connection = connect(database)
    try:
        changed = False
        with connection:
            for source_id, fingerprint in versions.items():
                row = connection.execute(
                    "SELECT fingerprint FROM input_versions WHERE source_id=?", (source_id,)
                ).fetchone()
                if row and row["fingerprint"] != fingerprint:
                    changed = True
                connection.execute(
                    """INSERT INTO input_versions (source_id, fingerprint, updated_at)
                       VALUES (?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
                       fingerprint=excluded.fingerprint, updated_at=excluded.updated_at""",
                    (source_id, fingerprint, _now()),
                )
            known = {
                row["source_id"]
                for row in connection.execute("SELECT source_id FROM input_versions")
            }
            if known - set(versions):
                changed = True
                connection.executemany(
                    "DELETE FROM input_versions WHERE source_id=?",
                    [(source_id,) for source_id in known - set(versions)],
                )
            if changed:
                connection.execute("UPDATE tasks SET is_stale=1 WHERE state='accepted'")
                connection.execute(
                    """UPDATE runs SET status='failed', outcome='stale', updated_at=?
                       WHERE status='running'""",
                    (_now(),),
                )
        return changed
    finally:
        connection.close()


def list_requirements(database: Path) -> list[Requirement]:
    connection = connect(database)
    try:
        rows = connection.execute("SELECT * FROM requirements ORDER BY id").fetchall()
        return [
            Requirement(
                id=row["id"],
                revision=row["revision"],
                source_passage=row["source_passage"],
                interpretation=row["interpretation"],
                acceptance_checks=json.loads(row["acceptance_checks_json"]),
                deliverable=row["deliverable"],
            )
            for row in rows
        ]
    finally:
        connection.close()


def refresh_requirements(database: Path, brief: Path) -> list[Requirement]:
    """Compile the current brief into durable requirement rows."""

    rows = []
    for number, line in enumerate(brief.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        text = stripped[2:].strip() if stripped.startswith("- ") else stripped
        if ":" not in text and not stripped.startswith("- "):
            continue
        rows.append((f"req-{number}", 1, stripped, text, "[]", "report.md"))
    connection = connect(database)
    try:
        with connection:
            connection.execute("DELETE FROM requirements")
            connection.executemany(
                """INSERT INTO requirements
                   (id, revision, source_passage, interpretation,
                    acceptance_checks_json, deliverable) VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
    finally:
        connection.close()
    return list_requirements(database)


def latest_run(database: Path) -> sqlite3.Row | None:
    connection = connect(database)
    try:
        return connection.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()


def inspect_task(database: Path, task_id: str) -> str:
    connection = connect(database)
    try:
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return f"No such task: {task_id}"
        attempts = connection.execute(
            """SELECT id, number, status, started_at, finished_at, fencing_token
               FROM attempts WHERE task_id=? ORDER BY number""",
            (task_id,),
        ).fetchall()
        lines = [
            f"Task: {task['id']}",
            f"Run: {task['run_id']}",
            f"Role: {task['role']}",
            f"State: {task['state']}",
            f"Stale: {'yes' if task['is_stale'] else 'no'}",
        ]
        lines += [
            f"Attempt {row['number']}: {row['id']} — {row['status']} (fence {row['fencing_token']})"
            for row in attempts
        ]
        return "\n".join(lines)
    finally:
        connection.close()
