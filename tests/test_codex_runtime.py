"""Native protocol and session lifecycle tests; no model or Codex installation needed."""

import json
import subprocess
import sys
from collections import deque

import pytest

from ar import codex_runtime
from ar.codex_runtime import AppServer, CodexError, CodexRuntime


@pytest.fixture
def fake_process(tmp_path, monkeypatch):
    """Launch a real stdio JSON-RPC child using Python in place of the Codex binary."""
    script = tmp_path / "app_server.py"
    script.write_text('''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": request["id"], "result": {"server": "fixture"}})
    elif method == "fixture/events":
        print("not valid JSON", flush=True)
        print("[]", flush=True)
        send({"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "build"}}})
        send({"id": request["id"], "result": {"ok": True}})
    elif method == "fixture/error":
        send({"id": request["id"], "error": {"code": -32000, "message": "fixture exploded"}})
    elif method == "fixture/approval":
        pending = request["id"]
        send({"id": 9001, "method": "item/commandExecution/requestApproval", "params": {"command": "build"}})
    elif request.get("id") == 9001:
        send({"id": pending, "result": {"decision": request["result"]["decision"]}})
    elif method == "fixture/exit":
        print("fixture process ended", file=sys.stderr, flush=True)
        break
''', encoding="utf-8")
    native_popen = subprocess.Popen
    calls = []

    def launch(command, **kwargs):
        if command[0] != "fixture-codex":
            return native_popen(command, **kwargs)
        calls.append(command)
        return native_popen([sys.executable, "-u", str(script)], **kwargs)

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", launch)
    return calls


def test_stdio_handshake_events_and_malformed_line_are_handled(fake_process):
    events = []
    with AppServer(binary="fixture-codex", max_agents=2,
                   on_event=lambda *event: events.append(event)) as server:
        assert server.request("fixture/events", {}) == {"ok": True}
        assert events == [("item/started", {"item": {"type": "commandExecution", "command": "build"}})]
        assert any("not valid JSON" in line for line in server.stderr)
        assert any("non-object" in line for line in server.stderr)
        process = server.process
    assert process.poll() is not None
    assert fake_process[0][0:2] == ["fixture-codex", "app-server"]
    assert "agents.max_threads=3" in fake_process[0]


def test_stdio_errors_and_process_exit_propagate(fake_process):
    with AppServer(binary="fixture-codex", on_event=lambda *_: None) as server:
        with pytest.raises(CodexError, match="fixture exploded"):
            server.request("fixture/error", {})
        with pytest.raises(CodexError, match="exited unexpectedly"):
            server.request("fixture/exit", {})


def test_native_permission_request_is_declined_and_audited(fake_process):
    events = []
    with AppServer(binary="fixture-codex", on_event=lambda *event: events.append(event)) as server:
        assert server.request("fixture/approval", {}) == {"decision": "decline"}
    assert events[0][0] == "permission.denied"
    assert events[0][1]["method"] == "item/commandExecution/requestApproval"


class ScriptedServer:
    """Exercise runtime orchestration without simulating a model's reasoning loop."""

    def __init__(self, *, on_event, max_agents, mode="complete", tick=None):
        self.on_event = on_event
        self.max_agents = max_agents
        self.mode = mode
        self.tick = tick
        self.requests = []
        self.responses = {}
        self.pending = deque()
        self.sequence = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def request(self, method, params, **_):
        self.requests.append((method, params))
        if method == "model/list":
            return {"data": [{"id": "non-default", "model": "other-model", "isDefault": False},
                             {"id": "default-id", "model": "supported-default", "isDefault": True}]}
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": params.get("threadId", "lead-thread")}}
        if method == "turn/start":
            if self.mode == "error":
                raise CodexError("turn rejected")
            if self.mode in {"complete", "failed", "steer"}:
                self.pending.extend([
                    ("item/completed", {"threadId": "delegate", "item": {"type": "agentMessage", "text": "ignore delegate final"}}),
                    ("item/completed", {"threadId": "lead-thread", "item": {"type": "agentMessage", "phase": "commentary", "text": "ignore commentary"}}),
                    ("item/completed", {"threadId": "lead-thread", "item": {"type": "commandExecution", "command": "pytest", "exitCode": 0}}),
                    ("thread/tokenUsage/updated", {"threadId": "lead-thread", "tokenUsage": {"total": {"totalTokens": 42}}}),
                    ("item/completed", {"threadId": "lead-thread", "item": {"type": "agentMessage", "text": '{"status":"completed"}'}}),
                    ("turn/completed", {"threadId": "lead-thread", "turn": {"id": "turn-1", "status": "failed" if self.mode == "failed" else "completed", "error": {"message": "execution failed"} if self.mode == "failed" else None}}),
                ])
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def submit(self, method, params):
        self.sequence += 1
        self.requests.append((method, params))
        if method == "turn/interrupt":
            self.pending.append(("turn/completed", {"threadId": "lead-thread", "turn": {"id": "turn-1", "status": "interrupted"}}))
        elif method == "turn/steer":
            self.responses[self.sequence] = {"id": self.sequence, "result": {}}
        return self.sequence

    def pump(self):
        if self.tick:
            self.tick()
        if self.pending:
            self.on_event(*self.pending.popleft())


