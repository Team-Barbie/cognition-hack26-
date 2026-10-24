import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ar import db, mcp_server
from ar.broker import Broker

SRC = Path(__file__).parent.parent / "src"
FIXTURE = Path(__file__).parent.parent / "fixtures" / "baselines"


@pytest.fixture
def project(tmp_path):
    db.init_project(tmp_path, FIXTURE).close()
    return tmp_path


def rpc(project, *messages):
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    out = io.StringIO()
    mcp_server.serve(project, io.StringIO("\n".join(lines) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(project, name, arguments, msg_id=1):
    (response,) = rpc(project, {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
    return response["result"]["structuredContent"], response["result"]["isError"]


def read_args(**kw):
    base = {"task_id": "t-1", "attempt_id": "a-1", "source_ref": {"source_id": "brief", "revision": 1}}
    return base | kw


def test_initialize_list_and_notifications(project):
    out = rpc(
        project,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "",
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert [r["id"] for r in out] == [1, 2, 3]  # the notification and the blank line get no reply
    assert out[0]["result"]["protocolVersion"] == "2025-03-26"
    assert out[0]["result"]["capabilities"] == {"tools": {}}
    tools = {t["name"]: t for t in out[2]["result"]["tools"]}
    assert set(tools) == {"project.status", "files.read", "tasks.submit"}
    schema = tools["files.read"]["inputSchema"]
    assert schema["type"] == "object" and {"source_ref", "task_id", "attempt_id"} <= set(schema["required"])
    assert schema["additionalProperties"] is False and "max_bytes" in schema["properties"]
    assert "required_reads" in tools["tasks.submit"]["inputSchema"]["required"]


def test_project_status(project):
    envelope, is_error = call(project, "project.status", {"request_id": "r-1"})
    assert not is_error and envelope["status"] == "ok" and envelope["request_id"] == "r-1"
    result = envelope["result"]
    assert result["runs"] == 0 and result["task_states"] == {} and result["claims"] == 0
    assert {s["source_id"] for s in result["sources"]} >= {"brief", "paper-a", "paper-b", "paper-c"}
    assert "no OS sandbox" in result["assurance"]


def test_files_read_returns_spec_fields_and_a_receipt(project):
    envelope, is_error = call(project, "files.read", read_args(selector={"kind": "lines", "start": 1, "end": 2}))
    assert not is_error
    result = envelope["result"]
    assert result["source_ref"] == {"source_id": "brief", "revision": 1}
    assert result["delivered_selector"] == {"kind": "lines", "start": 1, "end": 2}
    assert result["coverage"] == "full" and result["truncated"] is False  # of the requested selector
    expected = (FIXTURE / "input" / "brief.md").read_text(encoding="utf-8").splitlines()[:2]
    assert result["content"] == "\n".join(expected)
    conn = db.connect(project)
    try:
        row = conn.execute("SELECT representation_sha256 FROM sources WHERE source_id='brief'").fetchone()
        receipts = Broker(conn, project).receipts_for("t-1", "a-1")
    finally:
        conn.close()
    assert result["content_sha256"] == row[0]
    assert [r.id for r in receipts] == [result["read_receipt_id"]]


def test_files_read_truncates_and_continues_with_a_cursor(project):
    first, _ = call(project, "files.read", read_args(max_bytes=20))
    page = first["result"]
    assert page["truncated"] is True and page["coverage"] == "partial" and page["next_cursor"]
    assert len(page["content"].encode()) <= 20 or "\n" not in page["content"]
    second, _ = call(project, "files.read", read_args(max_bytes=20, cursor=page["next_cursor"]))
    nxt = second["result"]
    assert nxt["delivered_selector"]["start"] == page["delivered_selector"]["end"] + 1
    assert nxt["read_receipt_id"] != page["read_receipt_id"]


def test_files_read_errors_use_the_spec_envelope(project):
    envelope, is_error = call(project, "files.read", read_args(source_ref={"source_id": "ghost", "revision": 1}))
    assert is_error and envelope["status"] == "error"
    assert set(envelope["error"]) == {"code", "message", "retryable", "details"}
    assert envelope["error"]["code"] == "SOURCE_NOT_FOUND" and envelope["error"]["retryable"] is False

    envelope, _ = call(project, "files.read", read_args(selector={"kind": "lines", "start": 500, "end": 600}))
    assert envelope["error"]["code"] == "INVALID_SELECTOR" and "line_count" in envelope["error"]["details"]

    envelope, _ = call(project, "files.read", read_args(bogus=1))
    assert envelope["error"]["code"] == "INVALID_REQUEST" and envelope["error"]["details"]["errors"]

    envelope, _ = call(project, "files.read", {"task_id": "t"})
    assert envelope["error"]["code"] == "INVALID_REQUEST"


def test_tasks_submit_validates_required_reads_only(project):
    required = [{"source_id": "brief", "revision": 1}]
    args = {"task_id": "t-1", "attempt_id": "a-1", "required_reads": required}
    envelope, is_error = call(project, "tasks.submit", args)
    assert not is_error and envelope["status"] == "ok"
    result = envelope["result"]
    assert result["decision"] == "reject"
    assert result["error"]["code"] == "REQUIRED_READ_INCOMPLETE" and result["error"]["retryable"] is True
    assert result["error"]["details"]["missing"]

    call(project, "files.read", read_args())  # the whole brief
    result = call(project, "tasks.submit", args)[0]["result"]
    assert result["decision"] == "accept" and result["checked_sources"] == ["brief"]

    other = call(project, "tasks.submit", args | {"attempt_id": "a-2"})[0]["result"]
    assert other["decision"] == "reject"  # receipts are not transferable between attempts

    conn = db.connect(project)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0  # nothing was run or created
    finally:
        conn.close()


def test_tasks_submit_needs_at_least_one_required_read(project):
    envelope, _ = call(project, "tasks.submit", {"task_id": "t", "attempt_id": "a", "required_reads": []})
    assert envelope["error"]["code"] == "INVALID_REQUEST"


def test_uninitialised_project_is_reported(tmp_path):
    envelope, is_error = call(tmp_path, "project.status", {})
    assert is_error and envelope["error"]["code"] == "SOURCE_NOT_FOUND"
    assert not (tmp_path / ".research").exists()


def test_protocol_errors(project):
    out = rpc(
        project,
        "{not json",
        {"jsonrpc": "2.0", "id": 1, "method": "nope"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "artifacts.create"}},
        {"id": 3, "method": "ping"},
        [1],
    )
    assert [r["error"]["code"] for r in out] == [-32700, -32601, -32602, -32600, -32600]
    assert out[0]["id"] is None


def test_module_entry_point_speaks_stdio(project):
    request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "project.status", "arguments": {}}})
    done = subprocess.run(
        [sys.executable, "-m", "ar.mcp_server", str(project)], input=request + "\n", capture_output=True,
        text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert done.returncode == 0, done.stderr
    response = json.loads(done.stdout)
    assert response["id"] == 7 and response["result"]["structuredContent"]["status"] == "ok"
