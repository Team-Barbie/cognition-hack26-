"""Completion is a runtime decision over committed claims and the deliverable, never a model's say-so.

A deliverable completes only when every requirement maps to a non-empty section, every citation
resolves to a committed claim, contradicting evidence is cited, and the exact revision was
reviewed. Unresolved blocking findings from that review are carried as limitations.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ar.evidence import CITATION, contradicting_claims, get_claim
from ar.models import ArtifactVersion, Requirement
from ar.review import GateDecision, latest_review, list_findings

__all__ = ["CITATION", "HEADING", "CompletionAssessment", "RequirementRow", "assess", "sections"]

HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", re.M)


@dataclass
class RequirementRow:
    requirement_id: str
    section: str | None
    covered: bool
    claim_ids: list[str] = field(default_factory=list)  # citations inside the mapped section


@dataclass
class CompletionAssessment:
    complete: bool
    rows: list[RequirementRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)
    reviewed: bool = False
    uncited_contradictions: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.lstrip("# ").split()).casefold()


def sections(content: str) -> dict[str, str]:
    """Normalized heading to the text under it, up to the next heading of any level."""
    marks = list(HEADING.finditer(content))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        out.setdefault(_norm(m.group(1)), content[m.end() : end])
    return out


def assess(
    conn: sqlite3.Connection, *, requirements: list[Requirement], artifact: ArtifactVersion,
    content: str, coverage: dict[str, str], limitations: list[str], require_review: bool = True,
) -> CompletionAssessment:
    problems: list[str] = []
    if not content.strip():
        problems.append(f"deliverable {artifact.artifact_id} r{artifact.revision} is empty")

    cited = list(dict.fromkeys(CITATION.findall(content)))
    unknown = [cid for cid in cited if get_claim(conn, cid) is None]
    problems += [f"citation [claim:{cid}] does not resolve to a committed claim" for cid in unknown]

    uncited = [cid for cid in contradicting_claims(conn, artifact.depends_on or None) if cid not in cited]
    problems += [f"contradicting claim [claim:{cid}] is not cited in the deliverable" for cid in uncited]

    found = sections(content)
    rows: list[RequirementRow] = []
    for req in requirements:
        section = coverage.get(req.id)
        if section is None:
            problems.append(f"requirement {req.id} is not mapped to a section")
            rows.append(RequirementRow(req.id, None, False))
            continue
        # A writer may map one requirement to several sections: "Findings; Conclusion".
        names = [part.strip() for part in section.split(";") if part.strip()] or [section]
        missing = [name for name in names if _norm(name) not in found]
        bodies = [found[_norm(name)] for name in names if _norm(name) in found]
        if missing:
            listed = ", ".join(f"'{name}'" for name in missing)
            problems.append(f"requirement {req.id} maps to section {listed}, which is not in the deliverable")
        elif not any(body.strip() for body in bodies):
            problems.append(f"requirement {req.id} maps to section '{section}', which is empty")
        covered = not missing and any(body.strip() for body in bodies)
        ids = list(dict.fromkeys(cid for body in bodies for cid in CITATION.findall(body)))
        rows.append(RequirementRow(req.id, section, covered, ids))

    carried = list(limitations)
    review = latest_review(conn, artifact.artifact_id, artifact.revision)
    reviewed = review is not None and review["decision"] in (
        GateDecision.accept, GateDecision.accept_with_limitations
    )
    if require_review and not reviewed:
        why = "was never reviewed" if review is None else f"was not accepted by review ({review['decision']})"
        problems.append(f"deliverable {artifact.artifact_id} r{artifact.revision} {why}")
    if review is not None and review["decision"] == GateDecision.accept_with_limitations:
        for f in list_findings(conn, artifact.artifact_id, artifact.revision):
            text = f"Unresolved review finding: {f['message']}"
            if f["severity"] == "blocking" and f["message"] not in carried and text not in carried:
                carried.append(text)

    return CompletionAssessment(
        complete=not problems, rows=rows, problems=problems,
        limitations=carried, unresolved_citations=unknown,
        reviewed=reviewed, uncited_contradictions=uncited,
    )
