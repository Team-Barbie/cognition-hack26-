import sqlite3

import pytest

from ar.broker import Broker, BrokerError, ensure_schema
from ar.models import EvidenceLink, LineSelector

TEXT = "\n".join(f"fact {i}: alpha {i}" for i in range(1, 11))


@pytest.fixture
def broker(tmp_path):
    root = tmp_path / "project"
    (root / "input" / "papers").mkdir(parents=True)
    (root / ".research").mkdir()
    (root / "input" / "papers" / "p.md").write_text(TEXT)
    conn = sqlite3.connect(root / ".research" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    b = Broker(conn, root)
    b.ref = b.register_source("p", root / "input" / "papers" / "p.md")
    return b


def stage(b, **kw):
    return b.stage_read(task_id="t1", attempt_id="a1", packet_id="p1", ref=b.ref, **kw)


def gate(b, attempt="a1"):
    b.check_required_reads(task_id="t1", attempt_id=attempt, required=[b.ref])


def test_staging_issues_no_receipt(broker):
    staged = stage(broker)
    assert staged.lines[0] == "fact 1: alpha 1" and len(staged.lines) == 10
    assert broker.receipts_for("t1", "a1") == []
    with pytest.raises(BrokerError) as err:
        gate(broker)
    assert err.value.code == "REQUIRED_READ_INCOMPLETE"


def test_deliver_all_issues_one_receipt_and_opens_the_gate(broker):
    d = broker.deliver(stage(broker))
    assert (d.selector.start, d.selector.end) == (1, 10)
    assert d.content == TEXT
    gate(broker)
    assert len(broker.receipts_for("t1", "a1")) == 1


def test_only_the_included_lines_earn_a_receipt(broker):
    d = broker.deliver(stage(broker), (1, 6))  # lines 7-10 were dropped by packing
    assert d.content == "\n".join(TEXT.splitlines()[:6])
    assert (d.selector.start, d.selector.end) == (1, 6)
    with pytest.raises(BrokerError) as err:
        gate(broker)
    assert err.value.details["missing"] == [[7, 10]]


def test_included_lines_must_be_within_what_was_staged(broker):
    staged = stage(broker, start=3, end=5)
    for span in [(1, 4), (4, 9), (5, 4)]:
        with pytest.raises(BrokerError) as err:
            broker.deliver(staged, span)
        assert err.value.code == "INVALID_SELECTOR"
    assert broker.receipts_for("t1", "a1") == []
    d = broker.deliver(staged, (4, 5))
    assert d.content == "fact 4: alpha 4\nfact 5: alpha 5"


def test_omitted_content_cannot_back_evidence(broker):
    d = broker.deliver(stage(broker), (1, 3))
    ok = EvidenceLink(
        relation="supports", source_ref=broker.ref, locator=LineSelector(start=2, end=2),
        quote="alpha 2", read_receipt_id=d.read_receipt_id,
    )
    broker.verify_evidence_link(ok, task_id="t1", attempt_id="a1")
    bad = ok.model_copy(update={"locator": LineSelector(start=8, end=8), "quote": "alpha 8"})
    with pytest.raises(BrokerError) as err:
        broker.verify_evidence_link(bad, task_id="t1", attempt_id="a1")
    assert err.value.code == "EVIDENCE_REJECTED"


def test_read_is_still_stage_plus_full_delivery(broker):
    d = broker.read(task_id="t1", attempt_id="a1", packet_id="p1", ref=broker.ref)
    assert d.content == TEXT
    gate(broker)


# resume


def test_link_receipts_credits_only_included_prior_content(broker):
    first = broker.deliver(stage(broker, start=1, end=6))
    broker.deliver(stage(broker, start=7, end=10))

    linked = broker.link_receipts("t1", "a2", "p2", "a1", {first.read_receipt_id: (2, 4)})
    assert len(linked) == 1
    assert (linked[0].attempt_id, linked[0].packet_id) == ("a2", "p2")
    assert (linked[0].selector.start, linked[0].selector.end) == (2, 4)
    assert linked[0].id != first.read_receipt_id
    with pytest.raises(BrokerError) as err:
        gate(broker, "a2")
    assert err.value.details["missing"] == [[1, 1], [5, 10]]
    assert len(broker.receipts_for("t1", "a1")) == 2  # the old attempt is untouched

    rows = broker.conn.execute("SELECT * FROM receipt_links").fetchall()
    assert [(r["receipt_id"], r["from_receipt_id"]) for r in rows] == [
        (linked[0].id, first.read_receipt_id)
    ]


def test_link_receipts_accepts_bare_ids_and_can_complete_coverage(broker):
    ids = [
        broker.deliver(stage(broker, start=s, end=e)).read_receipt_id for s, e in [(1, 6), (7, 10)]
    ]
    broker.link_receipts("t1", "a2", "p2", "a1", ids)
    gate(broker, "a2")


def test_link_receipts_rejects_foreign_or_overreaching_links(broker):
    mine = broker.deliver(stage(broker, start=1, end=4))
    other = broker.read(task_id="t9", attempt_id="a1", packet_id="p1", ref=broker.ref)
    cases = [
        ([other.read_receipt_id], "ACCESS_DENIED"),
        ({mine.read_receipt_id: (1, 9)}, "INVALID_SELECTOR"),
        (["read-9999"], "ACCESS_DENIED"),
    ]
    for included, code in cases:
        with pytest.raises(BrokerError) as err:
            broker.link_receipts("t1", "a2", "p2", "a1", included)
        assert err.value.code == code
    assert broker.receipts_for("t1", "a2") == []


def test_link_receipts_refuses_content_from_a_replaced_snapshot(broker):
    first = broker.deliver(stage(broker))
    path = broker.project_root / "input" / "papers" / "p.md"
    path.write_text(TEXT + "\nfact 11: new")
    broker.register_source("p", path)
    with pytest.raises(BrokerError) as err:
        broker.link_receipts("t1", "a2", "p2", "a1", [first.read_receipt_id])
    assert err.value.code == "STALE_REVISION"
    assert broker.receipts_for("t1", "a2") == []
