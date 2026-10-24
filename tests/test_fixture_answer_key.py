import json
from pathlib import Path

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"


def test_answer_key_quotes_sit_at_their_lines():
    key = json.loads((FIXTURE / "answer_key.json").read_text())
    relevant = [(s, v) for s, v in key["sources"].items() if v["relevant"]]
    assert len(relevant) == key["expected_citations"]
    for source_id, entry in relevant:
        lines = (FIXTURE / "input" / "papers" / f"{source_id}.md").read_text().splitlines()
        loc = entry["locator"]
        span = "\n".join(lines[loc["start"] - 1 : loc["end"]])
        assert entry["quote"] in span