def make_runtime(**kwargs):
    servers = []
    mode = kwargs.pop("mode", "complete")
    tick = kwargs.pop("tick", None)

    def factory(**params):
        server = ScriptedServer(**params, mode=mode, tick=tick)
        servers.append(server)
        return server

    return CodexRuntime(server_factory=factory, **kwargs), servers


def run(runtime, tmp_path, events=None):
    return runtime.run(tmp_path, tmp_path / "session.json", "Implement baseline", "Read inputs first", {"type": "object"},
                       lambda *event: events.append(event) if events is not None else None)


def test_runtime_resumes_same_thread_and_collects_only_lead_result(tmp_path):
    runtime, servers = make_runtime(model="test-model", max_agents=2)
    events = []
    first = run(runtime, tmp_path, events)
    second = run(runtime, tmp_path, events)
    assert first["thread_id"] == second["thread_id"] == "lead-thread"
    assert first["text"] == '{"status":"completed"}'
    assert first["usage"] == {"total": {"totalTokens": 42}}
    assert first["commands"] == [{"type": "commandExecution", "command": "pytest", "exitCode": 0}]
    assert servers[0].requests[0][0] == "thread/start"
    method, params = servers[1].requests[0]
    assert method == "thread/resume" and params["threadId"] == "lead-thread"
    assert params["approvalPolicy"] == "never" and params["sandbox"] == "workspace-write"
    assert params["model"] == "test-model"
    assert not any(method == "model/list" for server in servers for method, _ in server.requests)
    assert servers[0].requests[1][1]["outputSchema"] == {"type": "object"}
    assert [p["resumed"] for m, p in events if m == "session.ready"] == [False, True]
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "completed"
    assert all(server.closed for server in servers)


def test_runtime_selects_available_default_model_when_no_override(tmp_path):
    runtime, servers = make_runtime()
    assert run(runtime, tmp_path)["status"] == "completed"
    assert run(runtime, tmp_path)["status"] == "completed"
    assert servers[0].requests[0] == ("model/list", {})
    start = next(params for method, params in servers[0].requests if method == "thread/start")
    assert start["model"] == "supported-default"
    resume = next(params for method, params in servers[1].requests if method == "thread/resume")
    assert resume["model"] == "supported-default"
    assert resume["threadId"] == "lead-thread"


@pytest.mark.parametrize("output", [
    "windows sandbox: CreateProcess failed with code 5: Access is denied",
    "Sandbox setup failed: could not create restricted process",
])
def test_native_sandbox_launch_failure_interrupts_and_preserves_evidence(tmp_path, output):
    servers = []

    class EnvironmentFailure(ScriptedServer):
        def request(self, method, params, **kwargs):
            response = super().request(method, params, **kwargs)
            if method == "turn/start":
                self.pending.clear()
                self.pending.append(("item/completed", {"threadId": "lead-thread", "item": {
                    "type": "commandExecution", "command": "python baseline.py", "exitCode": -1,
                    "aggregatedOutput": output}}))
            return response

    def factory(**params):
        servers.append(EnvironmentFailure(**params))
        return servers[-1]

    events = []
    result = run(CodexRuntime(server_factory=factory), tmp_path, events)
    assert result["status"] == "blocked"
    assert output in result["error"]
    assert result["commands"][0]["exitCode"] == -1
    assert result["commands"][0]["aggregatedOutput"] == output
    interrupts = [(method, params) for method, params in servers[0].requests if method == "turn/interrupt"]
    assert interrupts == [("turn/interrupt", {"threadId": "lead-thread", "turnId": "turn-1"})]
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "blocked"
    assert any(method == "item/completed" and params["item"]["aggregatedOutput"] == output for method, params in events)


