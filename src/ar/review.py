"""Review gate: findings are stored, blocking findings cannot be waived by an agent.

Deviation from INTERFACES.md: `gate` returns a `GateResult` (decision plus limitation strings and
the feedback to hand the writer) instead of a bare `GateDecision`, and takes `revision_pass`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from ar.models import ArtifactVersion, ReviewResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    claim_id TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS review_findings_artifact ON review_findings(artifact_id, revision);
"""

NO_FINDING = "Reviewer requested revision without naming a blocking finding."


class GateDecision(StrEnum):
    accept = "accept"
    needs_revision = "needs_revision"
    accept_with_limitations = "accept_with_limitations"


@dataclass
class GateResult:
    decision: GateDecision
    limitations: list[str] = field(default_factory=list)  # unresolved blocking findings, as text
    feedback: list[str] = field(default_factory=list)  # blocking messages to send the writer


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def gate(
    conn: sqlite3.Connection, review: ReviewResult, *, artifact: ArtifactVersion, revision_pass: int = 0
) -> GateResult:
    """Decide on one review of one exact artifact revision. A stale review raises ValueError."""
    if (review.reviewed_artifact_id, review.reviewed_revision) != (artifact.artifact_id, artifact.revision):
        raise ValueError(
            f"stale review: reviewed {review.reviewed_artifact_id} r{review.reviewed_revision}, "
            f"artifact is {artifact.artifact_id} r{artifact.revision}"
        )
    blocking = [f.message for f in review.findings if f.severity == "blocking"]
    if review.disposition == "revise" and not blocking:
        blocking = [NO_FINDING]
    if not blocking:
        decision = GateDecision.accept
    elif revision_pass >= 1:
        decision = GateDecision.accept_with_limitations
    else:
        decision = GateDecision.needs_revision

    with conn:
        conn.executemany(
            "INSERT INTO review_findings (artifact_id, revision, severity, message, claim_id) VALUES (?,?,?,?,?)",
            [(artifact.artifact_id, artifact.revision, f.severity, f.message, f.claim_id)
             for f in review.findings],
        )
        if decision is GateDecision.accept:
            conn.execute(
                "UPDATE review_findings SET resolved=1 WHERE artifact_id=? AND revision<?",
                (artifact.artifact_id, artifact.revision),
            )
    if decision is GateDecision.accept:
        return GateResult(decision)
    if decision is GateDecision.accept_with_limitations:
        return GateResult(decision, limitations=blocking)
    return GateResult(decision, feedback=blocking)


def list_findings(conn: sqlite3.Connection, artifact_id: str, revision: int | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM review_findings WHERE artifact_id=?", [artifact_id]
    if revision is not None:
        sql, args = sql + " AND revision=?", [*args, revision]
    return conn.execute(sql + " ORDER BY id", args).fetchall()
