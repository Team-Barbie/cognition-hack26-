import hashlib
import os
from pathlib import Path

import pytest

from ar.broker import BrokerError
from ar.snapshots import BlobStore, resolve_inside, sha256_bytes, sha256_text


def test_blob_store_is_content_addressed_and_idempotent(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"hello")
    assert digest == hashlib.sha256(b"hello").hexdigest() == sha256_bytes(b"hello")
    assert sha256_text("hello") == digest
    assert store.put(b"hello") == digest
    assert store.get(digest) == b"hello"
    assert (tmp_path / ".research" / "blobs" / digest).read_bytes() == b"hello"
    assert [p.name for p in store.root.iterdir()] == [digest]  # no temp files left


def test_blob_get_rejects_unknown_and_malformed_ids(tmp_path):
    store = BlobStore(tmp_path)
    with pytest.raises(BrokerError) as exc:
        store.get("0" * 64)
    assert exc.value.code == "SOURCE_NOT_FOUND"
    with pytest.raises(BrokerError):
        store.get("../../etc/passwd")


def test_blob_get_detects_tampering(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"original")
    (store.root / digest).write_bytes(b"tampered")
    with pytest.raises(BrokerError) as exc:
        store.get(digest)
    assert exc.value.code == "BLOB_CORRUPT"


def test_resolve_inside_allows_nested_and_absolute_inside(tmp_path):
    assert resolve_inside("output/report.md", tmp_path) == (tmp_path / "output" / "report.md").resolve()
    inside = tmp_path / "output" / "x.md"
    assert resolve_inside(inside, tmp_path) == inside.resolve()


def test_resolve_inside_rejects_traversal(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for rel in ("../escape.md", "output/../../escape.md", "output/../x.md"):
        with pytest.raises(BrokerError) as exc:
            resolve_inside(rel, root)
        assert exc.value.code == "ACCESS_DENIED"


def test_resolve_inside_rejects_absolute_outside(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "other" / "x.md"
    with pytest.raises(BrokerError) as exc:
        resolve_inside(outside, root)
    assert exc.value.code == "ACCESS_DENIED"
    with pytest.raises(BrokerError):
        resolve_inside(str(Path(root.anchor) / "windows-or-etc" / "x"), root)


def test_resolve_inside_rejects_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(BrokerError) as exc:
        resolve_inside("link/secret.md", root)
    assert exc.value.code == "ACCESS_DENIED"
