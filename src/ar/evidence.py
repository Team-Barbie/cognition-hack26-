"""Claims and their evidence links. The runtime assigns claim ids and stores only verified claims."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ar.models import Claim, ClaimDraft, EvidenceLink, LineSelector, SourceRef

UNLINKED_OK = {"inferred", "hypothesized"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    kind TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    scope TEXT NOT NULL,
    derivation TEXT NOT NULL,
    limitations TEXT NOT NULL,
    origin_task TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_links (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    relation TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    quote TEXT NOT NULL,
    read_receipt_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_links_claim ON evidence_links(claim_id);
"""


class Verifier(Protocol):
    def verify_evidence_link(self, link: EvidenceLink, *, task_id: str, attempt_id: str) -> None:
        """Raise to reject the link."""


@dataclass
class Rejection:
    draft: ClaimDraft
    reason: str


@dataclass
class AcceptResult:
    accepted: list[Claim] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def _check(draft: ClaimDraft, verifier: Verifier, task_id: str, attempt_id: str) -> str | None:
    if not draft.evidence_links:
        if draft.epistemic_status in UNLINKED_OK:
            return None
        return f"{draft.epistemic_status} claim has no evidence links"
    for link in draft.evidence_links:
        try:
            verifier.verify_evidence_link(link, task_id=task_id, attempt_id=attempt_id)
        except Exception as exc:  # any verifier error rejects the whole claim
            return str(exc) or type(exc).__name__
    return None


def accept_claims(
    conn: sqlite3.Connection, verifier: Verifier, *, task_id: str, attempt_id: str, drafts: list[ClaimDraft]
) -> AcceptResult:
    """One transaction per call. A claim with any rejected link is rejected whole and not stored."""
    result = AcceptResult()
    with conn:
        n = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        for draft in drafts:
            reason = _check(draft, verifier, task_id, attempt_id)
            if reason is not None:
                result.rejected.append(Rejection(draft, reason))
                continue
            n += 1
            claim = Claim(id=f"claim-{n}", origin_task=task_id, **draft.model_dump())
            conn.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                (claim.id, claim.statement, claim.kind, claim.epistemic_status, claim.scope,
                 claim.derivation, json.dumps(claim.limitations), task_id, attempt_id,
                 datetime.now(UTC).isoformat()),
            )
            conn.executemany(
                "INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?,?)",
                [(claim.id, l.relation, l.source_ref.source_id, l.source_ref.revision,
                  l.locator.start, l.locator.end, l.quote, l.read_receipt_id)
                 for l in claim.evidence_links],
            )
            result.accepted.append(claim)
    return result


def _links(conn: sqlite3.Connection, claim_id: str) -> list[EvidenceLink]:
    rows = conn.execute("SELECT * FROM evidence_links WHERE claim_id=? ORDER BY rowid", (claim_id,))
    return [
        EvidenceLink(
            relation=r["relation"],
            source_ref=SourceRef(source_id=r["source_id"], revision=r["revision"]),
            locator=LineSelector(start=r["line_start"], end=r["line_end"]),
            quote=r["quote"],
            read_receipt_id=r["read_receipt_id"],
        )
        for r in rows
    ]


def _claim(conn: sqlite3.Connection, row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"], statement=row["statement"], kind=row["kind"],
        epistemic_status=row["epistemic_status"], scope=row["scope"], derivation=row["derivation"],
        limitations=json.loads(row["limitations"]), origin_task=row["origin_task"],
        evidence_links=_links(conn, row["id"]),
    )


def get_claim(conn: sqlite3.Connection, claim_id: str) -> Claim | None:
    row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    return _claim(conn, row) if row else None


def list_claims(conn: sqlite3.Connection) -> list[Claim]:
    return [_claim(conn, r) for r in conn.execute("SELECT * FROM claims ORDER BY rowid")]


def inspect_claim(conn: sqlite3.Connection, claim_id: str) -> str:
    row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        return f"No such claim: {claim_id}"
    claim = _claim(conn, row)
    out = [
        f"{claim.id}: {claim.statement}",
        f"kind: {claim.kind}   status: {claim.epistemic_status}   scope: {claim.scope or '-'}",
        f"origin: task {claim.origin_task}, attempt {row['attempt_id']}",
    ]
    if claim.derivation:
        out.append(f"derivation: {claim.derivation}")
    if not claim.evidence_links:
        out.append("evidence: none (not a source-backed claim)")
    for link in claim.evidence_links:
        ref, loc = link.source_ref, link.locator
        out += [
            f"evidence [{link.relation}]: {ref.source_id} r{ref.revision}, lines {loc.start}-{loc.end}",
            f"  quote: \"{link.quote}\"",
            f"  read receipt: {link.read_receipt_id}",
        ]
    out += [f"limitation: {lim}" for lim in claim.limitations]
    return "\n".join(out)
