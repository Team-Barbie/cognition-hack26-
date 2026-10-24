"""Quality behaviour against the real broker: precise rejections, retries, runtime review checks,
completion gates, and an end-to-end fake run on the baselines fixture."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from ar import agents, broker as broker_mod, evidence, review
from ar.agents.fake import FakeAgent
from ar.answer_check import check_answer_key
from ar.broker import Broker
from ar.completion import assess
from ar.evidence import accept_claims, inspect_claim, list_rejections, source_lines_from
from ar.export import ExportError, export_run
from ar.models import (
    ArtifactVersion, ClaimDraft, EvidenceLink, LineSelector, Requirement, ReviewFinding, ReviewResult, SourceRef,
)
from ar.packet import DependencyItem, Packet
from ar.review import GateDecision

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"
KEY = json.loads((FIXTURE / "answer_key.json").read_text())
PAPERS = ["paper-a", "paper-b", "paper-c"]
B_QUOTE = KEY["sources"]["paper-b"]["quote"]
REQS = [
    Requirement(id="req-1", source_passage="Question", interpretation="State which baseline is supported"),
    Requirement(id="req-2", source_passage="Constraint", interpretation="Say whether the exclusion rule holds"),
]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(FIXTURE / "input", root / "input")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for mod in (broker_mod, evidence, review):
        mod.ensure_schema(conn)
    b = Broker(conn, root)
    for p in PAPERS:
        b.register_source(p, root / "input" / "papers" / f"{p}.md")
    return root, conn, b


def _link(rid: str, start: int, end: int, quote: str, source="paper-b", relation="contradicts") -> EvidenceLink:
    return EvidenceLink(relation=relation, source_ref=SourceRef(source_id=source),
                        locator=LineSelector(start=start, end=end), quote=quote, read_receipt_id=rid)


def _accept(conn, b, drafts, task="t-b", attempt="a1"):
    return accept_claims(conn, b, task_id=task, attempt_id=attempt, drafts=drafts, source_lines=source_lines_from(b))


def _read(b, task="t-b", attempt="a1", source="paper-b", **kw):
    return b.read(task_id=task, attempt_id=attempt, packet_id=f"p-{task}", ref=SourceRef(source_id=source), **kw)


# evidence


def test_misplaced_quote_is_rejected_with_where_it_really_is(project):
    _, conn, b = project
    r = _read(b)
    res = _accept(conn, b, [ClaimDraft(statement="s", evidence_links=[_link(r.read_receipt_id, 5, 6, B_QUOTE)])])
    [rej] = res.rejected
    assert (rej.code, rej.link_index, rej.id) == ("QUOTE_NOT_AT_LOCATOR", 0, "rej-1")
    assert rej.hint == "The quote appears at lines 7-7 of paper-b."


def test_invented_quote_says_it_is_nowhere(project):
    _, conn, b = project
    r = _read(b)
    res = _accept(conn, b, [ClaimDraft(statement="s", evidence_links=[_link(r.read_receipt_id, 7, 7, "Seasonal naive wins everywhere.")])])
    assert res.rejected[0].hint == "The quote does not appear anywhere in paper-b."


@pytest.mark.parametrize("case, code", [
    ("foreign", "RECEIPT_NOT_TRANSFERABLE"),
    ("partial", "SPAN_NOT_DELIVERED"),
    ("unknown", "RECEIPT_NOT_FOUND"),
    ("wrong_source", "RECEIPT_WRONG_SOURCE"),
])
def test_receipt_problems_get_precise_codes(project, case, code):
    _, conn, b = project
    if case == "foreign":
        rid = _read(b, task="t-a").read_receipt_id
    elif case == "partial":
        rid = _read(b, start=1, end=5).read_receipt_id
    elif case == "unknown":
        rid = "read-9999"
    else:
        rid = _read(b, source="paper-a").read_receipt_id
    res = _accept(conn, b, [ClaimDraft(statement="s", evidence_links=[_link(rid, 7, 7, B_QUOTE)])])
    assert res.rejected[0].code == code


@pytest.mark.parametrize("draft, code", [
    (ClaimDraft(statement="  "), "EMPTY_STATEMENT"),
    (ClaimDraft(statement="So.", epistemic_status="inferred"), "NO_DERIVATION"),
    (ClaimDraft(statement="So.", epistemic_status="inferred", derivation="from claim-7"), "UNKNOWN_DERIVATION"),
    (ClaimDraft(statement="s", evidence_links=[_link("read-0001", 7, 7, "   ")]), "EMPTY_QUOTE"),
])
def test_drafts_rejected_before_the_broker(project, draft, code):
    _, conn, b = project
    assert _accept(conn, b, [draft]).rejected[0].code == code


def test_inferred_claim_may_cite_a_claim_committed_earlier_in_the_batch(project):
    _, conn, b = project
    r = _read(b)
    drafts = [ClaimDraft(statement="base", evidence_links=[_link(r.read_receipt_id, 7, 7, B_QUOTE)]),
              ClaimDraft(statement="So.", epistemic_status="inferred", derivation="follows from claim-1")]
    assert [c.id for c in _accept(conn, b, drafts).accepted] == ["claim-1", "claim-2"]


def test_rejections_are_stored_and_inspectable(project):
    _, conn, b = project
    r = _read(b)
    _accept(conn, b, [ClaimDraft(statement="Wrong lines.", evidence_links=[_link(r.read_receipt_id, 5, 6, B_QUOTE)])])
    [(rej, task, attempt)] = list_rejections(conn)
    assert (rej.id, rej.code, task, attempt) == ("rej-1", "QUOTE_NOT_AT_LOCATOR", "t-b", "a1")
    text = inspect_claim(conn, "rej-1", broker=b)
    assert "rej-1: REJECTED QUOTE_NOT_AT_LOCATOR" in text
    assert "hint: The quote appears at lines 7-7 of paper-b." in text
    assert "lines 5-6  <- rejected" in text


def test_retry_of_a_committed_draft_returns_the_same_claim(project):
    _, conn, b = project
    r = _read(b)
    d = ClaimDraft(statement="s", evidence_links=[_link(r.read_receipt_id, 7, 7, B_QUOTE)])
    first = _accept(conn, b, [d], attempt="a1")
    again = _accept(conn, b, [d], attempt="a2")
    assert [c.id for c in again.accepted] == [c.id for c in first.accepted] == ["claim-1"]
    assert again.reused == ["claim-1"]
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 1


def test_old_claims_table_gains_a_digest_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE claims (id TEXT PRIMARY KEY, statement TEXT NOT NULL, kind TEXT NOT NULL, "
                 "epistemic_status TEXT NOT NULL, scope TEXT NOT NULL, derivation TEXT NOT NULL, "
                 "limitations TEXT NOT NULL, origin_task TEXT NOT NULL, attempt_id TEXT NOT NULL, created_at TEXT NOT NULL)")
    evidence.ensure_schema(conn)
    assert "digest" in {r[1] for r in conn.execute("PRAGMA table_info(claims)")}


def test_inspect_with_broker_shows_lines_and_full_receipt(project):
    _, conn, b = project
    r = _read(b)
    _accept(conn, b, [ClaimDraft(statement="s", evidence_links=[_link(r.read_receipt_id, 7, 7, B_QUOTE)])])
    text = inspect_claim(conn, "claim-1", broker=b)
    assert f"    > 7 | {B_QUOTE}" in text
    assert "      6 | Seasonal naive forecasts" in text
    assert f"read receipt: {r.read_receipt_id} -> t-b/a1, packet p-t-b, lines 1-7" in text


# review


def _art(content: str, revision=1, task="t-writer") -> ArtifactVersion:
    return ArtifactVersion(artifact_id="art-1", revision=revision, artifact_type="report",
                           content_sha256=hashlib.sha256(content.encode()).hexdigest(), producing_task=task)


def _review(revision=1, disposition="accept", *findings):
    return ReviewResult(reviewed_artifact_id="art-1", reviewed_revision=revision,
                        disposition=disposition, findings=list(findings))


def _two_claims(conn, b):
    ra, rb = _read(b, task="t-a", source="paper-a"), _read(b)
    a = KEY["sources"]["paper-a"]
    accept_claims(conn, b, task_id="t-a", attempt_id="a1", drafts=[
        ClaimDraft(statement="A", evidence_links=[_link(ra.read_receipt_id, 7, 7, a["quote"], "paper-a", "supports")])])
    _accept(conn, b, [ClaimDraft(statement="B", evidence_links=[_link(rb.read_receipt_id, 7, 7, B_QUOTE)])])


GOOD = "# Report\n\n## Supported baseline\n\n- A [claim:claim-1]\n\n## Contradictions\n\n- paper-b says B [claim:claim-2]\n"
COVERAGE = {"req-1": "Supported baseline", "req-2": "Contradictions"}


def test_author_cannot_review_its_own_draft(project):
    _, conn, _ = project
    with pytest.raises(ValueError, match="self review"):
        review.gate(conn, _review(), artifact=_art(GOOD), reviewer_task="t-writer")


def test_runtime_checks_block_what_a_lenient_reviewer_accepts(project):
    _, conn, b = project
    _two_claims(conn, b)
    draft = "# Report\n\n## Supported baseline\n\n- A [claim:claim-1] and [claim:claim-9]\n"
    res = review.gate(conn, _review(), artifact=_art(draft), reviewer_task="t-review", content=draft)
    assert res.decision is GateDecision.needs_revision
    assert {f.claim_id for f in res.runtime_findings} == {"claim-9", "claim-2"}
    rows = review.list_findings(conn, "art-1", 1)
    assert {r["origin"] for r in rows} == {"runtime"} and len(rows) == 2

    res2 = review.gate(conn, _review(2), artifact=_art(draft, 2), reviewer_task="t-review", content=draft, revision_pass=1)
    assert res2.decision is GateDecision.accept_with_limitations
    assert any("claim-2" in lim for lim in res2.limitations)


def test_clean_draft_passes_runtime_checks(project):
    _, conn, b = project
    _two_claims(conn, b)
    res = review.gate(conn, _review(), artifact=_art(GOOD), reviewer_task="t-review", content=GOOD)
    assert res.decision is GateDecision.accept and res.runtime_findings == []
    [row] = review.list_reviews(conn)
    assert (row["reviewer_task"], row["decision"]) == ("t-review", "accept")


# completion


def _assess(conn, content=GOOD, art=None, coverage=COVERAGE):
    return assess(conn, requirements=REQS, artifact=art or _art(content), content=content,
                  coverage=coverage, limitations=[])


def test_unreviewed_deliverable_is_not_complete(project):
    _, conn, b = project
    _two_claims(conn, b)
    a = _assess(conn)
    assert not a.complete and not a.reviewed
    assert "art-1 r1 was never reviewed" in a.problems[0]


def test_review_of_an_older_revision_does_not_count(project):
    _, conn, b = project
    _two_claims(conn, b)
    review.gate(conn, _review(1), artifact=_art(GOOD, 1), reviewer_task="t-review")
    a = _assess(conn, art=_art(GOOD, 2))
    assert not a.complete and any("r2 was never reviewed" in p for p in a.problems)


def test_reviewed_draft_completes_and_rows_list_claims(project):
    _, conn, b = project
    _two_claims(conn, b)
    review.gate(conn, _review(), artifact=_art(GOOD), reviewer_task="t-review", content=GOOD)
    a = _assess(conn)
    assert a.complete and a.reviewed
    assert [(r.requirement_id, r.claim_ids) for r in a.rows] == [("req-1", ["claim-1"]), ("req-2", ["claim-2"])]


def test_uncited_contradiction_and_empty_section_block_completion(project):
    _, conn, b = project
    _two_claims(conn, b)
    draft = "# Report\n\n## Supported baseline\n\n- A [claim:claim-1]\n\n## Contradictions\n\n"
    review.gate(conn, _review(), artifact=_art(draft), reviewer_task="t-review")
    a = _assess(conn, content=draft)
    assert not a.complete
    assert a.uncited_contradictions == ["claim-2"]
    assert any("'Contradictions', which is empty" in p for p in a.problems)


def test_limitations_carry_unresolved_review_findings(project):
    _, conn, b = project
    _two_claims(conn, b)
    finding = ReviewFinding(severity="blocking", message="Explain why paper-c is excluded.")
    review.gate(conn, _review(1, "revise", finding), artifact=_art(GOOD), reviewer_task="t-review", revision_pass=1)
    a = _assess(conn)
    assert a.complete and "Unresolved review finding: Explain why paper-c is excluded." in a.limitations


# export


def test_export_refuses_text_that_is_not_the_artifact(project, tmp_path):
    _, conn, b = project
    _two_claims(conn, b)
    with pytest.raises(ExportError, match="sha256"):
        export_run(conn, project_root=tmp_path, artifact=_art(GOOD), content=GOOD + "x",
                   assessment=_assess(conn), outcome="blocked", run_id="r")


def test_export_refuses_to_call_a_failed_check_completed(project, tmp_path):
    _, conn, b = project
    _two_claims(conn, b)
    with pytest.raises(ExportError, match="completion check did not pass"):
        export_run(conn, project_root=tmp_path, artifact=_art(GOOD), content=GOOD,
                   assessment=_assess(conn), outcome="completed", run_id="r")


# end to end


def _packet(role, task, attempt="a1", **kw) -> Packet:
    return Packet(packet_id=f"p-{task}", task_id=task, attempt_id=attempt, role=role, objective="fixture", **kw)


def test_fake_run_through_the_broker_matches_the_answer_key(project):
    root, conn, b = project
    agents.configure(FakeAgent())
    for p in PAPERS:
        task = f"t-{p}"
        read = _read(b, task=task, source=p, label=p)
        res = agents.run("evidence_reader", _packet("evidence_reader", task, reads=[read]))
        drafts = list(res.claims)
        if p == "paper-b":  # decoy: a quote the paper never says
            drafts.append(ClaimDraft(statement="Seasonal naive is superior on all short series.",
                                     evidence_links=[_link(read.read_receipt_id, 7, 7, "Seasonal naive is superior on all short series.")]))
        accept_claims(conn, b, task_id=task, attempt_id="a1", drafts=drafts, source_lines=source_lines_from(b))

    claims = evidence.list_claims(conn)
    deps = [DependencyItem(kind="claim", id=c.id, claim=ClaimDraft(**c.model_dump(exclude={"id", "origin_task"})))
            for c in claims]
    draft = agents.run("writer", _packet("writer", "t-write", requirements=REQS, dependencies=deps)).payload
    content = draft["content_markdown"]
    art = b.create_artifact(task_id="t-write", attempt_id="a1", artifact_type="report", content=content)
    rev = agents.run("reviewer", _packet("reviewer", "t-review", dependencies=[
        DependencyItem(kind="artifact", id=art.artifact_id, revision=art.revision, content=content), *deps]))
    g = review.gate(conn, ReviewResult(**rev.payload), artifact=art, reviewer_task="t-review", content=content)
    assert g.decision is GateDecision.accept

    a = assess(conn, requirements=REQS, artifact=art, content=content, coverage=draft["coverage"], limitations=[])
    assert a.complete, a.problems
    assert check_answer_key(conn, content, KEY) == []

    paths = export_run(conn, project_root=root, artifact=art, content=content, assessment=a,
                       outcome="completed", run_id="run-1", broker=b)
    prov = json.loads(paths[2].read_text())
    assert prov["cited_claim_ids"] == ["claim-1", "claim-2"]
    assert [s["source_id"] for s in prov["sources"]] == PAPERS
    assert {r["task_id"] for r in prov["receipts"]} == {"t-paper-a", "t-paper-b"}
    assert [r["code"] for r in prov["rejections"]] == ["QUOTE_NOT_AT_LOCATOR"]
    assert prov["reviews"][0]["reviewer_task"] == "t-review"
    for name, sha in prov["files"].items():
        assert hashlib.sha256((root / "output" / name).read_bytes()).hexdigest() == sha
    summary = paths[1].read_text()
    assert "## Sources read but not cited" in summary and "paper-c" in summary.split("## Sources read but not cited")[1]
    assert "## Rejected evidence" in summary and "QUOTE_NOT_AT_LOCATOR" in summary
    assert "Reviewed at this revision: yes" in summary
    assert "superior" not in paths[0].read_text()


def test_answer_key_check_catches_a_cited_miss(project):
    _, conn, b = project
    r = _read(b, task="t-c", source="paper-c")
    accept_claims(conn, b, task_id="t-c", attempt_id="a1", drafts=[ClaimDraft(statement="Baseline matters.", evidence_links=[
        _link(r.read_receipt_id, 3, 3, "We report the baseline characteristics", "paper-c", "supports")])])
    problems = check_answer_key(conn, "- [claim:claim-1]", KEY)
    assert "paper-c is off-topic but is cited" in problems
    assert any(p.startswith("paper-a: expected supports") for p in problems)


def test_requirement_may_map_to_several_sections(project):
    _, conn, b = project
    _two_claims(conn, b)
    review.gate(conn, _review(), artifact=_art(GOOD), reviewer_task="t-review")
    a = _assess(conn, coverage={"req-1": "Supported baseline; Contradictions", "req-2": "Contradictions"})
    assert a.complete and a.rows[0].claim_ids == ["claim-1", "claim-2"]
    a = _assess(conn, coverage={"req-1": "Supported baseline; Nowhere", "req-2": "Contradictions"})
    assert not a.complete and any("'Nowhere'" in p for p in a.problems)
