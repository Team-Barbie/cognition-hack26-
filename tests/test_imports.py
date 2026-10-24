import sqlite3

import pytest
from pydantic import ValidationError

from ar import broker as broker_mod
from ar import imports
from ar.imports import ImportRecord, Provenance, register_import, reject_self_confirmation
from ar.models import SourceRef
from ar.snapshots import BlobStore, BrokerError, sha256_bytes

LOG = "$ python train.py --seed 1\nepoch 3 loss 0.12\nexit status 0\nALL CHECKS PASSED\n"


@pytest.fixture
def env(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    broker_mod.ensure_schema(conn)
    return tmp_path, conn, broker_mod.Broker(conn, tmp_path)


def write(root, name, text):
    path = root / "host" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(text.encode())
    return path


def test_successful_looking_log_is_host_reported(env):
    root, conn, broker = env
    path = write(root, "run.log", LOG)
    rec = register_import(conn, broker, path=path, kind="experiment_log", executor="host-assistant",
                          command="python train.py --seed 1")
    assert rec.provenance == "host_reported" == Provenance.host_reported
    assert rec.provenance != Provenance.runtime_captured
    assert "did not enforce the original execution permissions" in rec.assurance
    assert rec.content_sha256 == sha256_bytes(LOG.encode())
    assert (rec.id, rec.source_id, rec.executor, rec.command) == (
        "import-1", "import-1", "host-assistant", "python train.py --seed 1")
    assert broker.blobs.get(rec.content_sha256) == LOG.encode()
    assert broker.source_line_count(SourceRef(source_id="import-1")) == 4
    assert imports.get_import(conn, "import-1") == rec
    assert imports.import_for_source(conn, "import-1") == rec
    assert imports.list_imports(conn) == [rec]


def test_import_can_never_be_runtime_captured():
    base = dict(id="i", kind="log", path="p", content_sha256="0" * 64, executor="x",
                imported_at="2026-10-24T00:00:00+00:00", assurance="a")
    with pytest.raises(ValidationError):
        ImportRecord(provenance="runtime_captured", **base)
    with pytest.raises(ValidationError):
        ImportRecord(provenance="agent_authored", **base)
    assert {p.value for p in Provenance} == {"runtime_captured", "host_reported", "agent_authored", "derived"}


def test_blob_store_only_snapshots_bytes(env):
    root, conn, _ = env
    rec = register_import(conn, BlobStore(root), path=write(root, "a.log", LOG), kind="experiment_log",
                          executor="host", url="https://example.org/run/1")
    assert rec.source_id is None and rec.url == "https://example.org/run/1"
    assert BlobStore(root).get(rec.content_sha256) == LOG.encode()


def test_reimport_is_idempotent_and_new_executor_is_new_record(env):
    root, conn, broker = env
    path = write(root, "run.log", LOG)
    first = register_import(conn, broker, path=path, kind="experiment_log", executor="host")
    assert register_import(conn, broker, path=path, kind="experiment_log", executor="host") == first
    other = register_import(conn, broker, path=path, kind="experiment_log", executor="someone-else")
    assert other.id == "import-2"


def test_missing_file_is_source_not_found(env):
    root, conn, broker = env
    with pytest.raises(BrokerError) as exc:
        register_import(conn, broker, path=root / "nope.log", kind="log", executor="host")
    assert exc.value.code == "SOURCE_NOT_FOUND"


def test_generated_artifact_cannot_be_independent_confirmation(env):
    root, conn, broker = env
    art = broker.create_artifact(task_id="t-w", attempt_id="a1", artifact_type="report",
                                 content="# Report\nWe agree.\n")
    path = write(root, "copy.md", "# Report\nWe agree.\n")
    with pytest.raises(BrokerError) as exc:
        register_import(conn, broker, path=path, kind="external_confirmation", executor="host",
                        claimed_independent=True)
    assert exc.value.code == "SELF_CONFIRMATION_REJECTED"
    assert exc.value.details["provenance"] == "derived" and art.artifact_id in exc.value.details["origin"]
    assert imports.list_imports(conn) == []
    rec = register_import(conn, broker, path=path, kind="report_copy", executor="host")
    assert rec.provenance == "derived" and "not independent" in rec.assurance


def test_exported_output_is_derived(env):
    root, conn, broker = env
    out = root / "output" / "report.md"
    out.parent.mkdir()
    out.write_text("# exported\n", encoding="utf-8")
    with pytest.raises(BrokerError) as exc:
        register_import(conn, broker, path=out, kind="confirmation", executor="host", claimed_independent=True)
    assert exc.value.code == "SELF_CONFIRMATION_REJECTED"


def test_reject_self_confirmation_ignores_foreign_bytes(env):
    _, conn, _ = env
    assert reject_self_confirmation(conn, content_sha256=sha256_bytes(b"external")) is None
