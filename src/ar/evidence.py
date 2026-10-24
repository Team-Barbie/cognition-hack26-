"""Claims and their evidence links. The runtime assigns claim ids and stores only verified claims.

Acceptance is fail-closed: a draft with any rejected link is rejected whole. Rejections are kept
in `claim_rejections` with a precise code, so a run can show what an agent tried and why it was
stopped. A retry that resubmits an already committed draft gets the committed claim back.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ar.broker import normalize_quote
from ar.clock import utc_now as _now
from ar.imports import ImportRecord, import_for_source
from ar.models import Claim, ClaimDraft, EvidenceLink, LineSelector, SourceRef

UNLINKED_OK = {"inferred", "hypothesized"}
CLAIM_REF = re.compile(r"\bclaim-\d+\b")
CITATION = re.compile(r"\[claim:([^\]\s]+)\]")  # how a deliverable cites a committed claim

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
    created_at TEXT NOT NULL,
    digest TEXT
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
CREATE TABLE IF NOT EXISTS claim_rejections (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    link_index INTEGER,
    hint TEXT NOT NULL,
    draft TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Broker reasons for EVIDENCE_REJECTED, mapped to precise codes. Unmatched reasons keep the broker code.
_REASON_CODES = [
    ("unknown receipt", "RECEIPT_NOT_FOUND"),
    ("not issued to this attempt", "RECEIPT_NOT_TRANSFERABLE"),
    ("receipt covers", "RECEIPT_WRONG_SOURCE"),
    ("not covered by receipt", "SPAN_NOT_DELIVERED"),
    ("empty quote", "EMPTY_QUOTE"),
    ("quote not found", "QUOTE_NOT_AT_LOCATOR"),
]

SourceLines = Callable[[SourceRef], "list[str] | None"]


class Verifier(Protocol):
    def verify_evidence_link(self, link: EvidenceLink, *, task_id: str, attempt_id: str) -> None:
        """Raise to reject the link."""


@dataclass
class Rejection:
    draft: ClaimDraft
    reason: str
    code: str = "EVIDENCE_REJECTED"
    link_index: int | None = None
    hint: str = ""  # e.g. where the quote actually sits in the source
    id: str = ""  # `rej-<n>`, set once stored


@dataclass
class AcceptResult:
    accepted: list[Claim] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)  # ids returned for drafts already committed


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
        if "digest" not in cols:  # databases created before claim digests
            conn.execute("ALTER TABLE claims ADD COLUMN digest TEXT")


def draft_digest(task_id: str, draft: ClaimDraft) -> str:
    """Identity of a committed mutation: the task plus the canonical draft, never the attempt."""
    payload = json.dumps({"task_id": task_id, "draft": draft.model_dump(mode="json")}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_lines_from(broker) -> SourceLines:
    """Read pinned snapshot lines for display. Read-only, and never issues a read receipt."""

    def lines(ref: SourceRef) -> list[str] | None:
        row = broker.conn.execute(
            "SELECT representation_sha256 FROM sources WHERE source_id=? AND revision=?",
            (ref.source_id, ref.revision),
        ).fetchone()
        if row is None:
            return None
        return broker.blobs.get(row[0]).decode("utf-8").splitlines()

    return lines


def locate_quote(lines: list[str], quote: str, max_span: int = 20) -> LineSelector | None:
    """Smallest line range whose normalized text holds the normalized quote."""
    needle = normalize_quote(quote)
    if not needle:
        return None
    for width in range(1, min(len(lines), max_span) + 1):
        for start in range(1, len(lines) - width + 2):
            if needle in normalize_quote("\n".join(lines[start - 1 : start - 1 + width])):
                return LineSelector(start=start, end=start + width - 1)
    return None


def _code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    message = str(getattr(exc, "message", "") or exc)
    if code in (None, "EVIDENCE_REJECTED"):
        for needle, precise in _REASON_CODES:
            if needle in message:
                return precise
    return code or "EVIDENCE_REJECTED"


def _hint(link: EvidenceLink, code: str, source_lines: SourceLines | None) -> str:
    if code != "QUOTE_NOT_AT_LOCATOR" or source_lines is None:
        return ""
    lines = source_lines(link.source_ref)
    if lines is None:
        return ""
    found = locate_quote(lines, link.quote)
    if found is None:
        return f"The quote does not appear anywhere in {link.source_ref.source_id}."
    return f"The quote appears at lines {found.start}-{found.end} of {link.source_ref.source_id}."


def link_provenance(conn: sqlite3.Connection, link: EvidenceLink) -> ImportRecord | None:
    """The import a link cites, if its source came from `imports.register`."""
    return import_for_source(conn, link.source_ref.source_id)


def _check(
    draft: ClaimDraft, verifier: Verifier, task_id: str, attempt_id: str,
    known_ids: set[str], source_lines: SourceLines | None, conn: sqlite3.Connection | None = None,
) -> Rejection | None:
    if not draft.statement.strip():
        return Rejection(draft, "claim has no statement", "EMPTY_STATEMENT")
    if not draft.evidence_links:
        if draft.epistemic_status not in UNLINKED_OK:
            return Rejection(draft, f"{draft.epistemic_status} claim has no evidence links", "NO_EVIDENCE")
        if draft.epistemic_status == "inferred":
            if not draft.derivation.strip():
                return Rejection(draft, "inferred claim has no derivation", "NO_DERIVATION")
            missing = sorted(set(CLAIM_REF.findall(draft.derivation)) - known_ids)
            if missing:
                return Rejection(draft, f"derivation cites {', '.join(missing)}, not a committed claim", "UNKNOWN_DERIVATION")
        return None
    for i, link in enumerate(draft.evidence_links):
        if not link.quote.strip():
            return Rejection(draft, "empty quote", "EMPTY_QUOTE", i)
        if link.locator.end < link.locator.start:
            return Rejection(draft, f"lines {link.locator.start}-{link.locator.end} run backwards", "INVALID_SELECTOR", i)
        imported = link_provenance(conn, link) if conn is not None else None
        if imported is not None and imported.provenance == "derived":
            return Rejection(
                draft, f"{imported.id} is derived material generated by this project, not independent evidence",
                "DERIVED_SOURCE", i,
            )
        try:
            verifier.verify_evidence_link(link, task_id=task_id, attempt_id=attempt_id)
        except Exception as exc:  # any verifier error rejects the whole claim
            code = _code(exc)
            return Rejection(draft, str(exc) or type(exc).__name__, code, i, _hint(link, code, source_lines))
    return None


def _next_number(conn: sqlite3.Connection, table: str, prefix: str) -> int:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM {table}")]  # noqa: S608 - fixed table names
    nums = [int(i[len(prefix):]) for i in ids if i.startswith(prefix) and i[len(prefix):].isdigit()]
    return max(nums, default=0) + 1


def accept_claims(
    conn: sqlite3.Connection, verifier: Verifier, *, task_id: str, attempt_id: str, drafts: list[ClaimDraft],
    source_lines: SourceLines | None = None,
) -> AcceptResult:
    """One transaction per call. A claim with any rejected link is rejected whole and not stored.

    `source_lines` (see `source_lines_from(broker)`) lets a rejection say where a misplaced quote is.
    """
    result = AcceptResult()
    with conn:
        # Multiple review instances may accept evidence concurrently. Lock before
        # allocating sequential claim/rejection ids or checking draft digests.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        n = _next_number(conn, "claims", "claim-")
        r = _next_number(conn, "claim_rejections", "rej-")
        known = {row[0] for row in conn.execute("SELECT id FROM claims")}
        for draft in drafts:
            digest = draft_digest(task_id, draft)
            existing = conn.execute("SELECT id FROM claims WHERE digest=?", (digest,)).fetchone()
            if existing is not None:
                result.accepted.append(get_claim(conn, existing[0]))
                result.reused.append(existing[0])
                continue
            rejection = _check(draft, verifier, task_id, attempt_id, known, source_lines, conn)
            if rejection is not None:
                rejection.id = f"rej-{r}"
                r += 1
                conn.execute(
                    "INSERT INTO claim_rejections (id, task_id, attempt_id, code, reason, link_index, hint, draft, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (rejection.id, task_id, attempt_id, rejection.code, rejection.reason, rejection.link_index,
                     rejection.hint, draft.model_dump_json(), _now()),
                )
                result.rejected.append(rejection)
                continue
            claim = Claim(id=f"claim-{n}", origin_task=task_id, **draft.model_dump())
            n += 1
            conn.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (claim.id, claim.statement, claim.kind, claim.epistemic_status, claim.scope,
                 claim.derivation, json.dumps(claim.limitations), task_id, attempt_id, _now(), digest),
            )
            conn.executemany(
                "INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?,?)",
                [(claim.id, l.relation, l.source_ref.source_id, l.source_ref.revision,
                  l.locator.start, l.locator.end, l.quote, l.read_receipt_id)
                 for l in claim.evidence_links],
            )
            known.add(claim.id)
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


def claim_attempt(conn: sqlite3.Connection, claim_id: str) -> str | None:
    row = conn.execute("SELECT attempt_id FROM claims WHERE id=?", (claim_id,)).fetchone()
    return row[0] if row else None


def _rejection(row: sqlite3.Row) -> Rejection:
    return Rejection(
        draft=ClaimDraft.model_validate_json(row["draft"]), reason=row["reason"], code=row["code"],
        link_index=row["link_index"], hint=row["hint"], id=row["id"],
    )


def list_rejections(conn: sqlite3.Connection) -> list[tuple[Rejection, str, str]]:
    """Stored rejections in order, each with its task id and attempt id."""
    rows = conn.execute("SELECT * FROM claim_rejections ORDER BY seq").fetchall()
    return [(_rejection(r), r["task_id"], r["attempt_id"]) for r in rows]


def contradicting_claims(conn: sqlite3.Connection, scope: list[str] | None = None) -> list[str]:
    """Claims with a contradicts link, limited to `scope` (e.g. an artifact's depends_on) when given."""
    rows = conn.execute(
        "SELECT DISTINCT claim_id FROM evidence_links WHERE relation='contradicts' ORDER BY rowid"
    ).fetchall()
    ids = [r[0] for r in rows]
    return [i for i in ids if i in scope] if scope else ids


def _window(lines: list[str], loc: LineSelector, context: int) -> list[str]:
    lo, hi = max(1, loc.start - context), min(len(lines), loc.end + context)
    width = len(str(hi))
    return [
        f"    {'>' if loc.start <= n <= loc.end else ' '} {n:>{width}} | {lines[n - 1]}"
        for n in range(lo, hi + 1)
    ]


def _evidence_lines(link: EvidenceLink, source_lines: SourceLines | None, receipts: dict, context: int) -> list[str]:
    ref, loc = link.source_ref, link.locator
    out = [
        f"evidence [{link.relation}]: {ref.source_id} r{ref.revision}, lines {loc.start}-{loc.end}",
        f"  quote: \"{link.quote}\"",
    ]
    lines = source_lines(ref) if source_lines else None
    if lines is not None and loc.end <= len(lines):
        out += _window(lines, loc, context)
    r = receipts.get(link.read_receipt_id)
    if r is not None:
        out.append(
            f"  read receipt: {r.id} -> {r.task_id}/{r.attempt_id}, packet {r.packet_id}, "
            f"lines {r.selector.start}-{r.selector.end}, {r.delivered_at}"
        )
    else:
        out.append(f"  read receipt: {link.read_receipt_id}")
    return out


def inspect_claim(conn: sqlite3.Connection, claim_id: str, *, broker=None, context: int = 1) -> str:
    """Statement, source, lines, quote, receipt, origin task. Also accepts a rejection id (`rej-<n>`).

    With `broker`, shows the cited lines around the locator and the full receipt.
    """
    source_lines = source_lines_from(broker) if broker is not None else None
    row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if row is not None:
        claim = _claim(conn, row)
        attempt = row["attempt_id"]
        receipts = {r.id: r for r in broker.receipts_for(claim.origin_task, attempt)} if broker else {}
        out = [
            f"{claim.id}: {claim.statement}",
            f"kind: {claim.kind}   status: {claim.epistemic_status}   scope: {claim.scope or '-'}",
            f"origin: task {claim.origin_task}, attempt {attempt}",
        ]
        if claim.derivation:
            out.append(f"derivation: {claim.derivation}")
        if not claim.evidence_links:
            out.append("evidence: none (not a source-backed claim)")
        for link in claim.evidence_links:
            out += _evidence_lines(link, source_lines, receipts, context)
            imported = link_provenance(conn, link)
            if imported is not None:
                out.append(f"  provenance: {imported.provenance} ({imported.id}, executor {imported.executor})")
        out += [f"limitation: {lim}" for lim in claim.limitations]
        return "\n".join(out)

    rej = conn.execute("SELECT * FROM claim_rejections WHERE id=?", (claim_id,)).fetchone()
    if rej is None:
        return f"No such claim: {claim_id}"
    rejection = _rejection(rej)
    receipts = {r.id: r for r in broker.receipts_for(rej["task_id"], rej["attempt_id"])} if broker else {}
    out = [
        f"{rejection.id}: REJECTED {rejection.code}",
        f"statement: {rejection.draft.statement}",
        f"origin: task {rej['task_id']}, attempt {rej['attempt_id']}",
        f"reason: {rejection.reason}",
    ]
    if rejection.hint:
        out.append(f"hint: {rejection.hint}")
    for i, link in enumerate(rejection.draft.evidence_links):
        marker = "  <- rejected" if i == rejection.link_index else ""
        lines = _evidence_lines(link, source_lines, receipts, context)
        out += [lines[0] + marker, *lines[1:]]
    return "\n".join(out)
