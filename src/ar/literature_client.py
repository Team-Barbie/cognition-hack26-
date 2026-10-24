"""Codex supplies review decisions; the review runtime owns search and evidence.

Each decision is one stateless native turn with no inherited user tool configuration.
The same LiveAgent validation and charged repair budget apply to all providers.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ar.adapter import AdapterError, LiveAgent, load_env, make_client
from ar.codex_runtime import codex_binary


class CodexDecisionClient:
    def __init__(self, model: str | None = None, *, timeout: float = 120):
        self.model, self.timeout = model, timeout

    def complete(self, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ar-literature-decision-") as folder:
            reply = Path(folder) / "reply.json"
            command = [codex_binary(), "exec", "--ignore-user-config", "--ephemeral",
                       "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                       "-C", folder, "-o", str(reply), "-c", 'approval_policy="never"',
                       "-c", 'web_search="disabled"']
            # No shell, connectors, plugins, browser, delegation or image tools in a decision turn.
            for feature in ("shell_tool", "multi_agent", "apps", "plugins", "browser_use",
                            "computer_use", "image_generation", "in_app_browser", "code_mode",
                            "code_mode_host", "hooks"):
                command += ["-c", f"features.{feature}=false"]
            if self.model:
                command += ["--model", self.model]
            command += ["-"]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
            try:
                stdout, _ = process.communicate(
                    system + "\n\nReturn the requested JSON from the supplied data only. Do not call tools.\n\n" + user,
                    timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.kill()
                process.communicate()
                raise AdapterError("Codex literature decision timed out") from exc
            if process.returncode or not reply.is_file():
                raise AdapterError("Codex literature decision failed; check Codex installation, login and model access")
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") in {"item.started", "item.completed"}:
                    kind = event.get("item", {}).get("type")
                    if kind not in {"agent_message", "reasoning", "todo_list"}:
                        raise AdapterError(f"Unexpected tool activity in a literature decision: {kind}")
            if reply.stat().st_size > 2_000_000:
                raise AdapterError("Codex literature decision exceeded the response limit")
            return reply.read_text(encoding="utf-8")


def review_agent(root: Path, *, provider: str = "codex", model: str | None = None,
                 base_url: str | None = None) -> LiveAgent:
    load_env(root / ".env", Path.cwd() / ".env")
    client = CodexDecisionClient(model) if provider == "codex" else make_client(provider, model, "medium", base_url)
    return LiveAgent(client)
