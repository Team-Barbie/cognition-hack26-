"""Independent literature services use API decisions and never native Codex workers."""
import json
import io
import threading
from types import SimpleNamespace

import pytest

from ar import cli, db, literature_mcp
from ar.literature import LiteratureReviewAgent, ReviewSpec
from ar import literature_client
from ar.literature_mcp import handle
from ar.literature_service import LiteratureService
from tests.test_literature import Decisions, Search, Web
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def forbid_native_codex_in_literature(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Literature must use API decisions, never native Codex")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("ar.codex_runtime.CodexRuntime.__init__", forbidden)
    monkeypatch.setattr("ar.codex_runtime.CodexRuntime.run", forbidden)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_literature_client_uses_requested_api_provider(project, monkeypatch, provider):
    calls = []
    client = object()
    monkeypatch.setattr(literature_client, "load_env", lambda *args: None)
    monkeypatch.setattr(literature_client, "make_client", lambda *args: calls.append(args) or client)
    agent = literature_client.review_agent(project, provider=provider, model="chosen-model", base_url="https://fixture.invalid")
    assert agent.client is client
    assert calls == [(provider, "chosen-model", "medium", "https://fixture.invalid")]
    assert not hasattr(literature_client, "CodexDecisionClient")


def test_literature_default_is_api_provider_and_codex_is_rejected(project, monkeypatch):
    calls = []
    monkeypatch.setattr(literature_client, "load_env", lambda *args: None)
    monkeypatch.setattr(literature_client, "make_client", lambda *args: calls.append(args) or object())
    literature_client.review_agent(project)
    assert calls[0][0] in {"anthropic", "openai"}
    with pytest.raises((ValueError, RuntimeError), match="provider"):
        literature_client.review_agent(project, provider="codex")
    assert len(calls) == 1


def test_default_literature_service_constructs_api_decision_agent(project, monkeypatch):
    providers = []

    def api_agent(root, *, provider, **kwargs):
        providers.append(provider)
        assert provider in {"anthropic", "openai"}
        return Decisions()

    monkeypatch.setattr("ar.literature_service.review_agent", api_agent)
    service = LiteratureService(project, runner_factory=runner)
    job = service.start({"id": "api-only", "question": "Does treatment help?"})
    assert service.wait(job["job_id"], 60)["status"] == "finished"
    assert len(providers) == 1


def test_literature_cli_uses_api_provider_and_rejects_codex(project, monkeypatch):
    providers = []
    monkeypatch.setattr(literature_client, "review_agent", lambda root, **kwargs: providers.append(kwargs["provider"]) or Decisions())

    class Review:
        def __init__(self, *args):
            pass

        def run(self, spec):
            return SimpleNamespace(id=spec.id, status="finished", claim_ids=[], searches=0, model_calls=0,
                                   report_path="output/literature/report.md", stop_reason="finished")

    monkeypatch.setattr("ar.literature.LiteratureReviewAgent", Review)
    args = ["literature", str(project), "--question", "Does treatment help?"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert providers == ["anthropic"]
    rejected = CliRunner().invoke(cli.app, [*args, "--provider", "codex"])
    assert rejected.exit_code != 0 and "provider" in rejected.output.lower()
    assert providers == ["anthropic"]


def test_literature_mcp_defaults_to_api_provider(project, monkeypatch):
    configured = []
    monkeypatch.setattr(literature_mcp, "LiteratureService", lambda root, **kwargs: configured.append(kwargs))
    monkeypatch.setattr(literature_mcp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(literature_mcp.sys, "stdout", io.StringIO())
    literature_mcp.main([str(project)])
    assert configured[0]["provider"] == "anthropic"
    with pytest.raises(SystemExit) as exc:
        literature_mcp.main([str(project), "--provider", "codex"])
    assert exc.value.code == 2


@pytest.fixture
def project(tmp_path):
    db.init_project(tmp_path).close()
    (tmp_path / "input" / "brief.md").write_text("# Objective\nCompare the treatment evidence and build a baseline.\n")
    return tmp_path


def runner(project, agent, **kwargs):
    return LiteratureReviewAgent(project, agent, search=Search(), web=Web(), **kwargs)


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
