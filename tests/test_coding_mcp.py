"""MCP wire dispatch, validation, and lifecycle without a model connection."""

import io
import json

import pytest

from ar import coding_mcp


class Agent:
    def __init__(self, *_):
        self.calls = []
        self.closed = False

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"job_id": "a" * 32, "status": "running"}

    def status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return {"job_id": kwargs["job_id"], "status": "completed", "events": [], "cursor": kwargs["after"]}

    def steer(self, **kwargs):
        self.calls.append(("steer", kwargs))
        return {"job_id": kwargs["job_id"], "status": "follow_up_queued"}

    def cancel(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"job_id": kwargs["job_id"], "status": "cancellation_requested"}

    def close(self):
        self.closed = True


def message(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "unsupported"])
def test_initialize_negotiates_protocol_and_lists_strict_tool_schemas(version):
    agent = Agent()
    response = coding_mcp.handle(agent, message("initialize", {"protocolVersion": version}))
    assert response["result"]["protocolVersion"] == ("2025-06-18" if version == "unsupported" else version)
    assert response["result"]["capabilities"] == {"tools": {}}
    tools = coding_mcp.handle(agent, message("tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"coding.start", "coding.status", "coding.steer", "coding.cancel"}
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


@pytest.mark.parametrize("name, arguments, status", [
    ("coding.start", {"objective": "Build and test", "max_agents": 0}, "running"),
    ("coding.status", {"job_id": "a" * 32, "after": 10, "include_result": False}, "completed"),
    ("coding.steer", {"job_id": "a" * 32, "message": "Test empty inputs"}, "follow_up_queued"),
    ("coding.cancel", {"job_id": "a" * 32}, "cancellation_requested"),
])
def test_tool_calls_preserve_arguments_and_provide_structured_results(name, arguments, status):
    agent = Agent()
    response = coding_mcp.handle(agent, message("tools/call", {"name": name, "arguments": arguments}, request_id="request-7"))
    assert response["id"] == "request-7"
    result = response["result"]
    assert result["isError"] is False and result["structuredContent"]["status"] == status
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert agent.calls[0][0] == name.split(".")[1]
    for key, value in arguments.items():
        assert agent.calls[0][1][key] == value


@pytest.mark.parametrize("name, arguments", [
    ("coding.start", {}), ("coding.start", {"objective": ""}),
    ("coding.start", {"objective": "x", "max_agents": 9}),
    ("coding.start", {"objective": "x", "timeout": 0}),
    ("coding.start", {"objective": "x", "input_paths": "src"}),
    ("coding.start", {"objective": "x", "unknown": True}),
    ("coding.status", {"job_id": "a" * 32, "after": -1}),
    ("coding.steer", {"job_id": "a" * 32, "message": ""}),
    ("coding.cancel", {}),
])
def test_invalid_tool_arguments_return_error_without_dispatch(name, arguments):
    agent = Agent()
    response = coding_mcp.handle(agent, message("tools/call", {"name": name, "arguments": arguments}))
    assert response["result"]["isError"] is True
    assert agent.calls == []


def test_worker_errors_are_explicit_tool_results():
    class Broken(Agent):
        def status(self, **kwargs):
            raise ValueError("Unknown coding job")

    response = coding_mcp.handle(Broken(), message("tools/call", {"name": "coding.status", "arguments": {"job_id": "a" * 32}}))
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Unknown coding job"


def test_jsonrpc_invalid_unknown_and_notification_messages():
    agent = Agent()
    assert coding_mcp.handle(agent, [])["error"]["code"] == -32600
    assert coding_mcp.handle(agent, {"method": "ping"})["error"]["code"] == -32600
    assert coding_mcp.handle(agent, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert coding_mcp.handle(agent, message("unknown"))["error"]["code"] == -32601
    assert coding_mcp.handle(agent, message("tools/call", {"name": "unknown"}))["error"]["code"] == -32602
    assert coding_mcp.handle(agent, message("initialize", [1]))["error"]["code"] == -32602
    assert coding_mcp.handle(agent, message("ping"))["result"] == {}


def test_stdio_main_handles_parse_errors_and_closes_worker(monkeypatch, tmp_path):
    agent = Agent()
    incoming = "\nnot JSON\n" + json.dumps(message("initialize", {"protocolVersion": "2025-06-18"})) + "\n"
    incoming += json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    incoming += json.dumps(message("tools/call", {"name": "coding.start", "arguments": {"objective": "Implement baseline"}})) + "\n"
    output = io.StringIO()
    monkeypatch.setattr(coding_mcp, "CodingAgent", lambda project: agent)
    monkeypatch.setattr(coding_mcp.sys, "stdin", io.StringIO(incoming))
    monkeypatch.setattr(coding_mcp.sys, "stdout", output)
    coding_mcp.main([str(tmp_path)])
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"]["serverInfo"]["name"] == "ar-coding"
    assert responses[2]["result"]["structuredContent"]["status"] == "running"
    assert agent.closed
