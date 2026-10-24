"""Run the evidence reader live on the baselines fixture and check it against the answer key.

Pass --base-url/--model/--api-key, or set AR_BASE_URL/AR_MODEL/AR_API_KEY. A local
OpenAI-compatible server (e.g. Ollama) needs no key. Not part of pytest: it calls a model.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ar.adapter import DEFAULT_BASE_URL, LiveAgent, OpenAICompatClient  # noqa: E402
from ar.models import LineSelector, SourceRef  # noqa: E402
from ar.packet import DeliveredRead, Packet  # noqa: E402

FIXTURE = ROOT / "fixtures" / "baselines"


def main() -> int:
    key = json.loads((FIXTURE / "answer_key.json").read_text())
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    args = parser.parse_args()
    if args.base_url or args.model or args.api_key:
        client = OpenAICompatClient(
            args.base_url or DEFAULT_BASE_URL, args.api_key or "", args.model or ""
        )
    else:
        client = OpenAICompatClient.from_env()
    agent = LiveAgent(client)
    failures = 0
    for source_id, entry in key["sources"].items():
        text = (FIXTURE / "input" / "papers" / f"{source_id}.md").read_text()
        lines = text.splitlines()
        read = DeliveredRead(
            source_ref=SourceRef(source_id=source_id),
            selector=LineSelector(start=1, end=len(lines)),
            content=text, read_receipt_id=f"read-{source_id}", label=source_id,
        )
        packet = Packet(
            packet_id=f"smoke-{source_id}", task_id=f"task-{source_id}", attempt_id="a1",
            role="evidence_reader", reads=[read],
            objective="Extract claims relevant to the brief: is seasonal naive an adequate "
            "baseline for weekly demand, and do series under 52 weeks need excluding?",
        )
        result = agent.run("evidence_reader", packet)
        problems = []
        if result.outcome != "produced":
            problems.append(f"outcome {result.outcome}: {result.unresolved}")
        for claim in result.claims:
            for link in claim.evidence_links:
                span = "\n".join(lines[link.locator.start - 1 : link.locator.end])
                if link.quote not in span:
                    problems.append(f"quote not at cited lines: {link.quote!r}")
        if entry["relevant"] and not result.claims:
            problems.append("expected a claim, got none")
        if not entry["relevant"] and result.claims:
            problems.append("expected no claims for an off-topic paper")
        failures += bool(problems)
        print(f"{source_id}: {'FAIL ' + '; '.join(problems) if problems else 'ok'} "
              f"({result.model_calls} calls)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
