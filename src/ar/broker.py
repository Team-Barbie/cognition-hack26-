"""The broker: snapshotted sources, read receipts, evidence checks, immutable artifacts."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ar.clock import utc_now as _now
from ar.models import (
    ArtifactVersion,
    EvidenceLink,
    LineSelector,
    ReadReceipt,
    Record,
    SourceRef,
)
from ar.packet import DeliveredRead
from ar.permissions import PermissionPolicy, effective_capabilities
from ar.snapshots import BlobStore, BrokerError, atomic_write, resolve_inside

__all__ = [
    "QUOTE_NORMALIZATION",
    "Broker",
    "BrokerError",
    "ReadPage",
    "StagedRead",
    "ensure_schema",
    "normalize_quote",
]

# Error codes: SOURCE_NOT_FOUND, ACCESS_DENIED, STALE_REVISION, INVALID_SELECTOR,
# REQUIRED_READ_INCOMPLETE, REVISION_CONFLICT, EVIDENCE_REJECTED, INVALID_ARTIFACT,
# ARTIFACT_NOT_FOUND, BLOB_CORRUPT, and CURSOR_MISMATCH (a continuation cursor was tampered with
# or reused against another source, revision, or original selector).

QUOTE_NORMALIZATION = "ws-collapse-v1"
PROTECTED_DIRS = {"input", ".research"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    path TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    representation_sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (source_id, revision)
);
CREATE TABLE IF NOT EXISTS read_receipts (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    representation_sha256 TEXT NOT NULL,
    sel_start INTEGER NOT NULL,
    sel_end INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_attempt ON read_receipts (task_id, attempt_id);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    artifact_type TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    producing_task TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    parent_revision INTEGER,
    depends_on TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (artifact_id, revision)
);
CREATE TABLE IF NOT EXISTS receipt_links (
    receipt_id TEXT NOT NULL UNIQUE,
    from_receipt_id TEXT NOT NULL,
    from_attempt_id TEXT NOT NULL,
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability TEXT NOT NULL,
    reason TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)


def normalize_quote(text: str) -> str:
    """`ws-collapse-v1`: join `splitlines()` with newlines, collapse whitespace runs, trim."""
    return re.sub(r"\s+", " ", "\n".join(text.splitlines())).strip()


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _gaps(spans: list[tuple[int, int]], total: int) -> list[list[int]]:
    gaps: list[list[int]] = []
    cursor = 1
    for start, end in _merge(spans):
        if start > cursor:
            gaps.append([cursor, start - 1])
        cursor = max(cursor, end + 1)
    if cursor <= total:
        gaps.append([cursor, total])
    return gaps


@contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """A transaction that takes the write lock up front so check-then-insert cannot race."""
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        yield


class ReadPage(Record):
    """One page of a read; each page carries its own receipt in `delivered`."""

    delivered: DeliveredRead
    coverage: Literal["full", "partial"]
    truncated: bool
    next_cursor: str | None = None


@dataclass(frozen=True)
class StagedRead:
    """Content fetched for context packing; no receipt exists until `Broker.deliver`."""

    task_id: str
    attempt_id: str
    packet_id: str
    ref: SourceRef
    representation_sha256: str
    selector: LineSelector
    lines: tuple[str, ...]
    label: str = ""


class Broker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        project_root: Path,
        policy: PermissionPolicy | None = None,
    ) -> None:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        self.conn = conn
        self.policy = policy
        self._key: bytes | None = None
        self.project_root = Path(project_root)
        self.blobs = BlobStore(self.project_root)

    # sources

    def register_source(self, source_id: str, path: Path) -> SourceRef:
        data = Path(path).read_bytes()
        original = self.blobs.put(data)
        text = "\n".join(_lines(data.decode("utf-8", errors="replace").lstrip("﻿")))
        representation = self.blobs.put(text.encode("utf-8"))
        count = len(_lines(text))
        try:
            stored = str(Path(path).resolve().relative_to(self.project_root.resolve()))
        except ValueError:
            stored = str(path)
        with _write_txn(self.conn):
            row = self.conn.execute(
                "SELECT revision, original_sha256 FROM sources WHERE source_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if row and row["original_sha256"] == original:
                return SourceRef(source_id=source_id, revision=row["revision"])
            revision = row["revision"] + 1 if row else 1
            self.conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                (source_id, revision, stored, original, representation, count, _now()),
            )
        return SourceRef(source_id=source_id, revision=revision)

    def _source(self, ref: SourceRef, *, latest_only: bool = True) -> sqlite3.Row:
        latest = self.conn.execute(
            "SELECT MAX(revision) FROM sources WHERE source_id=?", (ref.source_id,)
        ).fetchone()[0]
        if latest is None:
            raise BrokerError("SOURCE_NOT_FOUND", f"unknown source {ref.source_id!r}")
        if latest_only and ref.revision != latest:
            raise BrokerError(
                "STALE_REVISION",
                f"{ref.source_id} revision {ref.revision} is not current ({latest})",
                {"source_id": ref.source_id, "current_revision": latest},
            )
        row = self.conn.execute(
            "SELECT * FROM sources WHERE source_id=? AND revision=?",
            (ref.source_id, ref.revision),
        ).fetchone()
        if row is None:
            raise BrokerError("SOURCE_NOT_FOUND", f"{ref.source_id} has no revision {ref.revision}")
        return row

    def _source_lines(self, row: sqlite3.Row) -> list[str]:
        return _lines(self.blobs.get(row["representation_sha256"]).decode("utf-8"))

    def source_line_count(self, ref: SourceRef) -> int:
        return self._source(ref)["line_count"]

    def list_sources(self) -> list[SourceRef]:
        rows = self.conn.execute(
            "SELECT source_id, revision FROM sources s WHERE revision="
            "(SELECT MAX(revision) FROM sources WHERE source_id=s.source_id) ORDER BY source_id"
        ).fetchall()
        return [SourceRef(source_id=r["source_id"], revision=r["revision"]) for r in rows]

    # reads

    def read(
        self,
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        ref: SourceRef,
        start: int | None = None,
        end: int | None = None,
        label: str = "",
        capabilities: set[str] | None = None,
    ) -> DeliveredRead:
        staged = self.stage_read(
            task_id=task_id, attempt_id=attempt_id, packet_id=packet_id, ref=ref,
            start=start, end=end, label=label, capabilities=capabilities,
        )
        return self._deliver(staged, staged.selector.start, staged.selector.end)

    def _resolve(
        self, ref: SourceRef, start: int | None, end: int | None
    ) -> tuple[sqlite3.Row, list[str], int, int]:
        row = self._source(ref)
        lines = self._source_lines(row)
        total = len(lines)
        start = 1 if start is None else start
        end = total if end is None else end
        if not 1 <= start <= end <= total:
            raise BrokerError(
                "INVALID_SELECTOR",
                f"lines {start}-{end} are outside 1-{total} of {ref.source_id}",
                {"line_count": total},
            )
        return row, lines, start, end

    @staticmethod
    def _stage(
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        ref: SourceRef,
        row: sqlite3.Row,
        lines: list[str],
        start: int,
        end: int,
        label: str,
    ) -> StagedRead:
        return StagedRead(
            task_id=task_id,
            attempt_id=attempt_id,
            packet_id=packet_id,
            ref=ref,
            representation_sha256=row["representation_sha256"],
            selector=LineSelector(start=start, end=end),
            lines=tuple(lines[start - 1 : end]),
            label=label,
        )

    def _insert_receipt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        ref: SourceRef,
        representation_sha256: str,
        start: int,
        end: int,
    ) -> str:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM read_receipts").fetchone()[0]
        receipt_id = f"read-{seq:04d}"
        self.conn.execute(
            "INSERT INTO read_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq, receipt_id, ref.source_id, ref.revision, representation_sha256,
                start, end, task_id, attempt_id, packet_id, _now(),
            ),
        )
        return receipt_id

    def stage_read(
        self,
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        ref: SourceRef,
        start: int | None = None,
        end: int | None = None,
        label: str = "",
        capabilities: set[str] | None = None,
    ) -> StagedRead:
        """Fetch content without issuing a receipt; `deliver` credits what the packet kept."""
        self._require(capabilities, "files.read")
        row, lines, start, end = self._resolve(ref, start, end)
        return self._stage(
            task_id=task_id, attempt_id=attempt_id, packet_id=packet_id, ref=ref,
            row=row, lines=lines, start=start, end=end, label=label,
        )

    def deliver(
        self,
        staged: StagedRead,
        included_lines: tuple[int, int] | None = None,
        *,
        capabilities: set[str] | None = None,
    ) -> DeliveredRead:
        """Issue the receipt for the lines actually included in the packet (None = all staged)."""
        self._require(capabilities, "files.read")
        sel = staged.selector
        lo, hi = included_lines if included_lines is not None else (sel.start, sel.end)
        if not sel.start <= lo <= hi <= sel.end:
            raise BrokerError(
                "INVALID_SELECTOR",
                f"lines {lo}-{hi} were not staged (staged {sel.start}-{sel.end})",
                {"staged": [sel.start, sel.end]},
            )
        return self._deliver(staged, lo, hi)

    def _deliver(self, staged: StagedRead, lo: int, hi: int) -> DeliveredRead:
        offset = staged.selector.start
        with _write_txn(self.conn):
            receipt_id = self._insert_receipt(
                task_id=staged.task_id, attempt_id=staged.attempt_id, packet_id=staged.packet_id,
                ref=staged.ref, representation_sha256=staged.representation_sha256,
                start=lo, end=hi,
            )
        return DeliveredRead(
            source_ref=staged.ref,
            selector=LineSelector(start=lo, end=hi),
            content="\n".join(staged.lines[lo - offset : hi - offset + 1]),
            read_receipt_id=receipt_id,
            label=staged.label,
        )

    # cursors

    def _cursor_key(self) -> bytes:
        if self._key is None:
            with _write_txn(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO broker_meta VALUES ('cursor_key', ?)",
                    (secrets.token_hex(32),),
                )
                value = self.conn.execute(
                    "SELECT value FROM broker_meta WHERE key='cursor_key'"
                ).fetchone()[0]
            self._key = bytes.fromhex(value)
        return self._key

    def _seal_cursor(self, fields: list) -> str:
        body = base64.urlsafe_b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()
        mac = hmac.new(self._cursor_key(), body.encode(), "sha256").hexdigest()
        return f"{body}.{mac}"

    def _open_cursor(self, cursor: str) -> list:
        body, _, mac = cursor.partition(".")
        expected = hmac.new(self._cursor_key(), body.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise BrokerError("CURSOR_MISMATCH", "cursor is malformed or was tampered with")
        try:
            fields = json.loads(base64.urlsafe_b64decode(body.encode()))
        except (ValueError, binascii.Error):
            fields = None
        if not (isinstance(fields, list) and len(fields) == 6):
            raise BrokerError("CURSOR_MISMATCH", "cursor is malformed")
        return fields

    def read_page(
        self,
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        ref: SourceRef,
        start: int | None = None,
        end: int | None = None,
        max_bytes: int = 16000,
        cursor: str | None = None,
        label: str = "",
        capabilities: set[str] | None = None,
    ) -> ReadPage:
        """Read at most `max_bytes` (whole lines, at least one) and return a continuation cursor.

        A cursor is bound to the source, revision, representation hash, original selector, and the
        next line. Each page issues its own receipt, so the required-read gate stays closed until
        every page has been read.
        """
        self._require(capabilities, "files.read")
        if max_bytes < 1:
            raise BrokerError("INVALID_SELECTOR", f"max_bytes must be positive, got {max_bytes}")
        fields = self._open_cursor(cursor) if cursor is not None else None
        if fields is not None and (fields[0], fields[1]) != (ref.source_id, ref.revision):
            raise BrokerError(
                "CURSOR_MISMATCH",
                f"cursor was issued for {fields[0]} revision {fields[1]}, not "
                f"{ref.source_id} revision {ref.revision}",
                {"source_id": fields[0], "revision": fields[1]},
            )
        row, lines, start, end = self._resolve(ref, start, end)
        first = start
        if fields is not None:
            if fields[2] != row["representation_sha256"] or (fields[3], fields[4]) != (start, end):
                raise BrokerError(
                    "CURSOR_MISMATCH",
                    "cursor does not match this snapshot or the original selector",
                    {"original": [fields[3], fields[4]]},
                )
            first = fields[5]
            if not start < first <= end:
                raise BrokerError("CURSOR_MISMATCH", "cursor position is outside the selector")
        last, size = first, 0
        while last <= end:
            size += len(lines[last - 1].encode("utf-8")) + (1 if last > first else 0)
            if size > max_bytes and last > first:
                break
            last += 1
        last -= 1
        staged = self._stage(
            task_id=task_id, attempt_id=attempt_id, packet_id=packet_id, ref=ref,
            row=row, lines=lines, start=first, end=last, label=label,
        )
        truncated = last < end
        return ReadPage(
            delivered=self._deliver(staged, first, last),
            coverage="full" if (first, last) == (start, end) else "partial",
            truncated=truncated,
            next_cursor=(
                self._seal_cursor(
                    [ref.source_id, ref.revision, row["representation_sha256"], start, end, last + 1]
                )
                if truncated
                else None
            ),
        )

    # resume

    def link_receipts(
        self,
        task_id: str,
        new_attempt_id: str,
        packet_id: str,
        from_attempt_id: str,
        included: Mapping[str, tuple[int, int] | None] | Iterable[str],
        *,
        capabilities: set[str] | None = None,
    ) -> list[ReadReceipt]:
        """Credit the new attempt only for prior content its packet actually includes.

        `included` maps a prior receipt id to the lines kept (None or a bare id = its whole span).
        Each link is a fresh receipt for the new attempt plus a row in `receipt_links`.
        """
        self._require(capabilities, "files.read")
        wanted = dict(included) if isinstance(included, Mapping) else dict.fromkeys(included)
        plan: list[tuple[sqlite3.Row, int, int]] = []
        for receipt_id, lines in wanted.items():
            row = self.conn.execute(
                "SELECT * FROM read_receipts WHERE id=?", (receipt_id,)
            ).fetchone()
            if row is None or (row["task_id"], row["attempt_id"]) != (task_id, from_attempt_id):
                raise BrokerError(
                    "ACCESS_DENIED",
                    f"receipt {receipt_id!r} was not issued to {task_id}/{from_attempt_id}",
                )
            current = self._source(
                SourceRef(source_id=row["source_id"], revision=row["source_revision"])
            )
            if current["representation_sha256"] != row["representation_sha256"]:
                raise BrokerError("STALE_REVISION", f"receipt {receipt_id} covers a replaced snapshot")
            lo, hi = lines if lines is not None else (row["sel_start"], row["sel_end"])
            if not row["sel_start"] <= lo <= hi <= row["sel_end"]:
                raise BrokerError(
                    "INVALID_SELECTOR",
                    f"lines {lo}-{hi} are outside receipt {receipt_id} lines "
                    f"{row['sel_start']}-{row['sel_end']}",
                )
            plan.append((row, lo, hi))
        linked: list[str] = []
        with _write_txn(self.conn):
            for row, lo, hi in plan:
                new_id = self._insert_receipt(
                    task_id=task_id, attempt_id=new_attempt_id, packet_id=packet_id,
                    ref=SourceRef(source_id=row["source_id"], revision=row["source_revision"]),
                    representation_sha256=row["representation_sha256"], start=lo, end=hi,
                )
                self.conn.execute(
                    "INSERT INTO receipt_links VALUES (?,?,?,?)",
                    (new_id, row["id"], from_attempt_id, _now()),
                )
                linked.append(new_id)
        return [
            self._receipt(self.conn.execute("SELECT * FROM read_receipts WHERE id=?", (i,)).fetchone())
            for i in linked
        ]

    # permissions

    def capabilities_for(
        self,
        role: str,
        task_allowed: Iterable[str],
        host_restrictions: Iterable[str] | None = None,
    ) -> frozenset[str]:
        """Role, task, project policy, and host restrictions intersected, minus revocations."""
        if self.policy is None:
            caps = frozenset(task_allowed)
            if host_restrictions is not None:
                caps &= frozenset(host_restrictions)
        else:
            caps = effective_capabilities(role, task_allowed, self.policy, host_restrictions)
        return caps - self.revoked()

    def check_capability(
        self,
        role: str,
        task_allowed: Iterable[str],
        capability: str,
        host_restrictions: Iterable[str] | None = None,
    ) -> None:
        if capability not in self.capabilities_for(role, task_allowed, host_restrictions):
            raise BrokerError(
                "ACCESS_DENIED",
                f"{role} may not use {capability}",
                {"capability": capability, "role": role},
            )

    def revoke(self, capability: str, reason: str) -> int:
        """Deny `capability` on every later broker call, whatever policy version a task started on."""
        with _write_txn(self.conn):
            cur = self.conn.execute(
                "INSERT INTO revocations (capability, reason, revoked_at) VALUES (?,?,?)",
                (capability, reason, _now()),
            )
            has_events = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
            ).fetchone()
            if has_events:
                self.conn.execute(
                    "INSERT INTO events (run_id, task_id, kind, payload_json, created_at) "
                    "VALUES (NULL, NULL, 'capability_revoked', ?, ?)",
                    (json.dumps({"capability": capability, "reason": reason}, sort_keys=True), _now()),
                )
            return cur.lastrowid

    def revoked(self) -> frozenset[str]:
        rows = self.conn.execute("SELECT DISTINCT capability FROM revocations").fetchall()
        return frozenset(r["capability"] for r in rows)

    def _require(self, capabilities: set[str] | None, capability: str) -> None:
        """Revocations always apply; the caller's set is checked when one is supplied."""
        row = self.conn.execute(
            "SELECT reason FROM revocations WHERE capability=? ORDER BY id DESC LIMIT 1",
            (capability,),
        ).fetchone()
        if row is not None:
            raise BrokerError(
                "ACCESS_DENIED",
                f"{capability} was revoked: {row['reason']}",
                {"capability": capability, "revoked": True},
            )
        if capabilities is not None and capability not in capabilities:
            raise BrokerError(
                "ACCESS_DENIED", f"{capability} is not granted", {"capability": capability}
            )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> ReadReceipt:
        return ReadReceipt(
            id=row["id"],
            source_ref=SourceRef(source_id=row["source_id"], revision=row["source_revision"]),
            representation_sha256=row["representation_sha256"],
            selector=LineSelector(start=row["sel_start"], end=row["sel_end"]),
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            packet_id=row["packet_id"],
            delivered_at=row["delivered_at"],
        )

    def receipts_for(self, task_id: str, attempt_id: str) -> list[ReadReceipt]:
        rows = self.conn.execute(
            "SELECT * FROM read_receipts WHERE task_id=? AND attempt_id=? ORDER BY seq",
            (task_id, attempt_id),
        ).fetchall()
        return [self._receipt(r) for r in rows]

    def check_required_reads(
        self, *, task_id: str, attempt_id: str, required: list[SourceRef]
    ) -> None:
        receipts = self.receipts_for(task_id, attempt_id)
        incomplete: list[dict] = []
        for ref in required:
            row = self._source(ref)
            spans = [
                (r.selector.start, r.selector.end)
                for r in receipts
                if r.source_ref == ref and r.representation_sha256 == row["representation_sha256"]
            ]
            missing = _gaps(spans, row["line_count"])
            if missing:
                incomplete.append({"source_id": ref.source_id, "missing": missing})
        if incomplete:
            first = incomplete[0]
            raise BrokerError(
                "REQUIRED_READ_INCOMPLETE",
                "required reading is incomplete: "
                + "; ".join(f"{i['source_id']} lines {i['missing']}" for i in incomplete),
                {"source_id": first["source_id"], "missing": first["missing"], "incomplete": incomplete},
            )

    # evidence

    def verify_evidence_link(self, link: EvidenceLink, *, task_id: str, attempt_id: str) -> None:
        def reject(reason: str) -> BrokerError:
            return BrokerError("EVIDENCE_REJECTED", reason, {"receipt_id": link.read_receipt_id})

        row = self.conn.execute(
            "SELECT * FROM read_receipts WHERE id=?", (link.read_receipt_id,)
        ).fetchone()
        if row is None:
            raise reject(f"unknown receipt {link.read_receipt_id!r}")
        receipt = self._receipt(row)
        if (receipt.task_id, receipt.attempt_id) != (task_id, attempt_id):
            raise reject("receipt not issued to this attempt")
        if receipt.source_ref != link.source_ref:
            raise reject(f"receipt covers {receipt.source_ref.source_id}, not {link.source_ref.source_id}")
        loc, sel = link.locator, receipt.selector
        if not sel.start <= loc.start <= loc.end <= sel.end:
            raise reject(f"lines {loc.start}-{loc.end} are not covered by receipt lines {sel.start}-{sel.end}")
        lines = self._source_lines(self._source(receipt.source_ref, latest_only=False))
        cited = normalize_quote("\n".join(lines[loc.start - 1 : loc.end]))
        quote = normalize_quote(link.quote)
        if not quote:
            raise reject("empty quote")
        if quote not in cited:
            raise reject(f"quote not found in lines {loc.start}-{loc.end} of {link.source_ref.source_id}")

    # artifacts

    @staticmethod
    def _check_content(content: str) -> None:
        if not content or not content.strip():
            raise BrokerError("INVALID_ARTIFACT", "artifact content is empty")

    @staticmethod
    def _version(row: sqlite3.Row) -> ArtifactVersion:
        return ArtifactVersion(
            artifact_id=row["artifact_id"],
            revision=row["revision"],
            artifact_type=row["artifact_type"],
            content_type=row["content_type"],
            content_sha256=row["content_sha256"],
            producing_task=row["producing_task"],
            parent_revision=row["parent_revision"],
            depends_on=json.loads(row["depends_on"]),
        )

    def _insert_version(self, row: tuple) -> None:
        try:
            self.conn.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        except sqlite3.IntegrityError:
            raise BrokerError(
                "REVISION_CONFLICT", f"{row[0]} revision {row[1]} was already committed"
            ) from None

    def create_artifact(
        self,
        *,
        task_id: str,
        attempt_id: str,
        artifact_type: str,
        content: str,
        depends_on: Sequence[str] = (),
        capabilities: set[str] | None = None,
    ) -> ArtifactVersion:
        self._require(capabilities, "artifacts.create")
        if not artifact_type or not artifact_type.strip():
            raise BrokerError("INVALID_ARTIFACT", "artifact_type is empty")
        self._check_content(content)
        digest = self.blobs.put(content.encode("utf-8"))
        with _write_txn(self.conn):
            n = self.conn.execute("SELECT COUNT(DISTINCT artifact_id) + 1 FROM artifacts").fetchone()[0]
            artifact_id = f"artifact-{n}"
            self._insert_version(
                (
                    artifact_id, 1, artifact_type, "text/markdown", digest, task_id, attempt_id,
                    None, json.dumps(list(depends_on)), "", _now(),
                )
            )
        return self.get_artifact(artifact_id, 1)[0]

    def revise_artifact(
        self,
        *,
        task_id: str,
        attempt_id: str,
        artifact_id: str,
        expected_revision: int,
        content: str,
        change_reason: str = "",
        capabilities: set[str] | None = None,
    ) -> ArtifactVersion:
        self._require(capabilities, "artifacts.revise")
        self._check_content(content)
        digest = self.blobs.put(content.encode("utf-8"))
        with _write_txn(self.conn):
            current = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? ORDER BY revision DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
            if current is None:
                raise BrokerError("ARTIFACT_NOT_FOUND", f"unknown artifact {artifact_id!r}")
            if current["revision"] != expected_revision:
                raise BrokerError(
                    "REVISION_CONFLICT",
                    f"{artifact_id} is at revision {current['revision']}, expected {expected_revision}",
                    {"artifact_id": artifact_id, "current_revision": current["revision"]},
                )
            revision = expected_revision + 1
            self._insert_version(
                (
                    artifact_id, revision, current["artifact_type"], current["content_type"], digest,
                    task_id, attempt_id, expected_revision, current["depends_on"], change_reason, _now(),
                )
            )
        return self.get_artifact(artifact_id, revision)[0]

    def get_artifact(
        self, artifact_id: str, revision: int | None = None
    ) -> tuple[ArtifactVersion, str]:
        if revision is None:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? ORDER BY revision DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND revision=?",
                (artifact_id, revision),
            ).fetchone()
        if row is None:
            raise BrokerError("ARTIFACT_NOT_FOUND", f"no artifact {artifact_id!r} revision {revision}")
        return self._version(row), self.blobs.get(row["content_sha256"]).decode("utf-8")

    # paths

    def resolve_inside(self, rel: str | Path, root: Path) -> Path:
        return resolve_inside(rel, root)

    def write_project_file(self, rel: str | Path, content: str) -> Path:
        target = resolve_inside(rel, self.project_root)
        parts = target.relative_to(self.project_root.resolve()).parts
        if not parts or parts[0].lower() in PROTECTED_DIRS:
            raise BrokerError("ACCESS_DENIED", f"{str(rel)!r} is a protected location")
        atomic_write(target, content.encode("utf-8"))
        return target
