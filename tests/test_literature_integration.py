"""The literature specialist composes with Codex without replacing its lead runtime."""
import json
import threading
from pathlib import Path

import pytest
import yaml

from ar import db
from ar.codex_research import run_research
from ar.literature import LiteratureReviewAgent, ReviewSpec
from ar.literature_client import CodexDecisionClient
from ar.literature_mcp import handle
from ar.literature_service import LiteratureService
from tests.test_codex_research import ResearchRuntime
from tests.test_literature import Decisions, Search, Web


@pytest.fixture
def project(tmp_path):
    db.init_project(tmp_path).close()
    (tmp_path / "input" / "brief.md").write_text("# Objective\nCompare the treatment evidence and build a baseline.\n")
    return tmp_path


def runner(project, agent, **kwargs):
    return LiteratureReviewAgent(project, agent, search=Search(), web=Web(), **kwargs)


def test_native_lead_receives_two_verified_specialists_and_keeps_its_tools(project):
    specs = [ReviewSpec(id=name, question="Does treatment help?") for name in ("methods", "limitations")]
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [s.model_dump() for s in specs]}))
    native = ResearchRuntime(artifacts=["output/result.txt"])
    report = run_research(project, native, literature_runner=runner(project, Decisions()))
    assert report["outcome"] == "completed"
    assert len(native.calls) == 1 and native.max_agents == 2
    assert "native tools remain" in native.calls[0][2]
    assert len(report["literature_reviews"]) == 2
    assert all(review["claim_ids"] for review in report["literature_reviews"])
    assert report["executions"][0]["command"] == "python simulate.py"
    assert report["artifacts"][0]["path"] == "output/result.txt"
    staged = list((native.calls[0][0] / "literature-evidence").iterdir())
    assert len(staged) == 4
    manifest = json.loads((project / ".research" / "codex-inputs.json").read_text())["inputs"]
    assert len([item for item in manifest if item["path"].startswith("literature-evidence/")]) == 4
    provenance = json.loads((project / "output" / "provenance.json").read_text())
    assert provenance["literature_reviews"] == report["literature_reviews"]
    # Removed reviews cannot leave stale evidence in the resumed native workspace.
    (project / "input" / "literature.yaml").unlink()
    assert run_research(project, native)["literature_reviews"] == []
    assert not list((native.calls[-1][0] / "literature-evidence").iterdir())


def test_partial_specialist_cannot_be_hidden_by_native_completion(project):
    spec = ReviewSpec(id="bounded", question="Does treatment help?", max_model_calls=0)
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [spec.model_dump()]}))
    result = run_research(project, ResearchRuntime(), literature_runner=runner(project, Decisions()))
    assert result["outcome"] == "blocked"
    assert result["literature_reviews"][0]["status"] == "partial"
    assert any("bounded is partial" in message for message in result["limitations"])


def test_cancelled_native_run_does_not_start_specialists_or_lead(project):
    spec = ReviewSpec(id="methods", question="Does treatment help?")
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [spec.model_dump()]}))
    native = ResearchRuntime()
    native.cancelled = threading.Event()
    native.cancelled.set()
    result = run_research(project, native, literature_runner=runner(project, Decisions()))
    assert result["outcome"] == "cancelled" and not native.calls
    assert result["literature_reviews"] == []
    assert not (project / ".research" / "codex.lock").exists()


def test_cli_pause_signal_interrupts_literature_before_native_lead(project, monkeypatch):
    spec = ReviewSpec(id="methods", question="Does treatment help?")
    (project / "input" / "literature.yaml").write_text(yaml.safe_dump({"reviews": [spec.model_dump()]}))
    monkeypatch.setattr("ar.literature_client.review_agent", lambda *args, **kwargs: Decisions())
    native = ResearchRuntime()
    def pause(method, event):
        if method == "literature.review":
            (project / ".research" / "codex-stop").touch()
    result = run_research(project, native, on_event=pause)
    assert result["outcome"] == "cancelled" and not native.calls
    with db.connect(project) as conn:
        assert conn.execute("SELECT status FROM literature_jobs WHERE key=?", (spec.key,)).fetchone()[0] == "interrupted"


