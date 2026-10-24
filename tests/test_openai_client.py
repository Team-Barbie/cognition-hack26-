import json
import urllib.error

import pytest

from ar import adapter, openai_client
from ar.adapter import AdapterError, AnthropicClient, make_client
from ar.openai_client import DEFAULT_BASE_URL, OpenAICompatClient


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_hosted_url_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(AdapterError, match="OPENAI_API_KEY"):
        OpenAICompatClient.from_env()


def test_local_server_needs_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = OpenAICompatClient.from_env(model="llama3.1", base_url="http://localhost:11434/v1/")
    assert client.base_url == "http://localhost:11434/v1" and client.model == "llama3.1"


def test_request_shape(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatClient("http://host/v1/", "secret", "mod")
    assert client.complete("sys", "usr") == "{}"
    assert seen["url"] == "http://host/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["body"]["model"] == "mod"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_http_error_wrapped_without_key(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AdapterError) as info:
        OpenAICompatClient("http://host/v1", "secret", "mod").complete("s", "u")
    assert "401" in str(info.value) and "secret" not in str(info.value)


def test_make_client_picks_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    claude = make_client("anthropic", effort="high")
    assert isinstance(claude, AnthropicClient) and claude.effort == "high"
    gpt = make_client("openai", model="gpt-4o-mini")
    assert isinstance(gpt, OpenAICompatClient) and gpt.model == "gpt-4o-mini"
    assert gpt.base_url == DEFAULT_BASE_URL


def test_make_client_unknown_provider():
    with pytest.raises(AdapterError, match="unknown provider"):
        make_client("gemini")
    assert adapter.PROVIDERS == ("anthropic", "openai")
