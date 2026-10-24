"""Expose the literature specialist: python -m ar.literature_mcp PROJECT."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ar.literature import ReviewSpec
from ar.literature_service import LiteratureService


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: ReviewSpec


class Status(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    after: int = Field(default=0, ge=0)


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


TOOLS = {
    "literature.start": (Start, "Start an independently scoped literature review. Supply a unique spec.id, question and bounded search/read/model budgets. Returns immediately; identical specs reuse their journal. Different ids can run concurrently."),
    "literature.status": (Status, "Poll one review's search/read events and terminal result, including accepted claim ids, coverage, limitations and report path. Pass the returned cursor as after on the next call."),
    "literature.list": (Empty, "List persisted literature-review instances in this project."),
}


def handle(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    method, params = message["method"], message.get("params", {})
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if not isinstance(params, dict):
        return response | {"error": {"code": -32602, "message": "params must be an object"}}
    if method == "initialize":
        versions = {"2025-06-18", "2025-03-26", "2024-11-05"}
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in versions else "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "ar-literature-review", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "description": description, "inputSchema": schema.model_json_schema()}
                            for name, (schema, description) in TOOLS.items()]}
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return response | {"error": {"code": -32602, "message": "Unknown literature tool"}}
        try:
            arguments = TOOLS[name][0].model_validate(params.get("arguments") or {}).model_dump(mode="json")
            output = getattr(service, name.split(".")[1])(**arguments)
            result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                      "structuredContent": output, "isError": False}
        except (ValueError, RuntimeError, OSError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        return response | {"error": {"code": -32601, "message": "Unknown method"}}
    return response | {"result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Make the bounded literature-review agent callable by another agent")
    parser.add_argument("project", type=Path)
    parser.add_argument("--provider", choices=["codex", "anthropic", "openai"], default="codex")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    service = LiteratureService(args.project, provider=args.provider, model=args.model, base_url=args.base_url)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle(service, json.loads(line))
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
