"""Expose Codex as a tool to another agent: python -m ar.coding_mcp PROJECT."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ar.coding_agent import CodingAgent


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Params):
    objective: str = Field(min_length=1)
    input_paths: list[str] | None = None
    job_id: str | None = None
    model: str | None = None
    effort: str = "high"
    max_agents: int = Field(default=3, ge=0, le=8)
    timeout: float = Field(default=1800, gt=0)


class Status(Params):
    job_id: str
    after: int = Field(default=0, ge=0)
    include_result: bool = True


class Steer(Params):
    job_id: str
    message: str = Field(min_length=1)


class Cancel(Params):
    job_id: str


TOOLS = {
    "coding.start": (Start, "Start a native Codex coding/build/simulation/test job. Supply a free-form objective and project-relative input paths. Return immediately with a job id. Pass a previous job_id to resume its native session."),
    "coding.status": (Status, "Poll a job's captured tool events and final result, including output artifacts and hashes. Use the returned cursor as after on the next call. Execution errors and blocked work are explicit."),
    "coding.steer": (Steer, "Send a follow-up instruction to a running Codex job."),
    "coding.cancel": (Cancel, "Interrupt a running Codex job; its session can be resumed later."),
}


def handle(agent: CodingAgent, message) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    request_id = message["id"]
    method, params = message["method"], message.get("params") or {}
    try:
        if method == "initialize":
            versions = {"2025-06-18", "2025-03-26", "2024-11-05"}
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if version in versions else "2025-06-18",
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "ar-coding", "version": "0.2.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": description, "inputSchema": schema.model_json_schema()}
                                for name, (schema, description) in TOOLS.items()]}
        elif method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown coding tool"}}
            try:
                arguments = TOOLS[name][0].model_validate(params.get("arguments") or {}).model_dump()
                output = getattr(agent, name.split(".")[1])(**arguments)
                result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                          "structuredContent": output, "isError": False}
            except (ValueError, RuntimeError, OSError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unknown method"}}
    except (AttributeError, TypeError) as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Make the native Codex worker callable by another agent")
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    agent = CodingAgent(args.project)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                response = handle(agent, json.loads(line))
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        agent.close()


if __name__ == "__main__":
    main()
