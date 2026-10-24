"""Codex is a callable coding specialist, never the research or Studio runtime."""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ar import cli, codex_runtime, db
from ar.coding_agent import CodingAgent
from ar.studio import server
from ar.studio.intake import Brief
from ar.studio.server import Studio, handle
from ar.studio.session import Session


@pytest.fixture(autouse=True)
def forbid_native_launch(monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("Research and Studio must not start native Codex")

    monkeypatch.setattr(codex_runtime, "codex_binary", forbidden)
    monkeypatch.setattr(codex_runtime.CodexRuntime, "__init__", forbidden)
    monkeypatch.setattr(codex_runtime.CodexRuntime, "run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setenv("STUDIO_PASSWORD", "")
    monkeypatch.delenv("VERCEL", raising=False)


def request(studio, method, path, body=None):
    status, content_type, payload = handle(studio, method, path, "", json.dumps(body or {}).encode())
    assert content_type == "application/json"
    return status, json.loads(payload)


@pytest.mark.parametrize("key, expected", [(None, None), ("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai")])
def test_studio_default_uses_available_api_provider_or_demo(tmp_path, monkeypatch, key, expected):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if key:
        monkeypatch.setenv(key, "fixture-token")
    calls = []
    client = object()
    monkeypatch.setattr(server, "make_client", lambda provider, model: calls.append((provider, model)) or client)
    studio = Studio(tmp_path)
    assert studio.provider == expected
    status, response = request(studio, "POST", "/api/session")
    assert status == 200
    session = studio.get(response["id"])
    assert getattr(session, "runtime_factory", None) is None
    if expected:
        assert response["mode"] == f"live · {expected}"
        assert session.client is client and session.make_agent is not None
        assert calls and all(provider == expected for provider, _ in calls)
    else:
        assert response["mode"] == "demo · fake agent"
        assert session.client is None and session.make_agent is None and calls == []


def test_studio_key_precedence_stays_openai_first(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-anthropic")
    assert Studio(tmp_path).provider == "openai"


def test_studio_rejects_codex_as_research_provider(tmp_path):
    with pytest.raises(ValueError, match="provider"):
        Studio(tmp_path, provider="codex")


def test_demo_runs_original_research_agent_pipeline_without_native_codex(tmp_path):
    studio = Studio(tmp_path, fake=True)
    session = studio.new_session()
    session.use_demo()
    session.start()
    session.wait(60)
    assert not session.running
    assert session.report is not None
    assert session.report.get("runtime") != "codex"
    assert (session.root / "run-1" / ".research" / "state.sqlite").is_file()
    assert not (session.root / "codex-project").exists()
    assert not any(card.get("role") in {"codex", "codex_delegate"} for card in session.cards.values())


@pytest.mark.parametrize("command", ["run", "resume"])
@pytest.mark.parametrize("fake", [False, True])
def test_research_cli_remains_agent_pipeline(tmp_path, monkeypatch, command, fake):
    db.init_project(tmp_path).close()
    selected, dispatched = [], []
    api_agent = object()
    monkeypatch.setattr(cli, "_live_agent", lambda provider, *args: selected.append(provider) or api_agent)
    monkeypatch.setattr(cli, "execute_project", lambda root, agent, **kwargs: dispatched.append((root, agent, kwargs)) or SimpleNamespace(value="completed"))
    result = CliRunner().invoke(cli.app, [command, str(tmp_path), *(["--fake"] if fake else [])])
    assert result.exit_code == 0, result.output
    assert len(dispatched) == 1 and "Status: completed" in result.output
    if fake:
        assert selected == [] and isinstance(dispatched[0][1], cli.FakeAgent)
    else:
        assert selected == ["anthropic"] and dispatched[0][1] is api_agent


@pytest.mark.parametrize("command", ["run", "resume"])
def test_research_cli_rejects_explicit_codex_provider(tmp_path, monkeypatch, command):
    db.init_project(tmp_path).close()
    called = []
    monkeypatch.setattr(cli, "execute_project", lambda *args, **kwargs: called.append(True))
    result = CliRunner().invoke(cli.app, [command, str(tmp_path), "--provider", "codex"])
    assert result.exit_code != 0 and "provider" in result.output.lower()
    assert called == []


def test_research_brief_and_demo_deliverable_stay_source_based(tmp_path):
    brief = Brief(question="Compare baselines", context="Forecasts", constraints=["Use seed 7"], open_questions=["Does it beat naive?"])
    expected = ("# Brief\n\nQuestion: Compare baselines Context: Forecasts\n"
                "Constraint: Use seed 7\nOpen question: Does it beat naive?\n"
                "Deliverable: A report that answers the question from the supplied sources, cites a source passage for every finding, names contradictions, and states limitations.\n")
    assert brief.to_markdown() == expected
    session = Session(tmp_path, None)
    session.intake.brief = brief
    project = session.root / "research-project"
    session._write_project(project)
    assert (project / "input" / "brief.md").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("previous_job", [None, "b" * 32])
def test_explicit_code_cli_dispatches_coding_specialist_only(tmp_path, monkeypatch, previous_job):
    calls = []

    def start(self, objective, **kwargs):
        calls.append((objective, kwargs))
        return {"job_id": "a" * 32, "status": "running"}

    monkeypatch.setattr(CodingAgent, "start", start)
    monkeypatch.setattr(CodingAgent, "wait", lambda self, job_id: {"status": "completed", "result": {"outcome": "completed", "problems": []}})
    monkeypatch.setattr(CodingAgent, "close", lambda self: None)
    arguments = ["code", "Implement baseline", "--project", str(tmp_path), "--input", "src", "--max-agents", "0"]
    if previous_job:
        arguments.extend(["--job-id", previous_job])
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "Implement baseline"
    assert calls[0][1]["input_paths"] == ["src"] and calls[0][1]["max_agents"] == 0
    assert calls[0][1]["job_id"] == previous_job
    assert "Coding job" in result.output and "Status: completed" in result.output
