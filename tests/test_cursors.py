import base64
import json
import sqlite3

import pytest

from ar.broker import Broker, BrokerError, ensure_schema
from ar.models import SourceRef

LINES = [f"line {i:02d} " + "x" * 20 for i in range(1, 21)]  # 29 bytes each


@pytest.fixture
def broker(tmp_path):
    root = tmp_path / "project"
    (root / "input" / "papers").mkdir(parents=True)
    (root / ".research").mkdir()
    (root / "input" / "papers" / "big.md").write_text("\n".join(LINES))
    (root / "input" / "papers" / "other.md").write_text("\n".join(LINES))
    conn = sqlite3.connect(root / ".research" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    b = Broker(conn, root)
    b.big = b.register_source("big", root / "input" / "papers" / "big.md")
    b.other = b.register_source("other", root / "input" / "papers" / "other.md")
    return b


def page(b, ref=None, **kw):
    return b.read_page(task_id="t1", attempt_id="a1", packet_id="p1", ref=ref or b.big, **kw)


def mismatch(b, **kw):
    with pytest.raises(BrokerError) as err:
        page(b, **kw)
    assert err.value.code == "CURSOR_MISMATCH"


def test_small_read_is_full(broker):
    p = page(broker)
    assert (p.coverage, p.truncated, p.next_cursor) == ("full", False, None)
    assert (p.delivered.selector.start, p.delivered.selector.end) == (1, 20)


def test_large_read_is_truncated_at_a_line_boundary(broker):
    p = page(broker, max_bytes=100)  # three 29-byte lines plus separators fit
    assert (p.delivered.selector.start, p.delivered.selector.end) == (1, 3)
    assert p.delivered.content == "\n".join(LINES[:3])
    assert (p.coverage, p.truncated) == ("partial", True)
    assert p.next_cursor


def test_at_least_one_line_is_delivered(broker):
    p = page(broker, max_bytes=1)
    assert p.delivered.selector.end == 1
    assert p.delivered.content == LINES[0]


def test_pages_walk_the_whole_source_and_open_the_gate_last(broker):
    def gate():
        broker.check_required_reads(task_id="t1", attempt_id="a1", required=[broker.big])

    with pytest.raises(BrokerError) as err:
        gate()
    assert err.value.code == "REQUIRED_READ_INCOMPLETE"
    cursor, spans = None, []
    while True:
        p = page(broker, max_bytes=100, cursor=cursor)
        spans.append((p.delivered.selector.start, p.delivered.selector.end))
        if not p.truncated:
            break
        with pytest.raises(BrokerError) as err:
            gate()
        assert err.value.details["missing"] == [[spans[-1][1] + 1, 20]]
        cursor = p.next_cursor
    assert spans[0][0] == 1 and spans[-1][1] == 20
    assert all(b[0] == a[1] + 1 for a, b in zip(spans, spans[1:]))
    gate()
    receipts = broker.receipts_for("t1", "a1")
    assert [(r.selector.start, r.selector.end) for r in receipts] == spans
    assert len({r.id for r in receipts}) == len(spans)


def test_cursor_respects_original_range(broker):
    first = page(broker, start=5, end=12, max_bytes=100)
    second = page(broker, start=5, end=12, max_bytes=1000, cursor=first.next_cursor)
    assert (second.delivered.selector.start, second.delivered.selector.end) == (8, 12)
    assert (second.coverage, second.truncated) == ("partial", False)


def test_cursor_for_another_source_is_rejected(broker):
    mismatch(broker, ref=broker.other, cursor=page(broker, max_bytes=100).next_cursor)


def test_cursor_for_another_selector_is_rejected(broker):
    cursor = page(broker, max_bytes=100).next_cursor
    mismatch(broker, start=2, cursor=cursor)
    mismatch(broker, end=10, cursor=cursor)


def test_cursor_reused_against_another_revision_is_rejected(broker):
    cursor = page(broker, max_bytes=100).next_cursor
    path = broker.project_root / "input" / "papers" / "big.md"
    path.write_text("\n".join(LINES + ["a new final line"]))
    rev2 = broker.register_source("big", path)
    assert rev2.revision == 2
    mismatch(broker, ref=rev2, cursor=cursor)
    with pytest.raises(BrokerError) as err:
        page(broker, ref=SourceRef(source_id="big", revision=1), cursor=cursor)
    assert err.value.code == "STALE_REVISION"
    assert len(broker.receipts_for("t1", "a1")) == 1  # only the first page was ever delivered


def test_tampered_or_forged_cursors_are_rejected(broker):
    cursor = page(broker, max_bytes=100).next_cursor
    body, _, mac = cursor.partition(".")
    fields = json.loads(base64.urlsafe_b64decode(body))
    fields[5] = 15
    forged = base64.urlsafe_b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()
    for bad in [f"{forged}.{mac}", body + ".00", "garbage", ""]:
        mismatch(broker, cursor=bad)


def test_cursor_survives_a_new_broker_on_the_same_database(broker):
    cursor = page(broker, max_bytes=100).next_cursor
    again = Broker(broker.conn, broker.project_root)
    p = again.read_page(
        task_id="t1", attempt_id="a1", packet_id="p1", ref=broker.big, max_bytes=100, cursor=cursor
    )
    assert p.delivered.selector.start == 4