def test_review_cancellation_preserves_completed_operations_for_resume(project):
    stop = threading.Event()
    agent, search = Decisions(), Search()
    spec = ReviewSpec(id="resumable", question="Does treatment help?")
    reviewer = LiteratureReviewAgent(project, agent, search=search, web=Web(), should_stop=stop.is_set,
                                    on_event=lambda event: stop.set() if event["phase"] == "search" else None)
    with pytest.raises(InterruptedError):
        reviewer.run(spec)
    with db.connect(project) as conn:
        row = conn.execute("SELECT status,owner,calls,searches FROM literature_jobs WHERE key=?", (spec.key,)).fetchone()
        assert tuple(row) == ("interrupted", None, 1, 1)
    stop.clear()
    result = LiteratureReviewAgent(project, agent, search=search, web=Web()).run(spec)
    assert result.status == "finished" and result.model_calls == 5
    assert len(search.calls) == 2  # The first completed search was replayed without repeating network work.


def test_nonblocking_service_runs_separate_jobs_and_scopes_event_cursors(project):
    service = LiteratureService(project, agent_factory=Decisions, runner_factory=runner)
    jobs = [service.start({"id": name, "question": "Does treatment help?"}) for name in ("methods", "safety")]
    assert jobs[0]["job_id"] != jobs[1]["job_id"]
    results = [service.wait(job["job_id"], 60) for job in jobs]
    assert all(result["status"] == "finished" for result in results), results
    assert len(service.list()["jobs"]) == 2
    for result in results:
        assert result["result"]["claim_ids"]
        assert all(event["key"] == result["job_id"] for event in result["events"])
        assert not service.status(result["job_id"], after=result["cursor"])["events"]
    restored = LiteratureService(project, agent_factory=Decisions, runner_factory=runner)
    assert restored.status(jobs[0]["job_id"])["result"] == results[0]["result"]


def test_mcp_discovers_specialist_and_validates_protocol_before_dispatch(project):
    service = LiteratureService(project, agent_factory=Decisions, runner_factory=runner)
    def call(method, **params):
        return handle(service, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert call("initialize", protocolVersion="2025-06-18")["result"]["serverInfo"]["name"] == "ar-literature-review"
    tools = call("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"literature.start", "literature.status", "literature.list"}
    assert call("tools/call", name="literature.start", arguments={"spec": {"id": "../bad", "question": "q"}})["result"]["isError"]
    response = call("tools/call", name="literature.start", arguments={"spec": {"id": "methods", "question": "Does treatment help?"}})
    job = response["result"]["structuredContent"]["job_id"]
    assert service.wait(job, 60)["result"]["claim_ids"]
    assert call("tools/call", name="literature.status", arguments={"job_id": job})["result"]["structuredContent"]["status"] == "finished"
    assert handle(service, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []})["error"]["code"] == -32602


def test_codex_decisions_are_stateless_and_do_not_inherit_user_tool_configuration(monkeypatch):
    commands = []
    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            self.command = command
            commands.append(command)
            assert kwargs["encoding"] == "utf-8"
        def communicate(self, prompt, **kwargs):
            assert "Do not call tools" in prompt and "supplied packet" in prompt
            Path(self.command[self.command.index("-o") + 1]).write_text('{"payload":{}}')
            return '{"type":"item.completed","item":{"type":"agent_message"}}\n', ""
    monkeypatch.setattr("ar.literature_client.codex_binary", lambda: "fixture-codex")
    monkeypatch.setattr("ar.literature_client.subprocess.Popen", Process)
    assert CodexDecisionClient("explicit-model").complete("instructions", "supplied packet") == '{"payload":{}}'
    command = commands[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert "features.shell_tool=false" in command and "features.multi_agent=false" in command
    assert 'web_search="disabled"' in command and "explicit-model" in command


def test_service_capacity_and_unknown_job_errors_are_explicit(project):
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def __init__(self, *args): pass
        def run(self, spec):
            entered.set()
            release.wait(5)
    service = LiteratureService(project, agent_factory=Decisions, runner_factory=Blocking, max_parallel=1)
    first = service.start({"id": "one", "question": "First question?"})
    try:
        assert entered.wait(5)
        assert service.status(first["job_id"])["status"] == "starting"
        with pytest.raises(RuntimeError, match="capacity"):
            service.start({"id": "two", "question": "Second question?"})
        assert service.start({"id": "one", "question": "First question?"}) == first
        with pytest.raises(ValueError, match="unknown"):
            service.status("unknown")
    finally:
        release.set()
