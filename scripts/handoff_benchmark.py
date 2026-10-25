"""Compare structured vs plain-text model handoffs with the same context values.

Default is an offline integration smoke test. --live makes paid model calls in
both arms; repeat with fresh output directories to assess variability.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ar import db  # noqa: E402
from ar.adapter import LiveAgent, load_env, make_client  # noqa: E402
from ar.agents.fake import FakeAgent  # noqa: E402
from ar.controller import run_project  # noqa: E402


def flatten(value, prefix="handoff"):
    """Keep every value in the baseline, removing JSON's typed structure."""
    if isinstance(value, dict) and value:
        return "\n".join(flatten(item, f"{prefix}.{key}") for key, item in value.items())
    if isinstance(value, list) and value:
        return "\n".join(flatten(item, f"{prefix}[{index}]") for index, item in enumerate(value))
    return f"{prefix}: {value}"


class PlainTextClient:
    def __init__(self, client):
        self.client = client

    def complete(self, system, user):
        marker = "\n\nstructured_handoff:\n"
        if marker in user:
            context, state = user.rsplit(marker, 1)
            user = context + "\n\nPlain-text handoff:\n" + flatten(json.loads(state))
            system = system.replace("structured_handoff JSON", "plain-text handoff")
        return self.client.complete(system, user)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True, help="A new directory for this comparison")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    client = None
    if args.live:
        load_env(args.env_file)
        client = make_client(provider=args.provider, model=args.model)
    summary = {"mode": "live" if args.live else "offline_smoke", "model": args.model if args.live else None,
               "note": "Dependency requests and unresolved items are proxies, not semantic quality scores. "
                       "Offline results verify wiring only; they do not measure model behavior.", "arms": {}}
    for arm in ("structured", "plain_text"):
        project = args.output / arm
        shutil.copytree(ROOT / "fixtures" / "baselines" / "input", project / "input")
        db.init_project(project).close()
        agent = LiveAgent(client if arm == "structured" else PlainTextClient(client)) if args.live else FakeAgent()
        outcome = run_project(project, agent)
        with db.connect(project) as conn:
            metrics = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM events WHERE kind='handoff.evaluated' ORDER BY id")]
            roles = [row[0] for row in conn.execute("SELECT DISTINCT role FROM tasks")]
        summary["arms"][arm] = {
            "outcome": str(outcome), "specialties": roles, "handoffs": len(metrics),
            "dependency_requests": sum(m["dependency_requests"] for m in metrics),
            "unresolved_items": sum(m["unresolved_items"] for m in metrics),
            "rejected_packets": sum(m["rejected"] for m in metrics),
            "model_calls": sum(m["model_calls"] for m in metrics),
        }
        print(f"{arm}: {outcome}", flush=True)
    (args.output / "comparison.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if all(arm["outcome"] == "completed" for arm in summary["arms"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
