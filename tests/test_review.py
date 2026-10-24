import sqlite3

import pytest

from ar import review
from ar.models import ArtifactVersion, ReviewFinding, ReviewResult
from ar.review import GateDecision


def conn_() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    review.ensure_schema(conn)
    return conn


def art(revision: int = 1) -> ArtifactVersion:
    return ArtifactVersion(artifact_id="art-1", revision=revision, artifact_type="report",
                           content_sha256="0" * 64, producing_task="t-writer")


def result(*findings: ReviewFinding, disposition="accept", revision: int = 1, artifact_id="art-1"):
    return ReviewResult(reviewed_artifact_id=artifact_id, reviewed_revision=revision,
                        findings=list(findings), disposition=disposition)


BLOCK = ReviewFinding(severity="blocking", message="Does not cite claim-2.", claim_id="claim-2")
NOTE = ReviewFinding(severity="note", message="Fine.")


def test_accept():
    conn = conn_()
    res = review.gate(conn, result(NOTE), artifact=art())
    assert res.decision is GateDecision.accept and res.limitations == []
    assert len(review.list_findings(conn, "art-1")) == 1


def test_blocking_needs_revision_first_time():
    conn = conn_()
    res = review.gate(conn, result(BLOCK, disposition="revise"), artifact=art())
    assert res.decision is GateDecision.needs_revision
    assert res.feedback == [BLOCK.message]
    row = review.list_findings(conn, "art-1", 1)[0]
    assert (row["severity"], row["claim_id"], row["resolved"]) == ("blocking", "claim-2", 0)


def test_second_pass_accepts_with_limitations():
    conn = conn_()
    review.gate(conn, result(BLOCK, disposition="revise"), artifact=art())
    res = review.gate(conn, result(BLOCK, disposition="revise", revision=2), artifact=art(2), revision_pass=1)
    assert res.decision is GateDecision.accept_with_limitations
    assert res.limitations == [BLOCK.message]


def test_blocking_cannot_be_accepted_by_disposition_alone():
    res = review.gate(conn_(), result(BLOCK, disposition="accept"), artifact=art())
    assert res.decision is GateDecision.needs_revision


def test_revise_without_findings_still_blocks():
    res = review.gate(conn_(), result(disposition="revise"), artifact=art(), revision_pass=1)
    assert res.decision is GateDecision.accept_with_limitations and res.limitations


def test_clean_revision_resolves_earlier_findings():
    conn = conn_()
    review.gate(conn, result(BLOCK, disposition="revise"), artifact=art())
    review.gate(conn, result(NOTE, revision=2), artifact=art(2), revision_pass=1)
    assert [r["resolved"] for r in review.list_findings(conn, "art-1", 1)] == [1]


@pytest.mark.parametrize("kw", [{"revision": 1}, {"artifact_id": "art-other", "revision": 2}])
def test_stale_review_rejected(kw):
    conn = conn_()
    with pytest.raises(ValueError, match="stale review"):
        review.gate(conn, result(NOTE, **kw), artifact=art(2))
    assert review.list_findings(conn, "art-1") == []
