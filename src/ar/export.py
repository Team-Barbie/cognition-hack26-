"""Export the run: report with footnoted citations, an honest summary, and a portable manifest.

`output/report.md` is the readable form: each [claim:id] becomes a footnote with its source,
lines, quote, and receipt. The exact reviewed bytes are also kept, unchanged, at
`output/deliverables/<artifact>-r<revision>.md`, and `provenance.json` records the sha256 of every
file written. Export refuses to label a run completed when the completion check did not pass.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ar.broker import QUOTE_NORMALIZATION
from ar.completion import CompletionAssessment
from ar.evidence import CITATION, claim_attempt, get_claim, link_provenance, list_claims, list_rejections
from ar.experiment import ExperimentRun, get_run, list_runs
from ar.imports import ImportRecord, get_import, list_imports
from ar.models import SCHEMA_VERSION, ArtifactVersion, Claim
from ar.review import list_findings, list_reviews

ASSURANCE = "Enforcement is application-level brokerage through the runtime broker; there is no OS sandbox."


class ExportError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PROVENANCE_CITATION = re.compile(r"\[(run|import):([^\]\s]+)\]")


def _label(conn: sqlite3.Connection, link) -> str:
    imported = link_provenance(conn, link)
    return f" [{imported.provenance}: {imported.id}, executor {imported.executor}]" if imported else ""


def _footnote(conn: sqlite3.Connection, claim: Claim) -> str:
    if not claim.evidence_links:
        return f"[^{claim.id}]: {claim.statement} ({claim.epistemic_status}; no source passage)"
    cites = "; ".join(
        f"{l.relation}: {l.source_ref.source_id} r{l.source_ref.revision}, "
        f"lines {l.locator.start}-{l.locator.end}, \"{l.quote}\"{_label(conn, l)} (receipt {l.read_receipt_id})"
        for l in claim.evidence_links
    )
    return f"[^{claim.id}]: {claim.statement} ({cites})"


def _run_footnote(run: ExperimentRun) -> str:
    outputs = ", ".join(f"{p} sha256 {h}" for p, h in run.output_hashes.items()) or "no outputs"
    verdict = "result acceptable" if run.result_acceptable else "result NOT acceptable"
    deviations = f"; deviations: {'; '.join(run.deviations)}" if run.deviations else ""
    return (f"[^{run.id}]: Experiment run {run.id} of {run.spec_id} [{run.provenance}]: {run.outcome}, "
            f"exit {run.exit_status}, seed {run.seed}, {verdict}; outputs: {outputs}{deviations}. {run.assurance}")


def _import_footnote(rec: ImportRecord) -> str:
    return (f"[^{rec.id}]: Imported {rec.kind} {rec.id} [{rec.provenance}], executor {rec.executor}, "
            f"sha256 {rec.content_sha256}. {rec.assurance}")


def _render_report(
    conn: sqlite3.Connection, content: str, outcome: str
) -> tuple[str, list[Claim], list[str]]:
    cited: dict[str, Claim] = {}
    unresolved: list[str] = []
    extra: dict[str, str] = {}

    def sub(m) -> str:
        cid = m.group(1)
        claim = cited.get(cid) or get_claim(conn, cid)
        if claim is None:
            if cid not in unresolved:
                unresolved.append(cid)
            return f"[claim:{cid} UNRESOLVED]"
        cited.setdefault(cid, claim)
        return f"[^{cid}]"

    def sub_provenance(m) -> str:
        kind, rid = m.groups()
        found = get_run(conn, rid) if kind == "run" else get_import(conn, rid)
        if found is None:
            return f"[{kind}:{rid} UNRESOLVED]"
        extra[rid] = _run_footnote(found) if kind == "run" else _import_footnote(found)
        return f"[^{rid}]"

    body = PROVENANCE_CITATION.sub(sub_provenance, CITATION.sub(sub, content)).rstrip("\n")
    parts = []
    if outcome != "completed":
        parts.append(f"> Draft: the run ended as `{outcome}`. This report is unfinished.\n")
    parts.append(body)
    if cited or extra:
        parts += ["", "## Sources", ""] + [_footnote(conn, c) for c in cited.values()] + list(extra.values())
    return "\n".join(parts) + "\n", list(cited.values()), unresolved


def _sources(broker) -> list[dict[str, Any]]:
    """Every registered source revision, read from the broker's tables without issuing receipts."""
    if broker is None:
        return []
    rows = broker.conn.execute(
        "SELECT source_id, revision, path, original_sha256, representation_sha256, line_count "
        "FROM sources ORDER BY source_id, revision"
    ).fetchall()
    return [dict(r) for r in rows]


