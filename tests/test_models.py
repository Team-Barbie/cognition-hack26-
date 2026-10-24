import pytest
from pydantic import ValidationError

from ar.models import AgentResult, Claim, ClaimDraft, LineSelector


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        AgentResult(role="writer", task_id="t1", outcome="produced", bogus=1)


def test_selector_is_one_based():
    with pytest.raises(ValidationError):
        LineSelector(start=0, end=3)


def test_claim_extends_draft():
    draft = ClaimDraft(statement="x")
    claim = Claim(**draft.model_dump(), id="claim-1", origin_task="t1")
    assert claim.id == "claim-1"


def test_json_schema_generates():
    assert "outcome" in AgentResult.model_json_schema()["properties"]
