import json
import re
import sqlite3
from pathlib import Path

from ar import evidence
from ar.completion import CompletionAssessment, RequirementRow
from ar.export import ASSURANCE, export_run
from ar.models import ArtifactVersion, ClaimDraft, EvidenceLink, LineSelector, SourceRef

KEY = json.loads((Path(__file__).parent.parent / "fixtures" / "baselines" / "answer_key.json").read_text())
ART = ArtifactVersion(artifact_id="art-1", revision=2, artifact_type="report",
                      content_sha256="ab" * 32, producing_task="t-writer")
REPORT = """# Report

## Supported baseline

- Seasonal naive is supported. [claim:claim-1]

## Contradictions

- paper-b contradicts the exclusion rule. [claim:claim-2]
"""


class AcceptAll:
    def verify_evidence_link(self, link, *, task_id, attempt_id):
        return None


def draft(source: str) -> ClaimDraft:
    info = KEY["sources"][source]
    link = EvidenceLink(relation=info["relation"], source_ref=SourceRef(source_id=source),
                        locator=LineSelector(**info["locator"]), quote=info["quote"],
                        read_receipt_id=f"read-{source}")
    return ClaimDraft(statement=info["statement"], evidence_links=[link])


def conn_() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    evidence.ensure_schema(conn)
    evidence.accept_claims(conn, AcceptAll(), task_id="t-a", attempt_id="a1",
                           drafts=[draft("paper-a"), draft("paper-b")])
    return conn


def assessment(complete=True, problems=()) -> CompletionAssessment:
    rows = [RequirementRow("req-1", "Supported baseline", True), RequirementRow("req-2", "Contradictions", True)]
    return CompletionAssessment(complete=complete, rows=rows, problems=list(problems), limitations=["Not exhaustive."])


def export(tmp_path, conn=None, content=REPORT, outcome="completed", **kw):
    paths = export_run(conn or conn_(), project_root=tmp_path, artifact=ART, content=content,
                       assessment=kw.pop("assessment", assessment()), outcome=outcome,
                       assurance=ASSURANCE, run_id="run-1", **kw)
    return paths


def test_report_footnotes_resolve(tmp_path):
    paths = export(tmp_path)
    assert [p.name for p in paths] == ["report.md", "run-summary.md", "provenance.json"]
    assert all(p.parent == tmp_path / "output" for p in paths)
    report = paths[0].read_text(encoding="utf-8")
    assert "[claim:" not in report
    markers = set(re.findall(r"\[\^(claim-\d+)\](?!:)", report))
    defs = set(re.findall(r"^\[\^(claim-\d+)\]:", report, re.M))
    assert markers == defs == {"claim-1", "claim-2"}
    for src in ("paper-a", "paper-b"):
        assert KEY["sources"][src]["quote"] in report and src in report
    assert "lines 7-7" in report and "Draft:" not in report


def test_summary_has_assurance_and_outcome(tmp_path):
    summary = export(tmp_path, cost={"model_calls": 3})[1].read_text(encoding="utf-8")
    assert ASSURANCE in summary and "Outcome: completed" in summary
    assert "| req-1 | Supported baseline | yes |" in summary
    assert "Not exhaustive." in summary and "model_calls: 3" in summary


def test_provenance_lists_only_committed_claims(tmp_path):
    content = REPORT + "\n- Made up. [claim:claim-9]\n"
    paths = export(tmp_path, content=content, assessment=assessment(False, ["citation [claim:claim-9] unknown"]))
    prov = json.loads(paths[2].read_text(encoding="utf-8"))
    assert [c["id"] for c in prov["claims"]] == ["claim-1", "claim-2"]
    assert prov["claims"][1]["evidence_links"][0]["read_receipt_id"] == "read-paper-b"
    assert prov["claims"][0]["attempt_id"] == "a1"
    assert prov["assurance"] == ASSURANCE and prov["run_id"] == "run-1"
    assert prov["artifact"] == {"id": "art-1", "revision": 2, "sha256": "ab" * 32}
    assert prov["unresolved_citations"] == ["claim-9"]
    report = paths[0].read_text(encoding="utf-8")
    assert "[claim:claim-9 UNRESOLVED]" in report and "[^claim-9]" not in report
    assert "claim-9" in paths[1].read_text(encoding="utf-8")


def test_budget_exhausted_rendered_honestly(tmp_path):
    paths = export(tmp_path, outcome="budget_exhausted", assessment=assessment(False, ["requirement req-2 is not mapped"]))
    summary = paths[1].read_text(encoding="utf-8")
    assert "Outcome: budget_exhausted" in summary and "Outcome: completed" not in summary
    assert "not satisfied" in summary and "req-2 is not mapped" in summary
    assert "budget_exhausted" in paths[0].read_text(encoding="utf-8")
    prov = json.loads(paths[2].read_text(encoding="utf-8"))
    assert prov["outcome"] == "budget_exhausted" and prov["complete"] is False
    assert ASSURANCE in paths[1].read_text(encoding="utf-8") and prov["assurance"] == ASSURANCE
