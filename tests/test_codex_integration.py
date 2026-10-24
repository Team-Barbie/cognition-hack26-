"""Studio and CLI route complete Codex jobs without a completion client or live model."""

import json
import threading

import pytest
from typer.testing import CliRunner

from ar import cli, codex_runtime
from ar.studio import server
from ar.studio.server import Studio, handle
from ar.studio.intake import Brief
from ar.studio.session import Session


@pytest.fixture
def runtime_stub(monkeypatch):
    instances = []

    class Runtime:
        block = False

        def __init__(self, model=None, *, effort="high", max_agents=3, timeout=1800):
            self.model, self.effort = model, effort
            self.max_agents, self.timeout = max_agents, timeout
            self.entered = threading.Event()
            self.release = threading.Event()
            self.cancelled = False
            self.follow_ups = []
            self.calls = []
            instances.append(self)

        def steer(self, message):
            self.follow_ups.append(message)

        def cancel(self):
            self.cancelled = True
            self.release.set()

        def run(self, workspace, state_path, prompt, instructions, schema, on_event):
            old = json.loads(state_path.read_text()) if state_path.exists() else {}
            thread_id = old.get("thread_id", f"fixture-thread-{len(instances)}")
            self.calls.append({"workspace": workspace, "state_path": state_path,
                               "thread_id": thread_id, "resumed": bool(old),
                               "brief": (workspace / "input" / "brief.md").read_text(encoding="utf-8"),
                               "inputs": sorted(str(path.relative_to(workspace)) for path in (workspace / "input").rglob("*") if path.is_file())})
            on_event("session.ready", {"threadId": thread_id, "resumed": bool(old)})
            on_event("item/started", {"threadId": thread_id, "item": {"type": "commandExecution", "command": "python baseline.py"}})
            self.entered.set()
            if self.block and not self.release.wait(5):
                raise AssertionError("Test did not release the running stub")
            status = "cancelled" if self.cancelled else "completed"
            on_event("item/completed", {"threadId": thread_id, "item": {"type": "commandExecution", "command": "python baseline.py", "exitCode": 0, "aggregatedOutput": "score=7"}})
            on_event("item/completed", {"threadId": thread_id, "item": {
                "type": "collabAgentToolCall", "tool": "wait", "receiverThreadIds": ["focused-delegate"],
                "agentsStates": {"focused-delegate": {"status": "completed", "message": "Checked baseline"}}}})
            state_path.write_text(json.dumps({"thread_id": thread_id, "status": status}), encoding="utf-8")
            return {"status": status, "thread_id": thread_id, "turn_id": "fixture-turn", "usage": {},
                    "text": json.dumps({"status": "completed", "summary": "Baseline tested", "report_markdown": "# Baseline\nScore: 7", "limitations": [], "checks": ["Baseline ran"], "artifacts": []})}

    monkeypatch.setattr(codex_runtime, "CodexRuntime", Runtime)
    monkeypatch.setenv("STUDIO_PASSWORD", "")
    monkeypatch.delenv("VERCEL", raising=False)

    def forbidden(*_, **__):
        raise AssertionError("Codex integration must not construct a legacy model API client")

    monkeypatch.setattr(server, "make_client", forbidden)
    monkeypatch.setattr(cli, "_live_agent", forbidden)
    return Runtime, instances


def request(studio, method, path, body=None):
    status, content_type, payload = handle(studio, method, path, "", json.dumps(body or {}).encode())
    assert content_type == "application/json"
    return status, json.loads(payload)


def new_session(studio):
    status, response = request(studio, "POST", "/api/session")
    assert status == 200
    return studio.get(response["id"]), response


def test_studio_defaults_to_codex_without_legacy_client(tmp_path, runtime_stub):
    studio = Studio(tmp_path)
    assert studio.provider == "codex"
    assert studio.mode == "live · codex"
    assert studio._client() is None
    session, response = new_session(studio)
    assert response["mode"] == "live · codex"
    assert session.runtime_factory is not None
    assert session.client is None and session.make_agent is None
    assert not any("demo:" in event.get("text", "").lower() for event in session.events)
    status, info = request(studio, "GET", "/api/info")
    assert status == 200 and info["mode"] == "live · codex"


