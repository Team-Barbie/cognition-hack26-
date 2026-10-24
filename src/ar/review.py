"""Review gate: findings are stored, blocking findings cannot be waived by an agent.

Deviation from INTERFACES.md: `gate` returns a `GateResult` (decision plus limitation strings and
the feedback to hand the writer) instead of a bare `GateDecision`, and takes `revision_pass`.

The reviewer is a model, so its verdict is not the last word. With `content`, the gate adds its own
deterministic findings (empty draft, citations that do not resolve, contradicting claims left
uncited) that block even when the reviewer says "accept". Every review is recorded in `reviews`,
which completion uses to require a review of the exact deliverable revision.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ar.evidence import CITATION, contradicting_claims, get_claim
from ar.models import ArtifactVersion, ReviewFinding, ReviewResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    claim_id TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'reviewer'
);
CREATE INDEX IF NOT EXISTS review_findings_artifact ON review_findings(artifact_id, revision);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    reviewer_task TEXT,
    disposition TEXT NOT NULL,
    decision TEXT NOT NULL,
    revision_pass INTEGER NOT NULL,
    checks TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
    runtime_findings: list[ReviewFinding] = field(default_factory=list)  # added by the gate, not the reviewer


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_findings)")}
        if "origin" not in cols:  # databases created before runtime findings
            conn.execute("ALTER TABLE review_findings ADD COLUMN origin TEXT NOT NULL DEFAULT 'reviewer'")


def runtime_findings(conn: sqlite3.Connection, content: str) -> list[ReviewFinding]:
    """Checks the runtime can decide without judgment. Each one is blocking."""
    if not content.strip():
        return [ReviewFinding(severity="blocking", message="The draft is empty.")]
    cited = list(dict.fromkeys(CITATION.findall(content)))
    out = [
        ReviewFinding(severity="blocking", message=f"Citation [claim:{cid}] is not a committed claim.", claim_id=cid)
        for cid in cited
        if get_claim(conn, cid) is None
    ]
    out += [
        ReviewFinding(
            severity="blocking",
            message=f"Contradicting evidence [claim:{cid}] is not cited or addressed.",
            claim_id=cid,
        )
        for cid in contradicting_claims(conn)
        if cid not in cited
    ]
    return out


def gate(
    conn: sqlite3.Connection, review: ReviewResult, *, artifact: ArtifactVersion, revision_pass: int = 0,
    reviewer_task: str | None = None, content: str | None = None,
) -> GateResult:
    """Decide on one review of one exact artifact revision.

    A stale review, or a review by the task that wrote the artifact, raises ValueError.
    Pass `content` (the artifact's text) to add the runtime's own blocking checks.
    """
    if (review.reviewed_artifact_id, review.reviewed_revision) != (artifact.artifact_id, artifact.revision):
        raise ValueError(
            f"stale review: reviewed {review.reviewed_artifact_id} r{review.reviewed_revision}, "
            f"artifact is {artifact.artifact_id} r{artifact.revision}"
        )
    if reviewer_task is not None and reviewer_task == artifact.producing_task:
        raise ValueError(f"self review: {reviewer_task} wrote {artifact.artifact_id} and cannot review it")

    extra = runtime_findings(conn, content) if content is not None else []
    seen = {f.message for f in review.findings}
    extra = [f for f in extra if f.message not in seen]

    blocking = [f.message for f in [*review.findings, *extra] if f.severity == "blocking"]
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
            "INSERT INTO review_findings (artifact_id, revision, severity, message, claim_id, origin) VALUES (?,?,?,?,?,?)",
            [(artifact.artifact_id, artifact.revision, f.severity, f.message, f.claim_id, origin)
             for f, origin in [*((f, "reviewer") for f in review.findings), *((f, "runtime") for f in extra)]],
        )
        conn.execute(
            "INSERT INTO reviews (artifact_id, revision, reviewer_task, disposition, decision, revision_pass, checks, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (artifact.artifact_id, artifact.revision, reviewer_task, review.disposition, decision.value,
             revision_pass, json.dumps(review.checks, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        if decision is GateDecision.accept:
            conn.execute(
                "UPDATE review_findings SET resolved=1 WHERE artifact_id=? AND revision<?",
                (artifact.artifact_id, artifact.revision),
            )
    if decision is GateDecision.accept:
        return GateResult(decision, runtime_findings=extra)
    if decision is GateDecision.accept_with_limitations:
        return GateResult(decision, limitations=blocking, runtime_findings=extra)
    return GateResult(decision, feedback=blocking, runtime_findings=extra)


def list_findings(conn: sqlite3.Connection, artifact_id: str, revision: int | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM review_findings WHERE artifact_id=?", [artifact_id]
    if revision is not None:
        sql, args = sql + " AND revision=?", [*args, revision]
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def latest_review(conn: sqlite3.Connection, artifact_id: str, revision: int) -> sqlite3.Row | None:
    """The most recent gate decision on this exact revision, or None if it was never reviewed."""
    try:
        return conn.execute(
            "SELECT * FROM reviews WHERE artifact_id=? AND revision=? ORDER BY id DESC LIMIT 1",
            (artifact_id, revision),
        ).fetchone()
    except sqlite3.OperationalError:  # review schema not created
        return None


def list_reviews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute("SELECT * FROM reviews ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
