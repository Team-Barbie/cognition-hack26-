"""Claim acceptance without a model: a stub verifier stands in for the broker."""

import sqlite3

from ar import evidence
from ar.agents.fake import load_answer_key
from ar.models import ClaimDraft, EvidenceLink, LineSelector, SourceRef

KEY = load_answer_key()


class StubVerifier:
    """Rejects any quote not in `known`, as the broker does for a quote outside the cited lines."""

    def __init__(self, known: set[str] | None = None):
        self.known = known
        self.calls: list[tuple[str, str]] = []

    def verify_evidence_link(self, link, *, task_id, attempt_id):
        self.calls.append((task_id, attempt_id))
        if self.known is not None and link.quote not in self.known:
            raise ValueError(f"quote not at cited lines {link.locator.start}-{link.locator.end}")


def conn_() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    evidence.ensure_schema(conn)
    return conn


def draft(source: str, **kw) -> ClaimDraft:
    info = KEY["sources"][source]
    link = EvidenceLink(
        relation=info["relation"], source_ref=SourceRef(source_id=source),
        locator=LineSelector(**info["locator"]), quote=info["quote"], read_receipt_id=f"read-{source}",
    )
    return ClaimDraft(statement=info["statement"], evidence_links=[link], **kw)


def test_accepts_good_claims():
    conn = conn_()
    v = StubVerifier({KEY["sources"][s]["quote"] for s in ("paper-a", "paper-b")})
    res = evidence.accept_claims(conn, v, task_id="t1", attempt_id="a1",
                                 drafts=[draft("paper-a"), draft("paper-b")])
    assert [c.id for c in res.accepted] == ["claim-1", "claim-2"]
    assert not res.rejected
    stored = evidence.get_claim(conn, "claim-2")
    assert stored.origin_task == "t1"
    assert stored.evidence_links[0].relation == "contradicts"
    assert stored.evidence_links[0].quote == KEY["sources"]["paper-b"]["quote"]
    assert [c.id for c in evidence.list_claims(conn)] == ["claim-1", "claim-2"]
    assert v.calls == [("t1", "a1")] * 2


def test_rejected_link_rejects_claim_whole_and_stores_nothing():
    conn = conn_()
    bad = draft("paper-a").model_copy(update={"statement": "forged"})
    bad.evidence_links[0] = bad.evidence_links[0].model_copy(update={"quote": "never said"})
    good = draft("paper-b")
    v = StubVerifier({KEY["sources"]["paper-b"]["quote"]})
    res = evidence.accept_claims(conn, v, task_id="t1", attempt_id="a1", drafts=[bad, good])
    assert [r.draft.statement for r in res.rejected] == ["forged"]
    assert "quote not at cited lines" in res.rejected[0].reason
    assert [c.statement for c in evidence.list_claims(conn)] == [good.statement]
    assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 1
    assert res.accepted[0].id == "claim-1"  # a rejected claim burns no id


def test_one_bad_link_rejects_a_multi_link_claim():
    conn = conn_()
    d = draft("paper-a")
    d.evidence_links.append(d.evidence_links[0].model_copy(update={"quote": "bogus"}))
    res = evidence.accept_claims(conn, StubVerifier({d.evidence_links[0].quote}),
                                 task_id="t1", attempt_id="a1", drafts=[d])
    assert len(res.rejected) == 1 and not evidence.list_claims(conn)


def test_linkless_source_reported_rejected():
    conn = conn_()
    d = ClaimDraft(statement="Trust me.", epistemic_status="source_reported")
    res = evidence.accept_claims(conn, StubVerifier(), task_id="t1", attempt_id="a1", drafts=[d])
    assert res.rejected and "no evidence links" in res.rejected[0].reason
    assert evidence.list_claims(conn) == []


def test_linkless_hypothesized_and_inferred_accepted():
    conn = conn_()
    drafts = [ClaimDraft(statement="Maybe.", epistemic_status="hypothesized"),
              ClaimDraft(statement="So.", epistemic_status="inferred", derivation="from claim-1")]
    res = evidence.accept_claims(conn, StubVerifier(), task_id="t1", attempt_id="a1", drafts=drafts)
    assert len(res.accepted) == 2 and not res.rejected
    assert evidence.get_claim(conn, "claim-2").derivation == "from claim-1"


def test_ids_sequential_across_calls():
    conn = conn_()
    v = StubVerifier()
    evidence.accept_claims(conn, v, task_id="t1", attempt_id="a1", drafts=[draft("paper-a")])
    res = evidence.accept_claims(conn, v, task_id="t2", attempt_id="a1", drafts=[draft("paper-b")])
    assert res.accepted[0].id == "claim-2"
    assert evidence.get_claim(conn, "claim-99") is None


def test_inspect_claim_has_required_fields():
    conn = conn_()
    d = draft("paper-b", limitations=["Short series only."])
    evidence.accept_claims(conn, StubVerifier(), task_id="task-reader-b", attempt_id="att-7", drafts=[d])
    text = evidence.inspect_claim(conn, "claim-1")
    info = KEY["sources"]["paper-b"]
    for needle in (info["statement"], "paper-b", "r1", "lines 7-7", info["quote"], "read-paper-b",
                   "task-reader-b", "att-7", "contradicts", "Short series only."):
        assert needle in text
    assert "No such claim" in evidence.inspect_claim(conn, "claim-9")
