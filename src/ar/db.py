"""SQLite setup and small read helpers owned by the Spine track."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
        return StatusSummary(project_id=project["id"], runs=runs, tasks=tasks)
    finally:
        connection.close()
