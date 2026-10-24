"""Callable coding worker tests with deterministic native-runtime substitutes."""

import json
import os
import threading

import pytest

from ar.coding_agent import CodingAgent


@pytest.fixture
def worker(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('baseline')\n", encoding="utf-8")
    instances = []

    class Runtime:
        block = False
        fail = False

        def __init__(self, model=None, *, effort="high", max_agents=3, timeout=1800):
            self.model, self.effort, self.max_agents, self.timeout = model, effort, max_agents, timeout
            self.entered, self.release = threading.Event(), threading.Event()
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
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            tid = state.get("thread_id", f"native-{len(instances)}")
            self.calls.append({"workspace": workspace, "state_path": state_path, "thread_id": tid,
                               "inputs": {path.relative_to(workspace / "input").as_posix(): path.read_text(encoding="utf-8")
                                          for path in (workspace / "input").rglob("*") if path.is_file()}})
            self.entered.set()
            if self.block and not self.release.wait(5):
                raise AssertionError("Test did not release blocked runtime")
            if self.fail:
                raise RuntimeError("fake native process failed")
            on_event("session.ready", {"threadId": tid, "resumed": bool(state)})
            on_event("item/completed", {"threadId": tid, "item": {"type": "commandExecution", "command": "pytest", "exitCode": 0, "aggregatedOutput": "1 passed"}})
            (workspace / "output" / "change.patch").write_bytes(b"+print('updated')\n")
            status = "cancelled" if self.cancelled else "completed"
            state_path.write_text(json.dumps({"thread_id": tid, "status": status}), encoding="utf-8")
            return {"status": status, "thread_id": tid, "turn_id": "turn-1", "usage": {},
                    "text": json.dumps({"status": "completed", "summary": "Patch delivered", "report_markdown": "# Change\nTests passed", "limitations": [], "checks": ["pytest passed"], "artifacts": ["output/change.patch"]})}

    agent = CodingAgent(tmp_path, runtime_factory=Runtime)
    yield agent, Runtime, instances
    agent.close()


def test_start_returns_immediately_with_pollable_result_and_artifacts(worker, tmp_path):
    agent, runtime_class, instances = worker
    runtime_class.block = True
    started = agent.start("Improve baseline and test it", input_paths=["src"], model="chosen", max_agents=1, timeout=90)
    assert started["status"] == "running" and len(started["job_id"]) == 32
    runtime = instances[0]
    assert runtime.entered.wait(5)
    interim = agent.status(started["job_id"])
    assert interim["status"] == "running" and "result" not in interim
    assert runtime.model == "chosen" and runtime.max_agents == 1 and runtime.timeout == 90
    runtime.release.set()
    final = agent.wait(started["job_id"], 5)
    assert final["status"] == "completed"
    assert final["result"]["thread_id"] == "native-1"
    assert final["result"]["artifacts"][0]["sha256"]
    assert [event["event"] for event in final["events"]] == ["session.ready", "item/completed"]
    assert runtime.calls[0]["inputs"]["project/src/main.py"] == "print('baseline')\n"
    assert "Improve baseline" in runtime.calls[0]["inputs"]["brief.md"]
    assert (tmp_path / "src" / "main.py").read_text() == "print('baseline')\n"
    assert "result" not in agent.status(started["job_id"], include_result=False)


def test_resume_same_job_uses_same_native_thread_and_workspace(worker):
    agent, _, instances = worker
    job_id = agent.start("First objective", input_paths=["src"])["job_id"]
    assert agent.wait(job_id, 5)["status"] == "completed"
    assert agent.start("Follow-up objective", job_id=job_id, input_paths=["src"])["job_id"] == job_id
    second = agent.wait(job_id, 5)
    assert second["status"] == "completed"
    first_call, second_call = instances[0].calls[0], instances[1].calls[0]
    assert first_call["state_path"] == second_call["state_path"]
    assert first_call["workspace"] == second_call["workspace"]
    assert first_call["thread_id"] == second_call["thread_id"]
    assert "Follow-up objective" in second_call["inputs"]["brief.md"]
    assert second["events"][2]["payload"]["resumed"] is True


def test_live_steering_cancel_and_duplicate_start_protection(worker):
    agent, runtime_class, instances = worker
    runtime_class.block = True
    job_id = agent.start("Implement baseline", input_paths=[])["job_id"]
    runtime = instances[0]
    assert runtime.entered.wait(5)
    assert agent.steer(job_id, "Test empty inputs")["status"] == "follow_up_queued"
    assert runtime.follow_ups == ["Test empty inputs"]
    with pytest.raises(RuntimeError, match="running"):
        agent.start("Duplicate", job_id=job_id)
    assert agent.cancel(job_id)["status"] == "cancellation_requested"
    assert agent.wait(job_id, 5)["status"] == "cancelled"
    with pytest.raises(RuntimeError, match="No active worker"):
        agent.steer(job_id, "Too late")
    with pytest.raises(RuntimeError, match="No active worker"):
        agent.cancel(job_id)


def test_event_cursor_pages_and_partial_writer_line_are_recoverable(worker):
    agent, _, _ = worker
    job_id = agent.start("Task", input_paths=[])["job_id"]
    finished = agent.wait(job_id, 5)
    assert len(finished["events"]) == 2
    assert agent.status(job_id, after=finished["cursor"])["events"] == []
    journal = agent._job(job_id) / ".research" / "codex-events.jsonl"
    with journal.open("a", encoding="utf-8") as stream:
        for number in range(105):
            stream.write(json.dumps({"event": "step", "payload": {"number": number}}) + "\n")
        stream.write('{"event":')
    page = agent.status(job_id, after=2)
    assert len(page["events"]) == 100 and page["cursor"] == 102
    next_page = agent.status(job_id, after=page["cursor"])
    assert len(next_page["events"]) == 5 and next_page["cursor"] == 107
    with journal.open("a", encoding="utf-8") as stream:
        stream.write('"last","payload":{}}\n')
    assert agent.status(job_id, after=107)["events"][0]["event"] == "last"
    with pytest.raises(ValueError, match="non-negative"):
        agent.status(job_id, after=-1)


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "C:/Windows/win.ini", ".research/private", ".git/config", ".venv/python", "missing.py"])
def test_unsafe_or_missing_inputs_fail_before_runtime_dispatch(worker, path):
    agent, _, instances = worker
    with pytest.raises(ValueError):
        agent.start("Read source", input_paths=[path])
    assert all(instance.calls == [] for instance in instances)


