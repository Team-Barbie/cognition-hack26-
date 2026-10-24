"""The broker: snapshotted sources, read receipts, evidence checks, immutable artifacts."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ar.models import (
    ArtifactVersion,
    EvidenceLink,
    LineSelector,
    ReadReceipt,
    SourceRef,
)
from ar.packet import DeliveredRead
from ar.snapshots import BlobStore, BrokerError, atomic_write, resolve_inside

__all__ = ["QUOTE_NORMALIZATION", "Broker", "BrokerError", "ensure_schema", "normalize_quote"]

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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)


def normalize_quote(text: str) -> str:
    """`ws-collapse-v1`: join `splitlines()` with newlines, collapse whitespace runs, trim."""
    return re.sub(r"\s+", " ", "\n".join(text.splitlines())).strip()


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


class Broker:
    def __init__(self, conn: sqlite3.Connection, project_root: Path) -> None:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        self.conn = conn
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
    ) -> DeliveredRead:
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
        with _write_txn(self.conn):
            seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM read_receipts").fetchone()[0]
            receipt_id = f"read-{seq:04d}"
            self.conn.execute(
                "INSERT INTO read_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq, receipt_id, ref.source_id, ref.revision, row["representation_sha256"],
                    start, end, task_id, attempt_id, packet_id, _now(),
                ),
            )
        return DeliveredRead(
            source_ref=ref,
            selector=LineSelector(start=start, end=end),
            content="\n".join(lines[start - 1 : end]),
            read_receipt_id=receipt_id,
            label=label,
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
    ) -> ArtifactVersion:
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
    ) -> ArtifactVersion:
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
