"""Result/provenance and artifact boundaries using a deterministic runtime stub."""

import hashlib
import json
import os

import pytest

from ar import codex_research
from ar.codex_research import _capture_artifacts, run_research, stage_inputs
from ar.codex_runtime import CodexError


@pytest.fixture
def project(tmp_path):
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "brief.md").write_text("# Objective\nImplement and test a baseline.\n", encoding="utf-8")
    (tmp_path / "input" / "source.txt").write_text("Evidence from the user\n", encoding="utf-8")
    return tmp_path


class ResearchRuntime:
    max_agents = 2

    def __init__(self, *, text=None, status="completed", artifacts=None, error=None, permission_blocked=False):
        self.text = text
        self.status = status
        self.artifacts = artifacts or []
        self.error = error
        self.permission_blocked = permission_blocked
        self.calls = []

    def run(self, workspace, state_path, prompt, instructions, schema, on_event):
        self.calls.append((workspace, state_path, prompt, instructions, schema))
        assert (workspace / "input" / "brief.md").is_file()
        (workspace / "output" / "result.txt").write_bytes(b"measured result: 7\n")
        on_event("item/completed", {"threadId": "delegate-1", "item": {"type": "commandExecution", "command": "python simulate.py", "exitCode": 0, "aggregatedOutput": "metric=7"}})
        text = self.text if self.text is not None else json.dumps({
            "status": "completed", "summary": "Baseline delivered", "report_markdown": "# Findings\nMetric was 7.",
            "limitations": ["Small fixture"], "checks": ["Agent says baseline test passed"], "artifacts": self.artifacts,
        })
        return {"status": self.status, "thread_id": "lead-1", "turn_id": "turn-1", "text": text,
                "usage": {"total": {"totalTokens": 10}}, "error": self.error,
                "permission_blocked": self.permission_blocked}


def test_report_captures_actual_executions_artifacts_and_distinct_provenance(project):
    runtime = ResearchRuntime(artifacts=["output/result.txt", "output/result.txt"])
    observed = []
    report = run_research(project, runtime, on_event=lambda *event: observed.append(event))
    assert report["runtime"] == "codex" and report["outcome"] == "completed"
    assert report["checks"] == ["Agent says baseline test passed"]
    assert report["claims"] == [] and report["cited"] == []
    assert report["executions"][0]["thread_id"] == "delegate-1"
    assert report["executions"][0]["exitCode"] == 0
    assert len(report["artifacts"]) == 1
    artifact = report["artifacts"][0]
    content = (project / "output" / artifact["captured_path"]).read_bytes()
    assert content == b"measured result: 7\n"
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert artifact["bytes"] == len(content)
    assert (project / "output" / "report.md").read_text(encoding="utf-8") == report["markdown"]
    provenance = json.loads((project / "output" / "provenance.json").read_text())
    assert provenance["checks_provenance"] == "model_reported"
    assert provenance["report_sha256"] == hashlib.sha256((project / "output" / "report.md").read_bytes()).hexdigest()
    assert provenance["executions"] == report["executions"]
    assert provenance["thread_id"] == "lead-1"
    journal = [json.loads(line) for line in (project / ".research" / "codex-events.jsonl").read_text().splitlines()]
    assert journal[0]["event"] == "item/completed"
    assert observed[0][0] == "item/completed"
    assert not (project / ".research" / "codex.lock").exists()
    assert "Current input manifest" in runtime.calls[0][2]
    assert runtime.calls[0][4]["additionalProperties"] is False


@pytest.mark.parametrize("text", [
    "not JSON", "[]", '{"status":"completed"}',
    json.dumps({"status": "completed", "summary": "x", "report_markdown": "x", "limitations": [], "checks": [], "artifacts": [], "invented_field": True}),
])
def test_malformed_or_schema_invalid_final_output_fails(project, text):
    report = run_research(project, ResearchRuntime(text=text))
    assert report["outcome"] == "failed"
    assert any("valid research result" in problem for problem in report["problems"])
    assert report["artifacts"] == []
    assert not (project / ".research" / "codex.lock").exists()


@pytest.mark.parametrize("artifact", ["../secret.txt", "output/missing.txt", "output", "/absolute.txt", "C:/Windows/win.ini"])
def test_missing_directory_and_traversing_deliverables_block_completion(project, artifact):
    report = run_research(project, ResearchRuntime(artifacts=[artifact]))
    assert report["outcome"] == "blocked"
    assert report["artifacts"] == []
    assert any("missing or outside" in problem for problem in report["problems"])


def test_empty_report_and_permission_denial_cannot_claim_completion(project):
    empty = json.dumps({"status": "completed", "summary": "done", "report_markdown": "  ", "limitations": [], "checks": [], "artifacts": []})
    assert run_research(project, ResearchRuntime(text=empty))["outcome"] == "blocked"
    report = run_research(project, ResearchRuntime(permission_blocked=True))
    assert report["outcome"] == "blocked"
    assert any("permission" in line for line in report["limitations"])


