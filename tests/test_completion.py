import sqlite3

from ar import evidence, review
from ar.completion import assess
from ar.models import ArtifactVersion, ClaimDraft, Requirement, ReviewResult

REPORT = """# Report

## Supported baseline

- Seasonal naive is supported. [claim:claim-1]

## Contradictions

- paper-b contradicts the exclusion rule. [claim:claim-2]
"""
ART = ArtifactVersion(artifact_id="art-1", revision=1, artifact_type="report",
                      content_sha256="0" * 64, producing_task="t-writer")


class AcceptAll:
    def verify_evidence_link(self, link, *, task_id, attempt_id):
        return None


def req(rid: str, text: str = "baseline") -> Requirement:
    return Requirement(id=rid, source_passage=text, interpretation=text)


def conn_(n_claims: int = 2, reviewed: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    evidence.ensure_schema(conn)
    review.ensure_schema(conn)
    drafts = [ClaimDraft(statement=f"c{i}", epistemic_status="hypothesized") for i in range(n_claims)]
    evidence.accept_claims(conn, AcceptAll(), task_id="t", attempt_id="a", drafts=drafts)
    if reviewed:
        review.gate(conn, ReviewResult(reviewed_artifact_id="art-1", reviewed_revision=1, disposition="accept"),
                    artifact=ART, reviewer_task="t-reviewer")
    return conn


COVERAGE = {"req-1": "Supported baseline", "req-2": "Contradictions"}


def run(conn=None, *, reqs=None, content=REPORT, coverage=COVERAGE, limitations=()):
    return assess(conn or conn_(), requirements=reqs if reqs is not None else [req("req-1"), req("req-2")],
                  artifact=ART, content=content, coverage=coverage, limitations=list(limitations))


def test_complete_case():
    a = run(limitations=["Sources are not exhaustive."])
    assert a.complete and a.problems == []
    assert [(r.requirement_id, r.section, r.covered) for r in a.rows] == [
        ("req-1", "Supported baseline", True), ("req-2", "Contradictions", True)]
    assert a.limitations == ["Sources are not exhaustive."]


def test_heading_markers_in_coverage_are_tolerated():
    assert run(coverage={"req-1": "## supported baseline", "req-2": "Contradictions"}).complete


def test_unknown_citation_not_complete_and_listed():
    a = run(content=REPORT + "\n- Invented. [claim:claim-77]\n")
    assert not a.complete
    assert a.unresolved_citations == ["claim-77"]
    assert any("claim-77" in p for p in a.problems)


def test_missing_requirement_coverage():
    a = run(coverage={"req-1": "Supported baseline"})
    assert not a.complete
    assert [r.covered for r in a.rows] == [True, False]
    assert any("req-2" in p for p in a.problems)


def test_section_absent_from_content():
    a = run(coverage={"req-1": "Supported baseline", "req-2": "Nonexistent"})
    assert not a.complete and a.rows[1].covered is False


def test_empty_deliverable():
    a = run(content="  \n", coverage={})
    assert not a.complete and any("empty" in p for p in a.problems)


def test_no_experiment_brief_completes():
    reqs = [req("req-1", "Question: is seasonal naive adequate?"), req("req-2", "Constraint: exclude short series")]
    a = run(reqs=reqs)
    assert a.complete
    assert len(a.rows) == 2 and not any("experiment" in r.requirement_id for r in a.rows)