def _receipts(broker, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if broker is None:
        return []
    wanted = {(c["origin_task"], c["attempt_id"]) for c in claims}
    out = []
    for task_id, attempt_id in sorted(wanted):
        out += [r.model_dump(mode="json") for r in broker.receipts_for(task_id, attempt_id)]
    return out


def _rejections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"id": r.id, "task_id": task, "attempt_id": attempt, "code": r.code, "reason": r.reason,
         "link_index": r.link_index, "hint": r.hint, "draft": r.draft.model_dump(mode="json")}
        for r, task, attempt in list_rejections(conn)
    ]


def _reviews(conn: sqlite3.Connection, artifact: ArtifactVersion) -> list[dict[str, Any]]:
    out = []
    for r in list_reviews(conn):
        if r["artifact_id"] != artifact.artifact_id:
            continue
        rec = {k: r[k] for k in ("artifact_id", "revision", "reviewer_task", "disposition", "decision", "revision_pass")}
        rec["checks"] = json.loads(r["checks"])
        rec["findings"] = [
            {k: f[k] for k in ("severity", "message", "claim_id", "origin")} | {"resolved": bool(f["resolved"])}
            for f in list_findings(conn, artifact.artifact_id, r["revision"])
        ]
        out.append(rec)
    return out


def _summary(
    assessment: CompletionAssessment, outcome: str, assurance: str, run_id: str, artifact: ArtifactVersion,
    unresolved: list[str], cost: dict | None, *, cited: list[Claim] = (), sources: list[dict] = (),
    rejections: list[dict] = (), reviews: list[dict] = (),
) -> str:
    lines = [
        "# Run summary", "",
        f"- Run: {run_id}",
        f"- Outcome: {outcome}",
        f"- Deliverable: {artifact.artifact_id} r{artifact.revision} (sha256 {artifact.content_sha256})",
        f"- Completion check: {'passed' if assessment.complete else 'not satisfied'}",
        f"- Reviewed at this revision: {'yes' if assessment.reviewed else 'no'}",
        "", "## Requirements", "", "| Requirement | Section | Covered | Claims |", "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r.requirement_id} | {r.section or '-'} | {'yes' if r.covered else 'no'} | {', '.join(r.claim_ids) or '-'} |"
        for r in assessment.rows
    ]
    if not assessment.rows:
        lines.append("| (none) | - | - | - |")

    if cited:
        lines += ["", "## Citations", ""]
        for c in cited:
            lines.append(f"- `{c.id}`: {c.statement}")
            lines += [
                f"  - {l.relation} · {l.source_ref.source_id} r{l.source_ref.revision} lines "
                f"{l.locator.start}-{l.locator.end} · receipt {l.read_receipt_id}"
                for l in c.evidence_links
            ]
    if sources:
        used = {l.source_ref.source_id for c in cited for l in c.evidence_links}
        uncited = [s for s in sources if s["source_id"] not in used]
        if uncited:
            lines += ["", "## Sources read but not cited", ""]
            lines += [f"- {s['source_id']} ({s['path']})" for s in uncited]

    lines += ["", "## Problems", ""]
    problems = list(assessment.problems)
    problems += [f"[claim:{c}] was not a committed claim and is left unresolved in the report" for c in unresolved
                 if f"[claim:{c}]" not in " ".join(problems)]
    lines += [f"- {p}" for p in problems] or ["- none"]

    if reviews:
        lines += ["", "## Review", ""]
        for r in reviews:
            who = r["reviewer_task"] or "reviewer"
            lines.append(f"- r{r['revision']}: {who} said {r['disposition']}, gate decided {r['decision']}")
            lines += [
                f"  - {f['severity']} ({f['origin']}{', resolved' if f['resolved'] else ''}): {f['message']}"
                for f in r["findings"]
            ]
    if rejections:
        lines += ["", "## Rejected evidence", ""]
        for r in rejections:
            hint = f" {r['hint']}" if r["hint"] else ""
            lines.append(f"- `{r['id']}` {r['code']} from {r['task_id']}: {r['reason']}.{hint}")

    lines += ["", "## Limitations", ""]
    lines += [f"- {x}" for x in assessment.limitations] or ["- none recorded"]
    if cost:
        lines += ["", "## Cost", ""] + [f"- {k}: {v}" for k, v in cost.items()]
    lines += ["", "## Assurance", "", assurance]
    return "\n".join(lines) + "\n"