@pytest.mark.parametrize("upload", [False, True])
def test_studio_starts_with_question_and_optional_custom_sources(tmp_path, runtime_stub, upload):
    _, instances = runtime_stub
    studio = Studio(tmp_path)
    session, _ = new_session(studio)
    status, _ = request(studio, "POST", "/api/chat", {"id": session.id, "message": "Build and evaluate a baseline"})
    assert status == 200
    if upload:
        status, result = request(studio, "POST", "/api/source", {"id": session.id, "name": "My data.md", "text": "Custom observations: 2, 3, 5"})
        assert status == 200 and result["id"] == "my-data"
    status, _ = request(studio, "POST", "/api/run", {"id": session.id})
    assert status == 200
    session.wait(5)
    assert not session.running
    assert session.report["runtime"] == "codex" and session.report["outcome"] == "completed"
    assert session.report["executions"][0]["command"] == "python baseline.py"
    assert session.report["claims"] == []
    assert session.report["checks"] == ["Baseline ran"]
    assert set(session.cards) == {"codex", "codex:focused-delegate"}
    assert session.cards["codex"]["status"] == "done"
    assert session.cards["codex:focused-delegate"]["status"] == "done"
    call = instances[0].calls[0]
    assert "research or engineering objective" in call["brief"]
    if upload:
        assert any("my-data.md" in path for path in call["inputs"])
    else:
        assert session.sources == {}
        assert not (call["workspace"] / "input" / "literature.yaml").exists()
        assert len(call["inputs"]) == 1
    assert not (session.root / "run-1").exists()


def test_native_brief_allows_engineering_and_legacy_demo_brief_is_unchanged(tmp_path):
    brief = Brief(question="Build a baseline", context="Demand forecasts",
                  constraints=["Use seed 7"], open_questions=["Does it beat naive?"])
    common = ("# Brief\n\nQuestion: Build a baseline Context: Demand forecasts\n"
              "Constraint: Use seed 7\nOpen question: Does it beat naive?\n")
    legacy = common + ("Deliverable: A report that answers the question from the supplied sources, "
                       "cites a source passage for every finding, names contradictions, and states limitations.\n")
    assert brief.to_markdown() == legacy
    native = brief.to_markdown(native_codex=True)
    assert native.startswith(common)
    assert "code, builds, simulations or tests when needed" in native
    session = Session(tmp_path, None)
    session.intake.brief = brief
    project = session.root / "legacy-project"
    session._write_project(project)
    assert (project / "input" / "brief.md").read_text(encoding="utf-8") == legacy


def test_rerun_reuses_project_and_thread_and_refreshes_inputs(tmp_path, runtime_stub):
    _, instances = runtime_stub
    studio = Studio(tmp_path)
    session, _ = new_session(studio)
    session.chat("Evaluate a baseline")
    session.add_source("Old.md", "old evidence")
    session.start()
    session.wait(5)
    first = instances[0].calls[0]
    session.chat("Now compare the stronger baseline")
    session.remove_source("old")
    session.add_source("New.md", "new evidence")
    session.start()
    session.wait(5)
    second = instances[1].calls[0]
    assert first["workspace"] == second["workspace"]
    assert first["state_path"] == second["state_path"]
    assert first["thread_id"] == second["thread_id"]
    assert second["resumed"] is True
    assert "stronger baseline" in second["brief"]
    assert not any("old.md" in path for path in second["inputs"])
    assert any("new.md" in path for path in second["inputs"])


def test_live_chat_steers_running_session_and_stop_endpoint_interrupts(tmp_path, runtime_stub):
    runtime_class, instances = runtime_stub
    runtime_class.block = True
    studio = Studio(tmp_path)
    session, _ = new_session(studio)
    session.chat("Evaluate baseline")
    session.start()
    runtime = instances[0]
    try:
        assert runtime.entered.wait(5)
        status, _ = request(studio, "POST", "/api/chat", {"id": session.id, "message": "Include the empty input case"})
        assert status == 200
        assert runtime.follow_ups == ["Include the empty input case"]
        status, _ = request(studio, "POST", "/api/stop", {"id": session.id})
        assert status == 200
        assert runtime.cancelled
    finally:
        runtime.release.set()
        session.wait(5)
    assert session.report["outcome"] == "cancelled"
    assert session.cards["codex"]["status"] == "failed"
    status, error = request(studio, "POST", "/api/stop", {"id": session.id})
    assert status == 400 and "No Codex run is active" in error["error"]


def test_stop_endpoint_requires_existing_session(tmp_path, runtime_stub):
    studio = Studio(tmp_path)
    status, error = request(studio, "POST", "/api/stop", {"id": "missing"})
    assert status == 400 and "unknown session" in error["error"]


@pytest.mark.parametrize("args, expected", [
    ([], {"model": None, "effort": "medium", "max_agents": 3, "timeout": 1800}),
    (["--model", "chosen-model", "--effort", "high", "--max-agents", "0", "--timeout", "60"],
     {"model": "chosen-model", "effort": "high", "max_agents": 0, "timeout": 60}),
])
def test_cli_run_defaults_to_codex_and_preserves_delegation_options(tmp_path, runtime_stub, args, expected):
    _, instances = runtime_stub
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "brief.md").write_text("# Objective\nBuild and test a baseline", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), *args])
    assert result.exit_code == 0, result.output
    assert "Codex session:" in result.output and "Status: completed" in result.output
    runtime = instances[0]
    for name, value in expected.items():
        assert getattr(runtime, name) == value
    assert (tmp_path / "output" / "codex-result.json").is_file()
    assert json.loads((tmp_path / "output" / "provenance.json").read_text())["runtime"] == "codex"
