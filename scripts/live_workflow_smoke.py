"""Exercise research and synthesis handoffs with real GPT-5 nano calls on the demo papers."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ar.adapter import LiveAgent, load_env  # noqa: E402
from ar.openai_client import DEFAULT_TEST_MODEL, OpenAICompatClient  # noqa: E402
from ar.studio.session import Session  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_TEST_MODEL)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--workdir", type=Path, default=ROOT / ".research" / "live-workflows")
    args = parser.parse_args()
    load_env(args.env_file)
    client = OpenAICompatClient.from_env(model=args.model,
                                       reasoning_effort="minimal" if args.model == DEFAULT_TEST_MODEL else None)
    session = Session(args.workdir, lambda: LiveAgent(client),
                      make_test_agent=lambda: LiveAgent(client), test_model=args.model)
    session.use_demo()
    print(f"Testing workflow with {args.model}; artifacts: {session.root}", flush=True)
    session.start()
    session.wait(300)
    results = {}
    for event in session.since(0):
        if event["type"] == "runtime" and event["event"] == "attempt.accepted":
            result = event["payload"]["result"]
            results[result["role"]] = result["outcome"]
            print(f"{result['role']}: {result['outcome']} ({result['model_calls']} calls)", flush=True)
        if event["type"] == "chat" and "The run stopped:" in event.get("text", ""):
            print(event["text"], flush=True)
    print(json.dumps({"status": session.status, "roles": results}), flush=True)
    expected = {"planner", "evidence_reader", "research_agent", "synthesis_agent", "writer", "reviewer"}
    return 0 if session.status == "done" and all(results.get(role) == "produced" for role in expected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
