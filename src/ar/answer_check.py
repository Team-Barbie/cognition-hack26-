"""Product test: does a deliverable cite what the fixture's answer key says it should?"""

from __future__ import annotations

import sqlite3
from typing import Any

from ar.evidence import CITATION, get_claim


def check_answer_key(conn: sqlite3.Connection, content: str, key: dict[str, Any]) -> list[str]:
    """Mismatches between the deliverable's citations and `answer_key.json`. Empty means it matches.

    A cited locator matches when it overlaps the expected lines, so a live model may cite a
    slightly wider span. Quotes were already checked at acceptance.
    """
    problems: list[str] = []
    cited = [c for cid in dict.fromkeys(CITATION.findall(content)) if (c := get_claim(conn, cid))]
    links = [link for c in cited for link in c.evidence_links]

    expected = key.get("expected_citations")
    if expected is not None and len(cited) != expected:
        problems.append(f"deliverable cites {len(cited)} claims; the answer key expects {expected}")

    for source_id, entry in key.get("sources", {}).items():
        mine = [l for l in links if l.source_ref.source_id == source_id]
        if not entry.get("relevant", True):
            if mine:
                problems.append(f"{source_id} is off-topic but is cited")
            continue
        loc = entry["locator"]
        hits = [l for l in mine if l.relation == entry["relation"]
                and l.locator.start <= loc["end"] and l.locator.end >= loc["start"]]
        if not hits:
            got = ", ".join(f"{l.relation} {l.locator.start}-{l.locator.end}" for l in mine) or "nothing"
            problems.append(f"{source_id}: expected {entry['relation']} at lines {loc['start']}-{loc['end']}, got {got}")
        if entry["relation"] == "contradicts" and source_id not in content:
            problems.append(f"deliverable does not name {source_id} as the contradicting source")
    return problems
