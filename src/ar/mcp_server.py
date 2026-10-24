"""A thin MCP server over the runtime: `project.status`, `files.read`, `tasks.submit`.

Handlers are transport-independent. `serve` speaks newline-delimited JSON-RPC 2.0 on stdio using only
the stdlib. It never runs the controller and never writes project state except read receipts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import Field, ValidationError

from ar import db
from ar.broker import Broker, BrokerError
from ar.export import ASSURANCE
from ar.models import LineSelector, Record, SourceRef

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
RETRYABLE = {"REQUIRED_READ_INCOMPLETE", "REVISION_CONFLICT"}


class ToolError(Exception):
    """Structured error in the spec envelope: code, message, retryable, details."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}

    def envelope(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}

    @classmethod
    def from_broker(cls, exc: BrokerError) -> ToolError:
        return cls(exc.code, exc.message, retryable=exc.code in RETRYABLE, details=exc.details)


class ProjectStatusParams(Record):
    request_id: str | None = None


class FilesReadParams(Record):
    request_id: str | None = None
    task_id: str
    attempt_id: str
    packet_id: str = "mcp"
    source_ref: SourceRef
    representation: Literal["normalized_text"] = "normalized_text"
    selector: LineSelector | None = None
    cursor: str | None = None
    max_bytes: int = Field(default=16000, ge=1)
    purpose: str = ""


class TasksSubmitParams(Record):
    request_id: str | None = None
    task_id: str
    attempt_id: str
    required_reads: list[SourceRef] = Field(min_length=1)


def _database(project_root: Path) -> Path:
    database = Path(project_root) / ".research" / "state.sqlite"
    if not database.is_file():
        raise ToolError("SOURCE_NOT_FOUND", f"{project_root} is not an initialized project (run `ar init`)")
    return database


def _parse[M: Record](model: type[M], params: dict | M | None) -> M:
    if isinstance(params, model):
        return params
    try:
        return model.model_validate(params or {})
    except ValidationError as exc:
        raise ToolError(
            "INVALID_REQUEST", "request does not match the tool schema",
            details={"errors": json.loads(exc.json(include_url=False, include_input=False, include_context=False))},
        ) from None


def project_status(project_root: Path, params: dict | None = None) -> dict[str, Any]:
    _parse(ProjectStatusParams, params)
    database = _database(project_root)
    status = asdict(db.read_status(database))
    with closing(db.connect(database)) as conn:
        broker = Broker(conn, project_root)
        status["sources"] = [ref.model_dump() for ref in broker.list_sources()]
        try:
            status["claims"] = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        except sqlite3.OperationalError:
            status["claims"] = 0
    status["assurance"] = ASSURANCE
    return status


def files_read(project_root: Path, params: dict | FilesReadParams) -> dict[str, Any]:
    req = _parse(FilesReadParams, params)
    with closing(db.connect(_database(project_root))) as conn:
        broker = Broker(conn, project_root)
        try:
            page = broker.read_page(
                task_id=req.task_id, attempt_id=req.attempt_id, packet_id=req.packet_id, ref=req.source_ref,
                start=req.selector.start if req.selector else None, end=req.selector.end if req.selector else None,
                max_bytes=req.max_bytes, cursor=req.cursor, label=req.purpose,
            )
            row = conn.execute(
                "SELECT representation_sha256 FROM sources WHERE source_id=? AND revision=?",
                (req.source_ref.source_id, req.source_ref.revision),
            ).fetchone()
        except BrokerError as exc:
            raise ToolError.from_broker(exc) from None
    got = page.delivered
    return {
        "source_ref": got.source_ref.model_dump(),
        "representation": req.representation,
        "content_sha256": row["representation_sha256"],
        "delivered_selector": got.selector.model_dump(),
        "content": got.content,
        "coverage": page.coverage,
        "truncated": page.truncated,
        "next_cursor": page.next_cursor,
        "read_receipt_id": got.read_receipt_id,
    }


def tasks_submit(project_root: Path, params: dict | TasksSubmitParams) -> dict[str, Any]:
    """Validate only: accept when the attempt's receipts cover every required read, else reject."""
    req = _parse(TasksSubmitParams, params)
    with closing(db.connect(_database(project_root))) as conn:
        try:
            Broker(conn, project_root).check_required_reads(
                task_id=req.task_id, attempt_id=req.attempt_id, required=req.required_reads
            )
        except BrokerError as exc:
            return {"decision": "reject", "task_id": req.task_id, "error": ToolError.from_broker(exc).envelope()}
    return {
        "decision": "accept", "task_id": req.task_id, "attempt_id": req.attempt_id,
        "checked_sources": [r.source_id for r in req.required_reads],
    }


TOOLS: dict[str, tuple[str, type[Record], Callable[..., dict[str, Any]]]] = {
    "project.status": ("Project, run and task status plus the enforcement assurance note.",
                       ProjectStatusParams, project_status),
    "files.read": ("Read a line range of a snapshotted source and receive a read receipt for what was delivered.",
                   FilesReadParams, files_read),
    "tasks.submit": ("Check that a task attempt's receipts cover its required reads. Does not run anything.",
                     TasksSubmitParams, tasks_submit),
}


def call_tool(project_root: Path, name: str, arguments: dict | None, request_id: str | None = None) -> dict[str, Any]:
    """Run a tool and wrap the outcome in the response envelope."""
    arguments = dict(arguments or {})
    request_id = arguments.get("request_id") or request_id
    try:
        if name not in TOOLS:
            raise ToolError("UNKNOWN_TOOL", f"no tool named {name!r}", details={"tools": sorted(TOOLS)})
        result = TOOLS[name][2](project_root, arguments)
    except ToolError as exc:
        return {"request_id": request_id, "status": "error", "error": exc.envelope()}
    except Exception as exc:  # the server must not die on one bad call
        error = ToolError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        return {"request_id": request_id, "status": "error", "error": error.envelope()}
    return {"request_id": request_id, "status": "ok", "result": result}


def list_tools() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": model.model_json_schema()}
        for name, (text, model, _) in TOOLS.items()
    ]


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(project_root: Path, message: Any) -> dict[str, Any] | None:
    """One JSON-RPC message in, at most one response out. Notifications get none."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    method, params, msg_id = message["method"], message.get("params") or {}, message.get("id")
    if "id" not in message:
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ar", "version": "0.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list_tools()}
    elif method == "tools/call":
        if params.get("name") not in TOOLS:
            return _rpc_error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
        envelope = call_tool(project_root, params["name"], params.get("arguments"), str(msg_id))
        result = {
            "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
            "structuredContent": envelope,
            "isError": envelope["status"] == "error",
        }
    else:
        return _rpc_error(msg_id, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(project_root: Path, lines: Iterable[str], out: TextIO) -> None:
    for line in lines:
        if not line.strip():
            continue
        try:
            response = handle_message(project_root, json.loads(line))
        except json.JSONDecodeError:
            response = _rpc_error(None, -32700, "parse error")
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ar.mcp_server", description="MCP stdio server for an ar project")
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    serve(args.project, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