def test_directory_staging_excludes_runtime_metadata_and_environment_files(worker, tmp_path):
    agent, _, instances = worker
    for directory in ["node_modules", ".git", ".venv"]:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "private.txt").write_text("do not stage")
    (tmp_path / ".env").write_text("DUMMY_TOKEN=private")
    (tmp_path / ".env.local").write_text("DUMMY_TOKEN=private")
    job_id = agent.start("Inspect project", input_paths=["."])["job_id"]
    assert agent.wait(job_id, 5)["status"] == "completed"
    staged = instances[0].calls[0]["inputs"]
    assert "project/src/main.py" in staged
    assert not any("private.txt" in path or ".env" in path or ".research" in path for path in staged)


def test_input_symlinks_are_rejected(worker, tmp_path):
    agent, _, _ = worker
    try:
        os.symlink(tmp_path / "src" / "main.py", tmp_path / "alias.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        agent.start("Read alias", input_paths=["alias.py"])


@pytest.mark.parametrize("method, kwargs", [("status", {}), ("wait", {}), ("cancel", {}), ("steer", {"message": "Follow up"})])
def test_unknown_job_is_explicit(worker, method, kwargs):
    agent, _, _ = worker
    with pytest.raises(ValueError, match="Unknown coding job"):
        getattr(agent, method)("a" * 32, **kwargs)
    with pytest.raises(ValueError, match="Invalid coding job id"):
        getattr(agent, method)("../../escape", **kwargs)


def test_runtime_failure_is_terminal_and_available_to_caller(worker):
    agent, runtime_class, _ = worker
    runtime_class.fail = True
    job_id = agent.start("Fail", input_paths=[])["job_id"]
    status = agent.wait(job_id, 5)
    assert status["status"] == "failed"
    assert status["error"] == "fake native process failed"
    assert "result" not in status


def test_reopened_worker_marks_unowned_running_execution_unknown(worker):
    agent, runtime_class, _ = worker
    job_id = agent.start("Task", input_paths=[])["job_id"]
    agent.wait(job_id, 5)
    path = agent._job(job_id) / "job.json"
    state = json.loads(path.read_text())
    state["status"] = "running"
    path.write_text(json.dumps(state))
    other = CodingAgent(agent.project, runtime_factory=runtime_class)
    assert other.status(job_id)["status"] == "execution_unknown"
    assert "result" not in other.status(job_id)


def test_resume_refreshes_input_set_instead_of_retaining_removed_sources(worker, tmp_path):
    agent, _, instances = worker
    job_id = agent.start("Read old code", input_paths=["src"])["job_id"]
    agent.wait(job_id, 5)
    (tmp_path / "replacement.py").write_text("print('new')")
    agent.start("Use replacement only", job_id=job_id, input_paths=["replacement.py"])
    assert agent.wait(job_id, 5)["status"] == "completed"
    assert "project/replacement.py" in instances[-1].calls[0]["inputs"]
    assert "project/src/main.py" not in instances[-1].calls[0]["inputs"]


def test_failed_resume_does_not_return_previous_successful_report(worker):
    agent, runtime_class, _ = worker
    job_id = agent.start("Successful attempt", input_paths=[])["job_id"]
    assert agent.wait(job_id, 5)["result"]["outcome"] == "completed"
    runtime_class.fail = True
    agent.start("Fail next attempt", job_id=job_id, input_paths=[])
    failed = agent.wait(job_id, 5)
    assert failed["status"] == "failed"
    assert "result" not in failed
