import json
import urllib.error
from types import ModuleType

import pytest

from ar import adapter
from ar.adapter import (
    ENVELOPE_INSTRUCTIONS,
    AdapterError,
    LiveAgent,
    OpenAICompatClient,
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
    assert system == f"STUB SYSTEM\n\n{ENVELOPE_INSTRUCTIONS}"
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
    good = envelope(
        claims=[{"statement": "s"}],
        proposed_tasks=[{"role": "reviewer", "objective": "check"}],
    )
    result = LiveAgent(ScriptedClient([good])).run("writer", packet())
    assert result.claims[0].statement == "s"
    assert result.proposed_tasks[0].role == "reviewer"

    bad = envelope(claims=[{"statement": "s", "bogus": 1}])
    result = LiveAgent(ScriptedClient([bad, bad])).run("writer", packet())
    assert result.outcome == "unable_to_complete"
    assert result.model_calls == 2


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


def test_from_env_missing_key(monkeypatch):
    monkeypatch.delenv("AR_API_KEY", raising=False)
    with pytest.raises(AdapterError, match="AR_API_KEY"):
        OpenAICompatClient.from_env()


def test_from_env_reads_settings(monkeypatch):
    monkeypatch.setenv("AR_API_KEY", "k")
    monkeypatch.setenv("AR_MODEL", "m")
    monkeypatch.delenv("AR_BASE_URL", raising=False)
    client = OpenAICompatClient.from_env()
    assert client.base_url == adapter.DEFAULT_BASE_URL
    assert client.model == "m"


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_request_shape(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(adapter.urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatClient("http://host/v1/", "secret", "mod", timeout=5)
    assert client.complete("sys", "usr") == "{}"
    assert seen["url"] == "http://host/v1/chat/completions"
    assert seen["method"] == "POST"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["timeout"] == 5
    body = seen["body"]
    assert body["model"] == "mod"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


def test_http_error_wrapped_without_key(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

    monkeypatch.setattr(adapter.urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatClient("http://host/v1", "secret", "mod")
    with pytest.raises(AdapterError) as info:
        client.complete("s", "u")
    assert "401" in str(info.value)
    assert "secret" not in str(info.value)
