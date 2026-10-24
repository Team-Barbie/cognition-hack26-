"""Export the run: report with footnoted citations, an honest summary, and a portable manifest."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ar.completion import CITATION, CompletionAssessment
from ar.evidence import get_claim, list_claims
from ar.models import ArtifactVersion, Claim

ASSURANCE = "Enforcement is application-level brokerage through the runtime broker; there is no OS sandbox."


def _footnote(claim: Claim) -> str:
    if not claim.evidence_links:
        return f"[^{claim.id}]: {claim.statement} ({claim.epistemic_status}; no source passage)"
    cites = "; ".join(
        f"{l.relation}: {l.source_ref.source_id} r{l.source_ref.revision}, "
        f"lines {l.locator.start}-{l.locator.end}, \"{l.quote}\""
        for l in claim.evidence_links
    )
    return f"[^{claim.id}]: {claim.statement} ({cites})"


def _render_report(
    conn: sqlite3.Connection, content: str, outcome: str
) -> tuple[str, list[Claim], list[str]]:
    cited: dict[str, Claim] = {}
    unresolved: list[str] = []

    def sub(m) -> str:
        cid = m.group(1)
        claim = cited.get(cid) or get_claim(conn, cid)
        if claim is None:
            if cid not in unresolved:
                unresolved.append(cid)
            return f"[claim:{cid} UNRESOLVED]"
        cited.setdefault(cid, claim)
        return f"[^{cid}]"

    body = CITATION.sub(sub, content).rstrip("\n")
    parts = []
    if outcome != "completed":
        parts.append(f"> Draft: the run ended as `{outcome}`. This report is unfinished.\n")
    parts.append(body)
    if cited:
        parts += ["", "## Sources", ""] + [_footnote(c) for c in cited.values()]
    return "\n".join(parts) + "\n", list(cited.values()), unresolved


def _summary(
    assessment: CompletionAssessment, outcome: str, assurance: str, run_id: str, artifact: ArtifactVersion,
    unresolved: list[str], cost: dict | None,
) -> str:
    lines = [
        "# Run summary", "",
        f"- Run: {run_id}",
        f"- Outcome: {outcome}",
        f"- Deliverable: {artifact.artifact_id} r{artifact.revision} (sha256 {artifact.content_sha256})",
        f"- Completion check: {'passed' if assessment.complete else 'not satisfied'}",
        "", "## Requirements", "", "| Requirement | Section | Covered |", "| --- | --- | --- |",
    ]
    lines += [f"| {r.requirement_id} | {r.section or '-'} | {'yes' if r.covered else 'no'} |" for r in assessment.rows]
    if not assessment.rows:
        lines.append("| (none) | - | - |")
    lines += ["", "## Problems", ""]
    problems = list(assessment.problems)
    problems += [f"[claim:{c}] was not a committed claim and is left unresolved in the report" for c in unresolved
                 if f"[claim:{c}]" not in " ".join(problems)]
    lines += [f"- {p}" for p in problems] or ["- none"]
    lines += ["", "## Limitations", ""]
    lines += [f"- {x}" for x in assessment.limitations] or ["- none recorded"]
    if cost:
        lines += ["", "## Cost", ""] + [f"- {k}: {v}" for k, v in cost.items()]
    lines += ["", "## Assurance", "", assurance]
    return "\n".join(lines) + "\n"


def _claim_record(conn: sqlite3.Connection, claim: Claim) -> dict[str, Any]:
    attempt = conn.execute("SELECT attempt_id FROM claims WHERE id=?", (claim.id,)).fetchone()[0]
    rec = claim.model_dump(exclude={"evidence_links"})
    rec["attempt_id"] = attempt
    rec["evidence_links"] = [
        {"relation": l.relation, "source_id": l.source_ref.source_id, "revision": l.source_ref.revision,
         "line_start": l.locator.start, "line_end": l.locator.end, "quote": l.quote,
         "read_receipt_id": l.read_receipt_id}
        for l in claim.evidence_links
    ]
    return rec


def export_run(
    conn: sqlite3.Connection, *, project_root: Path, artifact: ArtifactVersion, content: str,
    assessment: CompletionAssessment, outcome: str, assurance: str = ASSURANCE, run_id: str,
    cost: dict | None = None,
) -> list[Path]:
    outcome, assurance = str(outcome), assurance or ASSURANCE
    out = Path(project_root) / "output"
    out.mkdir(parents=True, exist_ok=True)
    report, _, unresolved = _render_report(conn, content, outcome)
    claims = [_claim_record(conn, c) for c in list_claims(conn)]
    manifest = {
        "run_id": run_id, "outcome": outcome, "assurance": assurance,
        "complete": assessment.complete, "problems": assessment.problems,
        "limitations": assessment.limitations, "unresolved_citations": unresolved,
        "requirements": [vars(r) for r in assessment.rows],
        "artifact": {"id": artifact.artifact_id, "revision": artifact.revision,
                     "sha256": artifact.content_sha256},
        "claims": claims, "cost": cost,
    }
    paths = {
        "report.md": report,
        "run-summary.md": _summary(assessment, outcome, assurance, run_id, artifact, unresolved, cost),
        "provenance.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    }
    written = []
    for name, text in paths.items():
        path = out / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written

