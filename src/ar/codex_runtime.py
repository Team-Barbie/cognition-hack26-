"""Native Codex App Server transport. The coding harness stays inside Codex.

Only orchestration is implemented here: JSON-RPC, durable thread identities, native
events, cancellation and bounded execution. No replacement model/tool loop.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any


class CodexError(RuntimeError):
    pass


def codex_binary() -> str:
    configured = os.environ.get("AR_CODEX_BIN")
    binary = shutil.which(configured or "codex")
    if not binary:
        raise CodexError("Codex CLI is not installed. Install Codex, run 'codex login', or set AR_CODEX_BIN.")
    return binary


class AppServer:
    """A single background Codex process, with a queue so reads cannot hang forever."""

    def __init__(self, *, on_event: Callable[[str, dict], None], max_agents: int = 3,
                 binary: str | None = None) -> None:
        self.on_event = on_event
        self.binary = binary
        self.max_agents = max_agents
        self.messages: queue.Queue = queue.Queue()
        self.responses: dict[int, dict] = {}
        self.stderr: deque[str] = deque(maxlen=30)
        self.sequence = 0
        self.process = None
        self._write_lock = threading.Lock()

    def __enter__(self):
        command = [self.binary or codex_binary(), "app-server", "-c", "features.multi_agent=true",
                   "-c", f"agents.max_threads={self.max_agents + 1}"]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise CodexError(f"Cannot start Codex: {exc}") from exc
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {"name": "res_loop", "version": "0.2.0"},
                                        "capabilities": {"experimentalApi": True}}, timeout=30)
            self.send({"method": "initialized"})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        self.stderr.append("Ignoring non-object Codex protocol output\n")
                        continue
                    self.messages.put(message)
                except json.JSONDecodeError:
                    self.stderr.append(line[:2000])
        finally:
            self.messages.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line[:2000])

    def send(self, message: dict) -> None:
        with self._write_lock:
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CodexError("Codex disconnected") from exc

    def submit(self, method: str, params: dict) -> int:
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    def pump(self, timeout: float = .2) -> None:
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            return
        if message is None:
            raise CodexError("Codex exited unexpectedly. " + "".join(self.stderr)[-2000:])
        method = message.get("method")
        if method:
            params = message.get("params") or {}
            if "id" in message:
                # Unattended workers never grant new permissions. Expose requests in the audit feed.
                self.on_event("permission.denied", {"method": method, "request": params})
                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                    self.send({"id": message["id"], "result": {"decision": "decline"}})
                elif method == "item/tool/requestUserInput":
                    self.send({"id": message["id"], "result": {"answers": {}}})
                else:
                    self.send({"id": message["id"], "error": {"code": -32601,
                               "message": "This unattended worker does not support this client request"}})
            else:
                self.on_event(method, params)
        elif "id" in message:
            self.responses[message["id"]] = message

    def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        request_id = self.submit(method, params)
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            if time.monotonic() >= deadline:
                raise CodexError(f"Codex timed out during {method}")
            self.pump()
        response = self.responses.pop(request_id)
        if "error" in response:
            raise CodexError(f"Codex {method}: {response['error'].get('message', response['error'])}")
        return response.get("result") or {}

    def __exit__(self, *_):
        if self.process:
            if self.process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    import signal
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()


class CodexRuntime:
    def __init__(self, model: str | None = None, *, effort: str = "high", max_agents: int = 3,
                 timeout: float = 1800, server_factory=AppServer) -> None:
        if not 0 <= max_agents <= 8 or timeout <= 0:
            raise ValueError("max_agents must be 0–8 and timeout must be positive")
        self.model, self.effort = model, effort
        self.max_agents, self.timeout = max_agents, timeout
        self.server_factory = server_factory
        self.cancelled = threading.Event()
        self.steering: queue.Queue[str] = queue.Queue()

    def cancel(self) -> None:
        self.cancelled.set()

    def steer(self, message: str) -> None:
        self.steering.put(message)

    def run(self, workspace: Path, state_path: Path, prompt: str, instructions: str,
            schema: dict, on_event: Callable[[str, dict], None]) -> dict[str, Any]:
        workspace = workspace.resolve()
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        thread_id = state.get("thread_id")
        turn_id = None
        final_text = ""
        completed = None
        usage: dict = {}
        commands: list[dict] = []
        blocked = False
        environment_error = None

        def event(method, params):
            nonlocal final_text, completed, usage, blocked, environment_error
            on_event(method, params)
            if method == "permission.denied":
                blocked = True
            if params.get("threadId") != thread_id:
                return
            event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
            if event_turn and turn_id and event_turn != turn_id:
                return
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    final_text = item.get("text", "")
                if item.get("type") == "commandExecution":
                    commands.append(item)
                    output = item.get("aggregatedOutput") or ""
                    if item.get("exitCode") == -1 and ("windows sandbox:" in output.lower() or "sandbox setup failed" in output.lower()):
                        environment_error = "Native Codex sandbox could not launch a command: " + output[:1000]
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage", {})
            elif method == "turn/completed":
                completed = params.get("turn", {})

        server = self.server_factory(on_event=event, max_agents=self.max_agents)
        try:
            with server:
                params = {"cwd": str(workspace), "sandbox": "workspace-write", "approvalPolicy": "never",
                          "developerInstructions": instructions,
                          "config": {"features.multi_agent": self.max_agents > 0,
                                     "model_reasoning_effort": self.effort}}
                if self.model:
                    params["model"] = self.model
                else:
                    catalog = server.request("model/list", {}).get("data", [])
                    default = next((entry for entry in catalog if entry.get("isDefault")), None)
                    if default:
                        params["model"] = default.get("model") or default["id"]
                if thread_id:
                    params["threadId"] = thread_id
                    response = server.request("thread/resume", params)
                else:
                    response = server.request("thread/start", params)
                thread_id = response["thread"]["id"]
                state_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(state_path, {"thread_id": thread_id, "workspace": str(workspace), "status": "running"})
                on_event("session.ready", {"threadId": thread_id, "resumed": bool(state.get("thread_id"))})
                if self.cancelled.is_set():
                    _write_json(state_path, {"thread_id": thread_id, "workspace": str(workspace), "status": "cancelled"})
                    return {"status": "cancelled", "thread_id": thread_id, "commands": [], "usage": {}}
                response = server.request("turn/start", {"threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}], "effort": self.effort, "outputSchema": schema})
                turn_id = response["turn"]["id"]
                deadline = time.monotonic() + self.timeout
                interrupted = None
                interrupt_deadline = None
                pending_steers: set[int] = set()
                while completed is None:
                    stop_requested = self.cancelled.is_set() or (state_path.parent / "codex-stop").exists()
                    if not interrupted and (stop_requested or environment_error or time.monotonic() >= deadline):
                        interrupted = "cancelled" if stop_requested else "blocked" if environment_error else "timed_out"
                        server.submit("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                        interrupt_deadline = time.monotonic() + 5
                    if interrupt_deadline and time.monotonic() >= interrupt_deadline:
                        break
                    if not interrupted:
                        while not self.steering.empty():
                            message = self.steering.get_nowait()
                            pending_steers.add(server.submit("turn/steer", {"threadId": thread_id,
                                "expectedTurnId": turn_id, "input": [{"type": "text", "text": message}]}))
                    server.pump()
                    for request_id in list(pending_steers):
                        response = server.responses.pop(request_id, None)
                        if response:
                            pending_steers.remove(request_id)
                            if "error" in response:
                                raise CodexError("Codex could not accept the follow-up: " + response["error"]["message"])
                status = interrupted or (completed or {}).get("status", "failed")
                result = {"status": status, "thread_id": thread_id, "turn_id": turn_id,
                          "text": final_text, "usage": usage, "commands": commands, "permission_blocked": blocked}
                if status == "failed":
                    result["error"] = (completed or {}).get("error")
                if environment_error:
                    result["error"] = environment_error
                _write_json(state_path, {"thread_id": thread_id, "workspace": str(workspace), "status": status})
                return result
        except Exception:
            if thread_id:
                _write_json(state_path, {"thread_id": thread_id, "workspace": str(workspace), "status": "failed"})
            raise
        finally:
            # Also cleans up when startup/initialization fails inside __enter__.
            server.__exit__(None, None, None)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
