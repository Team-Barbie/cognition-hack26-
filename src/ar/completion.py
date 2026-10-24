"""Completion is a runtime decision over committed claims and the deliverable, never a model's say-so."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ar.evidence import get_claim
from ar.models import ArtifactVersion, Requirement

CITATION = re.compile(r"\[claim:([^\]\s]+)\]")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", re.M)


@dataclass
class RequirementRow:
    requirement_id: str
    section: str | None
    covered: bool


@dataclass
class CompletionAssessment:
    complete: bool
    rows: list[RequirementRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.lstrip("# ").split()).casefold()


def assess(
    conn: sqlite3.Connection, *, requirements: list[Requirement], artifact: ArtifactVersion,
    content: str, coverage: dict[str, str], limitations: list[str],
) -> CompletionAssessment:
    problems: list[str] = []
    if not content.strip():
        problems.append(f"deliverable {artifact.artifact_id} r{artifact.revision} is empty")

    cited = list(dict.fromkeys(CITATION.findall(content)))
    unknown = [cid for cid in cited if get_claim(conn, cid) is None]
    problems += [f"citation [claim:{cid}] does not resolve to a committed claim" for cid in unknown]

    headings = {_norm(h) for h in HEADING.findall(content)}
    rows: list[RequirementRow] = []
    for req in requirements:
        section = coverage.get(req.id)
        covered = section is not None and _norm(section) in headings
        if section is None:
            problems.append(f"requirement {req.id} is not mapped to a section")
        elif not covered:
            problems.append(f"requirement {req.id} maps to section '{section}', which is not in the deliverable")
        rows.append(RequirementRow(req.id, section, covered))

    return CompletionAssessment(
        complete=not problems, rows=rows, problems=problems,
        limitations=list(limitations), unresolved_citations=unknown,
    )
