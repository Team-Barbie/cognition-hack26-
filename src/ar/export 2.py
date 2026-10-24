"""Reviewed output export."""

from __future__ import annotations

import json
from pathlib import Path

from ar.packet import DependencyItem


def export_run(
    root: Path,
    run_id: str,
    report: str,
    claims: list[DependencyItem],
) -> Path:
    output = root / "output"
    deliverables = output / "deliverables"
    deliverables.mkdir(parents=True, exist_ok=True)
    report_path = deliverables / "report.md"
    report_path.write_text(report, encoding="utf-8")
    (output / "run-summary.md").write_text(
        f"# Run summary\n\nRun `{run_id}` completed with a reviewed report and {len(claims)} accepted claims.\n",
        encoding="utf-8",
    )
    provenance = {
        "run_id": run_id,
        "claims": [
            {
                "id": item.id,
                "statement": item.claim.statement if item.claim else item.content,
                "evidence": [link.model_dump(mode="json") for link in item.claim.evidence_links]
                if item.claim
                else [],
            }
            for item in claims
        ],
    }
    (output / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    return report_path
