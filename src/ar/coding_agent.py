"""A callable Codex worker for any planner, orchestrator or agent harness.

Callers supply an objective and project-relative inputs. They do not have to adopt
the research role graph, task contracts or broker's evidence schemas.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

from ar.codex_research import run_research
from ar.codex_runtime import CodexRuntime, _write_json

EXCLUDED = {".git", ".research", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


class CodingAgent:
    """Background jobs with persistent native sessions and a pollable event journal."""

    def __init__(self, project: Path, *, runtime_factory=CodexRuntime) -> None:
        self.project = Path(project).resolve()
        self.jobs_root = self.project / ".research" / "coding-jobs"
        self.runtime_factory = runtime_factory
        self._jobs: dict[str, tuple[CodexRuntime, threading.Thread]] = {}
        self._lock = threading.RLock()

    def _job(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid coding job id")
        path = self.jobs_root / job_id
        if not (path / "job.json").is_file():
            raise ValueError("Unknown coding job")
        return path

    def _stage(self, destination: Path, inputs: list[str]) -> None:
        for name in inputs:
            relative = Path(name)
            source = self.project / relative
            if relative.is_absolute() or relative.drive or ".." in relative.parts or not source.resolve().is_relative_to(self.project):
                raise ValueError(f"Input must be inside the bound project: {name}")
            if any(part in EXCLUDED for part in relative.parts) or source.is_symlink():
                raise ValueError(f"Input is runtime metadata or a symlink: {name}")
            if not source.exists():
                raise ValueError(f"Input does not exist: {name}")
            paths = source.rglob("*") if source.is_dir() else [source]
            for path in paths:
                rel = path.relative_to(self.project)
                if any(part in EXCLUDED for part in rel.parts):
                    continue
                # Hidden env files are not brought along by directory-wide requests.
                if source.is_dir() and (path.name == ".env" or path.name.startswith(".env.")):
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(self.project):
                    raise ValueError(f"Input contains a link outside its source tree: {rel}")
                if path.is_file():
                    target = destination / "project" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)

    def start(self, objective: str, *, input_paths: list[str] | None = None,
              job_id: str | None = None, model: str | None = None, effort: str = "high",
              max_agents: int = 3, timeout: float = 1800) -> dict:
        if not objective.strip():
            raise ValueError("Provide a coding objective")
        runtime = self.runtime_factory(model=model, effort=effort, max_agents=max_agents, timeout=timeout)
        with self._lock:
            if job_id:
                project = self._job(job_id)
                existing = self._jobs.get(job_id)
                if existing and existing[1].is_alive():
                    raise RuntimeError("This coding job is running; send a follow-up with steer instead")
                if (project / ".research" / "codex.lock").exists():
                    raise RuntimeError("A worker still holds this session; inspect it before resuming")
            else:
                job_id = uuid.uuid4().hex
                project = self.jobs_root / job_id
                project.mkdir(parents=True)
            inputs = project / "input"
            inputs.mkdir(exist_ok=True)
            chosen = input_paths if input_paths is not None else (["input"] if (self.project / "input").is_dir() else [])
            staging = project / (".pending-input-" + uuid.uuid4().hex)
            staging.mkdir()
            try:
                self._stage(staging, chosen)
                previous = inputs / "project"
                if previous.exists():
                    if previous.is_symlink() or not previous.resolve().is_relative_to(project.resolve()):
                        raise ValueError("Staged project directory escapes the coding job")
                    shutil.rmtree(previous)
                if (staging / "project").exists():
                    (staging / "project").replace(previous)
            finally:
                if staging.resolve().is_relative_to(project.resolve()) and not staging.is_symlink():
                    shutil.rmtree(staging)
            (inputs / "brief.md").write_text(
                "# Coding assignment\n\nObjective: " + objective.strip() +
                "\n\nProject files are staged under input/project/. Work on a copy in work/ and return "
                "all changed/new source files or a patch as artifacts, along with build/test/simulation "
                "results as appropriate. Preserve the user's constraints.\n", encoding="utf-8")
            _write_json(project / "job.json", {"job_id": job_id, "status": "running", "objective": objective,
                                               "project": str(project), "input_paths": chosen,
                                               "result_available": False})
            thread = threading.Thread(target=self._run, args=(job_id, project, runtime), daemon=True)
            self._jobs[job_id] = (runtime, thread)
            thread.start()
            return {"job_id": job_id, "status": "running"}

    def _run(self, job_id: str, project: Path, runtime: CodexRuntime) -> None:
        state_path = project / "job.json"
        try:
            report = run_research(project, runtime)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update(status=report["outcome"], thread_id=report.get("thread_id"), result_available=True)
        except Exception as exc:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update(status="failed", error=str(exc))
        _write_json(state_path, state)

    def status(self, job_id: str, *, after: int = 0, include_result: bool = True) -> dict:
        if after < 0:
            raise ValueError("Event cursor must be non-negative")
        project = self._job(job_id)
        result = json.loads((project / "job.json").read_text(encoding="utf-8"))
        with self._lock:
            known = self._jobs.get(job_id)
        if result["status"] == "running" and known is None:
            result["status"] = "execution_unknown"
        events, cursor = [], after
        journal = project / ".research" / "codex-events.jsonl"
        if journal.exists():
            with journal.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if number <= after:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        break  # The writer may be halfway through the last line.
                    events.append({"seq": number, **event})
                    cursor = number
                    if len(events) >= 100:
                        break
        result.update(events=events, cursor=cursor)
        report = project / "output" / "codex-result.json"
        if include_result and result.get("result_available") and result["status"] not in {"running", "execution_unknown"} and report.exists():
            result["result"] = json.loads(report.read_text(encoding="utf-8"))
        return result

    def steer(self, job_id: str, message: str) -> dict:
        if not message.strip():
            raise ValueError("Provide a follow-up message")
        self._job(job_id)
        with self._lock:
            active = self._jobs.get(job_id)
            if not active or not active[1].is_alive():
                raise RuntimeError("No active worker for this job; resume it with start(job_id=...)")
            active[0].steer(message)
        return {"job_id": job_id, "status": "follow_up_queued"}

    def cancel(self, job_id: str) -> dict:
        self._job(job_id)
        with self._lock:
            active = self._jobs.get(job_id)
            if not active or not active[1].is_alive():
                raise RuntimeError("No active worker for this job")
            active[0].cancel()
        return {"job_id": job_id, "status": "cancellation_requested"}

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        self._job(job_id)
        with self._lock:
            active = self._jobs.get(job_id)
        if active:
            active[1].join(timeout)
        return self.status(job_id)

    def close(self) -> None:
        with self._lock:
            active = list(self._jobs.values())
        for runtime, thread in active:
            if thread.is_alive():
                runtime.cancel()
        for _, thread in active:
            if thread.is_alive():
                thread.join(6)