def test_ordinary_command_failure_is_left_to_agent_instead_of_environment_block(tmp_path):
    class CommandFailure(ScriptedServer):
        def request(self, method, params, **kwargs):
            response = super().request(method, params, **kwargs)
            if method == "turn/start":
                self.pending.appendleft(("item/completed", {"threadId": "lead-thread", "item": {
                    "type": "commandExecution", "command": "pytest", "exitCode": 1,
                    "aggregatedOutput": "AssertionError: expected 7, got 8"}}))
            return response

    result = run(CodexRuntime(server_factory=CommandFailure), tmp_path)
    assert result["status"] == "completed"
    assert "error" not in result
    assert result["commands"][0]["exitCode"] == 1


def test_follow_up_steers_active_turn(tmp_path):
    runtime, servers = make_runtime(mode="steer")
    runtime.steer("Also test the empty input")
    assert run(runtime, tmp_path)["status"] == "completed"
    steering = next(params for method, params in servers[0].requests if method == "turn/steer")
    assert steering == {"threadId": "lead-thread", "expectedTurnId": "turn-1", "input": [{"type": "text", "text": "Also test the empty input"}]}


def test_cancel_before_start_does_not_dispatch_turn_and_records_cancelled_state(tmp_path):
    runtime, servers = make_runtime()
    runtime.cancel()
    result = run(runtime, tmp_path)
    assert result["status"] == "cancelled"
    assert all(method != "turn/start" for method, _ in servers[0].requests)
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "cancelled"


def test_stale_turn_completion_is_ignored_but_permission_denial_is_recorded(tmp_path):
    class EventsServer(ScriptedServer):
        def request(self, method, params, **kwargs):
            response = super().request(method, params, **kwargs)
            if method == "turn/start":
                self.pending.appendleft(("turn/completed", {"threadId": "lead-thread", "turn": {"id": "old-turn", "status": "failed"}}))
                self.pending.appendleft(("permission.denied", {"method": "item/tool/requestUserInput"}))
            return response

    runtime = CodexRuntime(server_factory=EventsServer)
    result = run(runtime, tmp_path)
    assert result["status"] == "completed"
    assert result["text"] == '{"status":"completed"}'
    assert result["permission_blocked"] is True


def test_rejected_steering_is_reported_as_error(tmp_path):
    class RejectSteering(ScriptedServer):
        def submit(self, method, params):
            request_id = super().submit(method, params)
            if method == "turn/steer":
                self.responses[request_id] = {"error": {"message": "turn is no longer active"}}
            return request_id

    runtime = CodexRuntime(server_factory=RejectSteering)
    runtime.steer("Follow-up")
    with pytest.raises(CodexError, match="could not accept the follow-up.*no longer active"):
        run(runtime, tmp_path)


@pytest.mark.parametrize("reason", ["cancelled", "timed_out"])
def test_active_turn_is_interrupted_and_state_is_finalized(tmp_path, monkeypatch, reason):
    clock = [0.0]
    monkeypatch.setattr(codex_runtime.time, "monotonic", lambda: clock[0])
    runtime, servers = make_runtime(mode="idle", timeout=1)

    def tick():
        clock[0] += 2
        if reason == "cancelled":
            runtime.cancel()

    # Inject after creation so both cancellation and timeout pass through one pump.
    runtime.server_factory = lambda **params: servers.append(ScriptedServer(**params, mode="idle", tick=tick)) or servers[-1]
    result = run(runtime, tmp_path)
    assert result["status"] == reason
    assert ("turn/interrupt", {"threadId": "lead-thread", "turnId": "turn-1"}) in servers[0].requests
    assert json.loads((tmp_path / "session.json").read_text())["status"] == reason
    assert servers[0].closed


def test_turn_failure_and_transport_error_are_preserved(tmp_path):
    runtime, _ = make_runtime(mode="failed")
    assert run(runtime, tmp_path)["error"] == {"message": "execution failed"}
    runtime, servers = make_runtime(mode="error")
    with pytest.raises(CodexError, match="turn rejected"):
        run(runtime, tmp_path)
    assert servers[0].closed


def test_request_timeout_is_bounded(monkeypatch):
    server = AppServer(on_event=lambda *_: None)
    monkeypatch.setattr(server, "submit", lambda *_: 1)
    monkeypatch.setattr(server, "pump", lambda: None)
    ticks = iter([0, 2])
    monkeypatch.setattr(codex_runtime.time, "monotonic", lambda: next(ticks))
    with pytest.raises(CodexError, match="timed out during thread/start"):
        server.request("thread/start", {}, timeout=1)