def _provenance(conn: sqlite3.Connection, link) -> str | None:
    imported = link_provenance(conn, link)
    return imported.provenance if imported else None


def _claim_record(conn: sqlite3.Connection, claim: Claim) -> dict[str, Any]:
    rec = claim.model_dump(exclude={"evidence_links"})
    rec["attempt_id"] = claim_attempt(conn, claim.id)
    rec["evidence_links"] = [
        {"relation": l.relation, "source_id": l.source_ref.source_id, "revision": l.source_ref.revision,
         "line_start": l.locator.start, "line_end": l.locator.end, "quote": l.quote,
         "read_receipt_id": l.read_receipt_id, "provenance": _provenance(conn, l)}
        for l in claim.evidence_links
    ]
    return rec


def export_run(
    conn: sqlite3.Connection, *, project_root: Path, artifact: ArtifactVersion, content: str,
    assessment: CompletionAssessment, outcome: str, assurance: str = ASSURANCE, run_id: str,
    cost: dict | None = None, broker=None,
) -> list[Path]:
    """Write report.md, run-summary.md, provenance.json, and the exact deliverable bytes.

    Raises ExportError when `content` is not the artifact's bytes, or when `outcome` is
    "completed" but the completion check failed. With `broker`, the manifest also lists every
    source with its hashes and the receipts behind each committed claim.
    """
    outcome, assurance = str(outcome), assurance or ASSURANCE
    if _sha(content) != artifact.content_sha256:
        raise ExportError(f"content does not match {artifact.artifact_id} r{artifact.revision} (sha256 differs)")
    if outcome == "completed" and not assessment.complete:
        raise ExportError("outcome is completed but the completion check did not pass: " + "; ".join(assessment.problems))

    out = Path(project_root) / "output"
    out.mkdir(parents=True, exist_ok=True)
    report, cited, unresolved = _render_report(conn, content, outcome)
    if outcome == "completed" and unresolved:
        raise ExportError(f"completed report cites {', '.join(unresolved)}, not committed claims")

    claims = [_claim_record(conn, c) for c in list_claims(conn)]
    sources = _sources(broker)
    rejections = _rejections(conn)
    reviews = _reviews(conn, artifact)
    exact = f"deliverables/{artifact.artifact_id}-r{artifact.revision}.md"
    texts = {
        "report.md": report,
        "run-summary.md": _summary(assessment, outcome, assurance, run_id, artifact, unresolved, cost,
                                   cited=cited, sources=sources, rejections=rejections, reviews=reviews),
        exact: content,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION, "quote_normalization": QUOTE_NORMALIZATION,
        "run_id": run_id, "outcome": outcome, "assurance": assurance,
        "complete": assessment.complete, "reviewed": assessment.reviewed, "problems": assessment.problems,
        "limitations": assessment.limitations, "unresolved_citations": unresolved,
        "uncited_contradictions": assessment.uncited_contradictions,
        "requirements": [vars(r) for r in assessment.rows],
        "artifact": {"id": artifact.artifact_id, "revision": artifact.revision,
                     "sha256": artifact.content_sha256, "producing_task": artifact.producing_task,
                     "exact_copy": exact},
        "cited_claim_ids": [c.id for c in cited],
        "claims": claims, "rejections": rejections, "reviews": reviews,
        "sources": sources, "receipts": _receipts(broker, claims),
        "files": {name: _sha(text) for name, text in texts.items()},
        "cost": cost,
        "experiments": [r.model_dump() for r in list_runs(conn)],
        "imports": [r.model_dump() for r in list_imports(conn)],
    }
    texts["provenance.json"] = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    written = []
    for name in ("report.md", "run-summary.md", "provenance.json", exact):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(texts[name].encode("utf-8"))  # bytes, so the manifest hashes match on Windows
        written.append(path)
    return written
