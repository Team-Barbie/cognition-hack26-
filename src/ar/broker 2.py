"""Minimal managed file and artifact broker for the fixture research run."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ar.db import connect
from ar.models import ArtifactVersion, LineSelector, SourceRef
from ar.packet import DeliveredRead, DependencyItem


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Broker:
    def __init__(self, database: Path, project_root: Path) -> None:
        self.database = database
        self.root = project_root.resolve()
        self.blobs = self.root / ".research" / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        connection = connect(self.database)
        try:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sources (
                        source_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        relative_path TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        blob_path TEXT NOT NULL,
                        line_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (source_id, revision)
                    );
                    CREATE TABLE IF NOT EXISTS read_receipts (
                        id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        source_revision INTEGER NOT NULL,
                        representation_sha256 TEXT NOT NULL,
                        line_start INTEGER NOT NULL,
                        line_end INTEGER NOT NULL,
                        task_id TEXT NOT NULL,
                        attempt_id TEXT NOT NULL,
                        packet_id TEXT NOT NULL,
                        delivered_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        artifact_type TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        blob_path TEXT NOT NULL,
                        producing_task TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, revision)
                    );
                    """
                )
        finally:
            connection.close()

    def snapshot(self, path: Path, source_id: str) -> SourceRef:
        """Copy one authorized input into the immutable blob store."""

        resolved = path.resolve()
        input_root = (self.root / "input").resolve()
        if not resolved.is_relative_to(input_root):
            raise PermissionError(f"source is outside project input: {path}")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        data = resolved.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        blob = self.blobs / digest
        if not blob.exists():
            blob.write_bytes(data)
        connection = connect(self.database)
        try:
            latest = connection.execute(
                """SELECT revision, content_sha256 FROM sources
                   WHERE source_id = ? ORDER BY revision DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
            if latest and latest["content_sha256"] == digest:
                return SourceRef(source_id=source_id, revision=latest["revision"])
            revision = latest["revision"] + 1 if latest else 1
            text = data.decode("utf-8")
            with connection:
                connection.execute(
                    """INSERT INTO sources
                       (source_id, revision, relative_path, content_sha256, blob_path,
                        line_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        revision,
                        str(resolved.relative_to(self.root)),
                        digest,
                        str(blob),
                        len(text.splitlines()),
                        _now(),
                    ),
                )
            return SourceRef(source_id=source_id, revision=revision)
        finally:
            connection.close()

    def read_all(
        self,
        source_ref: SourceRef,
        *,
        task_id: str,
        attempt_id: str,
        packet_id: str,
        label: str,
    ) -> DeliveredRead:
        """Deliver an exact snapshotted document and issue a bound receipt."""

        connection = connect(self.database)
        try:
            source = connection.execute(
                "SELECT * FROM sources WHERE source_id = ? AND revision = ?",
                (source_ref.source_id, source_ref.revision),
            ).fetchone()
            if source is None:
                raise KeyError(f"unknown source {source_ref.source_id}@{source_ref.revision}")
            content = Path(source["blob_path"]).read_text(encoding="utf-8")
            selector = LineSelector(start=1, end=max(1, source["line_count"]))
            receipt_id = f"read-{uuid4()}"
            with connection:
                connection.execute(
                    """INSERT INTO read_receipts
                       (id, source_id, source_revision, representation_sha256,
                        line_start, line_end, task_id, attempt_id, packet_id, delivered_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        receipt_id,
                        source_ref.source_id,
                        source_ref.revision,
                        source["content_sha256"],
                        selector.start,
                        selector.end,
                        task_id,
                        attempt_id,
                        packet_id,
                        _now(),
                    ),
                )
            return DeliveredRead(
                source_ref=source_ref,
                selector=selector,
                content=content,
                read_receipt_id=receipt_id,
                label=label,
            )
        finally:
            connection.close()

    def create_report(self, content: str, producing_task: str) -> ArtifactVersion:
        """Commit an immutable report, reusing the version after resume."""

        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = "report"
        connection = connect(self.database)
        try:
            existing = connection.execute(
                """SELECT * FROM artifacts WHERE artifact_id = ? AND producing_task = ?
                   AND content_sha256 = ?""",
                (artifact_id, producing_task, digest),
            ).fetchone()
            if existing:
                return ArtifactVersion(
                    artifact_id=artifact_id,
                    revision=existing["revision"],
                    artifact_type=existing["artifact_type"],
                    content_type=existing["content_type"],
                    content_sha256=digest,
                    producing_task=producing_task,
                )
            latest = connection.execute(
                "SELECT MAX(revision) AS revision FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()["revision"]
            revision = (latest or 0) + 1
            blob = self.blobs / digest
            if not blob.exists():
                blob.write_bytes(data)
            with connection:
                connection.execute(
                    """INSERT INTO artifacts
                       (artifact_id, revision, artifact_type, content_type, content_sha256,
                        blob_path, producing_task, created_at)
                       VALUES (?, ?, 'analysis_report', 'text/markdown', ?, ?, ?, ?)""",
                    (artifact_id, revision, digest, str(blob), producing_task, _now()),
                )
            return ArtifactVersion(
                artifact_id=artifact_id,
                revision=revision,
                artifact_type="analysis_report",
                content_sha256=digest,
                producing_task=producing_task,
            )
        finally:
            connection.close()

    @staticmethod
    def artifact_dependency(artifact: ArtifactVersion, content: str) -> DependencyItem:
        return DependencyItem(
            kind="artifact", id=artifact.artifact_id, revision=artifact.revision, content=content
        )
