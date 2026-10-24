import json
from types import ModuleType, SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from ar.adapter import (
    DEFAULT_MODEL,
    ENVELOPE_INSTRUCTIONS,
    AdapterError,
    AnthropicClient,
    LiveAgent,
)
from ar.agents import roles
from ar.models import Record
from ar.packet import Packet


class StubPayload(Record):
    text: str


class ScriptedClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0)


def envelope(**kw) -> str:
    body = {"payload": {"text": "hi"}, "outcome": "produced"}
    body.update(kw)
    return json.dumps(body)


@pytest.fixture(autouse=True)
def stub_role():
    mod = ModuleType("stub_writer")
    mod.PAYLOAD = StubPayload
    mod.SYSTEM = "STUB SYSTEM"
    mod.user_message = lambda packet: f"objective: {packet.objective}"
    roles.OVERRIDES["writer"] = mod
    yield
    roles.OVERRIDES.clear()


def packet() -> Packet:
    return Packet(packet_id="p1", task_id="t1", attempt_id="a1", role="writer", objective="go")


def test_valid_first_reply():
    client = ScriptedClient([envelope(limitations=["l"], unresolved=["u"])])
    result = LiveAgent(client).run("writer", packet())
    assert result.outcome == "produced"
    assert result.payload == {"text": "hi"}
    assert result.limitations == ["l"]
    assert result.unresolved == ["u"]
    assert result.model_calls == 1
    system, user = client.calls[0]
    assert system.startswith(f"STUB SYSTEM\n\n{ENVELOPE_INSTRUCTIONS}")
    assert '"required": ["text"]' in system
    assert '"research_agent"' in system and '"synthesis_agent"' in system
    assert user == "objective: go"


def test_fenced_json_with_prose():
    reply = f"Here you go:\n```json\n{envelope()}\n```\nHope that helps."
    result = LiveAgent(ScriptedClient([reply])).run("writer", packet())
    assert result.outcome == "produced"
    assert result.model_calls == 1


def test_model_cannot_set_model_calls():
    result = LiveAgent(ScriptedClient([envelope(model_calls=99)])).run("writer", packet())
    assert result.model_calls == 1


def test_one_repair_then_success():
    client = ScriptedClient([json.dumps({"payload": {"wrong": 1}}), envelope()])
    result = LiveAgent(client).run("writer", packet())
    assert result.outcome == "produced"
    assert result.model_calls == 2
    repair = client.calls[1][1]
    assert repair.startswith("objective: go")
    assert '{"payload": {"wrong": 1}}' in repair
    assert "text" in repair and "missing" in repair
    assert '"input"' not in repair.split("Validation errors:")[1]


def test_repair_on_invalid_json():
    client = ScriptedClient(["not json at all", envelope()])
    result = LiveAgent(client).run("writer", packet())
    assert result.model_calls == 2
    assert "invalid JSON" in client.calls[1][1]


def test_two_bad_replies_unable_to_complete():
    client = ScriptedClient(["{}", "{}"])
    result = LiveAgent(client).run("writer", packet())
    assert result.outcome == "unable_to_complete"
    assert result.model_calls == 2
    assert result.payload == {}
    assert result.unresolved and "text" in result.unresolved[0]
    assert len(client.calls) == 2


def test_claims_and_tasks_validated():
    roles.OVERRIDES["evidence_reader"] = roles.OVERRIDES["writer"]  # same stub payload
    good = envelope(
        claims=[{"statement": "s"}],
        proposed_tasks=[{"role": "reviewer", "objective": "check"}],
    )
    result = LiveAgent(ScriptedClient([good])).run("evidence_reader", packet())
    assert result.claims[0].statement == "s"
    assert result.proposed_tasks[0].role == "reviewer"

    bad = envelope(claims=[{"statement": "s", "bogus": 1}])
    result = LiveAgent(ScriptedClient([bad, bad])).run("evidence_reader", packet())
    assert result.outcome == "unable_to_complete"
    assert result.model_calls == 2


def test_only_the_evidence_reader_creates_claims():
    # A writer that echoes claim references in the envelope must not fail validation or mint claims.
    echoed = envelope(claims=[{"claim_id": "claim-1", "evidence_links": [{"claim_id": "claim-1"}]}])
    result = LiveAgent(ScriptedClient([echoed])).run("writer", packet())
    assert result.outcome == "produced" and result.claims == [] and result.model_calls == 1


def test_writer_schema_and_invalid_outline_repair():
    roles.OVERRIDES.clear()
    invalid = envelope(payload={"thesis": "A report", "sections": []})
    valid = envelope(payload={"content_markdown": "# Findings\nLimited evidence.", "cited_claim_ids": [],
                              "coverage": {}, "limitations_stated": True})
    client = ScriptedClient([invalid, valid])
    result = LiveAgent(client).run("writer", packet())
    assert result.outcome == "produced" and result.model_calls == 2
    assert result.payload["content_markdown"].startswith("# Findings")
    assert '"required": ["content_markdown", "cited_claim_ids", "coverage", "limitations_stated"]' in client.calls[0][0]
    assert "content_markdown" in client.calls[1][1] and "extra_forbidden" in client.calls[1][1]


def test_unknown_proposed_role_requires_repair():
    invalid = envelope(proposed_tasks=[{"role": "curator", "objective": "screen"}])
    client = ScriptedClient([invalid, envelope(proposed_tasks=[])])
    result = LiveAgent(client).run("writer", packet())
    assert result.outcome == "produced" and result.model_calls == 2
    assert result.proposed_tasks == []
    assert "literal_error" in client.calls[1][1]


def test_non_produced_outcome_passes_through():
    reply = json.dumps({"outcome": "needs_dependency", "unresolved": ["need reader"]})
    result = LiveAgent(ScriptedClient([reply])).run("writer", packet())
    assert result.outcome == "needs_dependency"
    assert result.unresolved == ["need reader"]
    assert result.model_calls == 1


def test_client_exception_wrapped():
    class Boom:
        def complete(self, system: str, user: str) -> str:
            raise ConnectionError("down")

    with pytest.raises(AdapterError):
        LiveAgent(Boom()).run("writer", packet())


class FakeSdk:
    """Stands in for anthropic.Anthropic; records the request and returns a canned response."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.kwargs: dict = {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def _response(blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, stop_details=stop_details)


def _text(text):
    return SimpleNamespace(type="text", text=text)


def test_request_shape():
    sdk = FakeSdk(_response([SimpleNamespace(type="thinking", thinking=""), _text("{}"), _text("!")]))
    client = AnthropicClient(sdk, effort="high")
    assert client.complete("sys", "usr") == "{}!"
    kw = sdk.kwargs
    assert kw["model"] == DEFAULT_MODEL == "claude-opus-5-5"
    assert kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": "usr"}]
    assert kw["output_config"] == {"effort": "high"}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and "thinking" not in kw


def test_refusal_raises_adapter_error():
    resp = _response([], "refusal", SimpleNamespace(category="cyber"))
    with pytest.raises(AdapterError, match="cyber"):
        AnthropicClient(FakeSdk(resp)).complete("s", "u")


def test_http_error_wrapped():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.AuthenticationError(
        "bad key sk-secret", response=httpx.Response(401, request=request), body=None
    )
    with pytest.raises(AdapterError) as info:
        AnthropicClient(FakeSdk(error=error)).complete("s", "u")
    assert "401" in str(info.value)
    assert "sk-secret" not in str(info.value)


def test_connection_error_wrapped():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(AdapterError):
        AnthropicClient(FakeSdk(error=anthropic.APIConnectionError(request=request))).complete("s", "u")
