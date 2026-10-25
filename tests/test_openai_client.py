import json
import shutil
import urllib.error
from pathlib import Path

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
    assert "reasoning_effort" not in seen["body"]


def test_explicit_test_reasoning_is_sent_without_changing_normal_requests(monkeypatch):
    bodies = []

    def urlopen(request, timeout=None):
        bodies.append(json.loads(request.data))
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", urlopen)
    OpenAICompatClient("http://host/v1", "key", "gpt-5.6-terra", reasoning_effort="low").complete("JSON", "JSON")
    OpenAICompatClient("http://host/v1", "key", "gpt-5.6-sol").complete("JSON", "JSON")
    assert bodies[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies[1]


def test_http_error_wrapped_without_key(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AdapterError) as info:
        OpenAICompatClient("http://host/v1", "secret", "mod").complete("s", "u")
    assert "401" in str(info.value) and "secret" not in str(info.value)


def test_transient_timeout_is_retried_once_with_the_configured_bound(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(openai_client.time, "sleep", lambda _: None)
    client = OpenAICompatClient("http://host/v1", "secret", "mod", timeout=17)
    assert client.complete("sys", "usr") == "{}"
    assert calls == [17, 17]


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


def test_cli_run_accepts_provider_openai(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from ar import cli
    from ar.agents.fake import FakeAgent

    seen = {}
    monkeypatch.setattr(cli, "make_client", lambda *a: seen.update(args=a) or object())
    monkeypatch.setattr(cli, "LiveAgent", lambda client: FakeAgent())
    shutil.copytree(Path(__file__).parent.parent / "fixtures" / "baselines", tmp_path / "p")
    result = CliRunner().invoke(
        cli.app, ["run", str(tmp_path / "p"), "--provider", "openai", "--model", "gpt-4o-mini"]
    )
    assert result.exit_code == 0, result.output
    assert seen["args"] == ("openai", "gpt-4o-mini", "medium", None)
    bad = CliRunner().invoke(cli.app, ["run", str(tmp_path / "p"), "--provider", "gemini"])
    assert bad.exit_code == 2


def test_hosted_calls_verify_tls_with_the_os_trust_store(monkeypatch):
    installed = []
    monkeypatch.setattr(openai_client, "_tls_installed", False)
    monkeypatch.setattr(openai_client.urllib.request, "install_opener", installed.append)

    def fake_urlopen(request, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(openai_client.urllib.request, "urlopen", fake_urlopen)
    client = openai_client.OpenAICompatClient(api_key="k")
    for _ in range(2):
        with pytest.raises(AdapterError):
            client.complete("s", "u")
    assert len(installed) == 1  # installed once, then reused
    assert client.model == "gpt-5.6-terra"
