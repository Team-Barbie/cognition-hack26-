"""SQLite setup and small read helpers owned by the Spine track."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

DB_SCHEMA_VERSION = 1


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


RunStatus = Literal["pending", "running", "completed", "budget_exhausted", "failed"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def connect(database: Path) -> sqlite3.Connection:
    """Open a configured connection to an existing project database."""

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database: Path, project_root: Path) -> InitResult:
    """Create the Wave 0 database. Calling this twice keeps the same project."""

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database)
    try:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'project'"
        ).fetchone()
        if existing:
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
                    UNIQUE (task_id, number)
                );
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
            "SELECT status FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        state_rows = connection.execute(
            "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state ORDER BY state"
        ).fetchall()
        return StatusSummary(
            project_id=project["id"],
            runs=runs,
            tasks=tasks,
            latest_run_status=latest["status"] if latest else None,
            task_states={row["state"]: row["count"] for row in state_rows},
        )
    finally:
        connection.close()


def create_run(database: Path, max_model_calls: int) -> str:
    """Create and start one run."""

    if max_model_calls < 0:
        raise ValueError("max_model_calls must be non-negative")
    connection = connect(database)
    try:
        project = connection.execute("SELECT id FROM project LIMIT 1").fetchone()
        if project is None:
            raise RuntimeError("project database is missing its project record")
        run_id = f"run-{uuid4()}"
        now = _now()
        with connection:
            connection.execute(
                """INSERT INTO runs
                   (id, project_id, status, max_model_calls, model_calls, created_at, updated_at)
                   VALUES (?, ?, 'running', ?, 0, ?, ?)""",
                (run_id, project["id"], max_model_calls, now, now),
            )
        return run_id
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
    finally:
        connection.close()


def start_attempt(database: Path, task_id: str, attempt_id: str) -> None:
    """Atomically mark a task running and create its first attempt."""

    connection = connect(database)
    try:
        with connection:
            task = connection.execute(
                "SELECT state, attempt_count FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "ready":
                raise RuntimeError(f"task {task_id} is not ready")
            number = task["attempt_count"] + 1
            now = _now()
            connection.execute(
                "UPDATE tasks SET state = 'running', attempt_count = ?, updated_at = ? WHERE id = ?",
                (number, now, task_id),
            )
            connection.execute(
                """INSERT INTO attempts (id, task_id, number, status, started_at)
                   VALUES (?, ?, ?, 'running', ?)""",
                (attempt_id, task_id, number, now),
            )
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
    finally:
        connection.close()


def finish_run(
    database: Path,
    run_id: str,
    status: Literal["completed", "budget_exhausted", "failed"],
    model_calls: int,
) -> None:
    """Persist the terminal outcome and model-call usage of a run."""

    connection = connect(database)
    try:
        with connection:
            updated = connection.execute(
                """UPDATE runs SET status = ?, model_calls = ?, updated_at = ?
                   WHERE id = ? AND status = 'running'""",
                (status, model_calls, _now(), run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} is not running")
    finally:
        connection.close()
