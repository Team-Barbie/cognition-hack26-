from pathlib import Path

from ar.agents.fake import load_answer_key

FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"


def test_answer_key_quotes_sit_at_their_lines():
    key = load_answer_key()
    relevant = [(s, v) for s, v in key["sources"].items() if v["relevant"]]
    assert len(relevant) == key["expected_citations"]
    for source_id, entry in relevant:
        lines = (FIXTURE / "input" / "papers" / f"{source_id}.md").read_text().splitlines()
        loc = entry["locator"]
        span = "\n".join(lines[loc["start"] - 1 : loc["end"]])
        assert entry["quote"] in span