@pytest.mark.parametrize("status, expected", [("cancelled", "cancelled"), ("interrupted", "cancelled"), ("timed_out", "timed_out"), ("failed", "failed")])
def test_incomplete_status_and_error_are_preserved(project, status, expected):
    report = run_research(project, ResearchRuntime(status=status, error={"message": "upstream stopped"}))
    assert report["outcome"] == expected
    assert any("upstream stopped" in problem for problem in report["problems"])
    if expected in {"cancelled", "timed_out"}:
        assert any("Resume" in line for line in report["limitations"])


def test_environment_block_is_persisted_with_original_command_logs(project):
    output = "windows sandbox: CreateProcess failed with code 5: Access is denied"

    class EnvironmentFailure:
        max_agents = 0

        def run(self, workspace, state_path, prompt, instructions, schema, on_event):
            on_event("item/completed", {"threadId": "lead-1", "item": {
                "type": "commandExecution", "command": "python baseline.py", "exitCode": -1,
                "aggregatedOutput": output}})
            return {"status": "blocked", "thread_id": "lead-1", "text": "", "error": output}

    report = run_research(project, EnvironmentFailure())
    assert report["outcome"] == "blocked"
    assert report["problems"] == [output]
    assert report["executions"][0]["aggregatedOutput"] == output
    journal = [json.loads(line) for line in (project / ".research" / "codex-events.jsonl").read_text().splitlines()]
    assert journal[0]["payload"]["item"]["exitCode"] == -1
    persisted = json.loads((project / "output" / "codex-result.json").read_text())
    provenance = json.loads((project / "output" / "provenance.json").read_text())
    assert persisted["outcome"] == "blocked"
    assert provenance["executions"][0]["aggregatedOutput"] == output
    assert output in (project / "output" / "run-summary.md").read_text()


def test_input_manifest_refresh_removes_deleted_sources(project):
    workspace = project / "worker"
    first = stage_inputs(project, workspace)
    assert len(first) == 2
    assert {row["path"] for row in first} == {"input/brief.md", "input/source.txt"}
    (project / "input" / "source.txt").unlink()
    (project / "input" / "brief.md").write_text("Revised objective", encoding="utf-8")
    second = stage_inputs(project, workspace)
    assert len(second) == 1
    assert (workspace / "input" / "brief.md").read_text() == "Revised objective"
    assert not (workspace / "input" / "source.txt").exists()
    assert first[0]["sha256"] != second[0]["sha256"]


def symlink_or_skip(target, link, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")


def test_symlink_artifacts_are_never_captured(project):
    workspace = project / "workspace"
    destination = project / "captured"
    workspace.mkdir()
    destination.mkdir()
    source = workspace / "real.txt"
    source.write_text("real output")
    symlink_or_skip(source, workspace / "alias.txt")
    artifacts, problems = _capture_artifacts(workspace, destination, ["alias.txt"])
    assert artifacts == []
    assert len(problems) == 1
    assert list(destination.iterdir()) == []


def test_symlink_parent_cannot_escape_artifact_workspace(project):
    workspace = project / "workspace"
    outside = project / "outside"
    destination = project / "captured"
    workspace.mkdir()
    outside.mkdir()
    destination.mkdir()
    (outside / "secret.txt").write_text("not a deliverable")
    symlink_or_skip(outside, workspace / "escape", directory=True)
    artifacts, problems = _capture_artifacts(workspace, destination, ["escape/secret.txt"])
    assert artifacts == [] and problems


def test_oversized_artifact_is_a_reported_failure(project, monkeypatch):
    workspace = project / "workspace"
    destination = project / "captured"
    workspace.mkdir()
    destination.mkdir()
    (workspace / "large.txt").write_text("123456789")
    monkeypatch.setattr(codex_research, "MAX_ARTIFACT_BYTES", 4)
    artifacts, problems = _capture_artifacts(workspace, destination, ["large.txt"])
    assert artifacts == []
    assert "capture limit" in problems[0]


def test_input_source_symlink_is_rejected(project):
    outside = project / "outside.txt"
    outside.write_text("external")
    symlink_or_skip(outside, project / "input" / "alias.txt")
    with pytest.raises(CodexError, match="Symlink input"):
        stage_inputs(project, project / "workspace")


def test_staging_root_symlink_cannot_redirect_input_writes(project):
    workspace = project / "workspace"
    outside = project / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("preserve this file")
    symlink_or_skip(outside, workspace / "input", directory=True)
    with pytest.raises(CodexError, match="escapes the workspace"):
        stage_inputs(project, workspace)
    assert not (outside / "brief.md").exists()
    assert (outside / "keep.txt").read_text() == "preserve this file"


def test_existing_lock_prevents_second_run(project):
    metadata = project / ".research"
    metadata.mkdir()
    (metadata / "codex.lock").write_text("another process owns this")
    runtime = ResearchRuntime()
    with pytest.raises(CodexError, match="already holds"):
        run_research(project, runtime)
    assert runtime.calls == []
    assert (metadata / "codex.lock").read_text() == "another process owns this"


def test_transport_error_releases_lock_and_does_not_publish_result(project):
    class BrokenRuntime:
        max_agents = 0

        def run(self, *_):
            raise CodexError("server unavailable")

    with pytest.raises(CodexError, match="server unavailable"):
        run_research(project, BrokenRuntime())
    assert not (project / ".research" / "codex.lock").exists()
    assert not (project / "output" / "codex-result.json").exists()
