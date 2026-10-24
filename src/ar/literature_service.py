"""Non-blocking specialist interface for planners and agent harnesses."""
from __future__ import annotations

import json
import threading
from contextlib import closing
from pathlib import Path

from ar import db
from ar.literature import SCHEMA, LiteratureReviewAgent, ReviewSpec
from ar.literature_client import review_agent


class LiteratureService:
    """Multiple independent reviews sharing a project's broker and durable journal.

    Keep the host alive while jobs run. A restart never silently retries an
    operation whose outcome is unknown; identical finished specs reuse results.
    """
    def __init__(self, project: Path, *, provider="anthropic", model=None, base_url=None,
                 agent_factory=None, runner_factory=LiteratureReviewAgent, max_parallel=4):
        if not 1 <= max_parallel <= 8:
            raise ValueError("max_parallel must be between 1 and 8")
        self.project = Path(project).resolve()
        db.init_project(self.project).close()
        with closing(db.connect(self.project)) as conn:
            conn.executescript(SCHEMA)
        self.agent_factory = agent_factory or (lambda: review_agent(
            self.project, provider=provider, model=model, base_url=base_url))
        self.runner_factory, self.max_parallel = runner_factory, max_parallel
        self._lock = threading.Lock()
        self._jobs: dict[str, threading.Thread] = {}
        self._errors: dict[str, str] = {}

    def start(self, spec: dict) -> dict:
        protocol = ReviewSpec.model_validate(spec)
        with self._lock:
            if protocol.key in self._jobs and self._jobs[protocol.key].is_alive():
                return {"job_id": protocol.key, "status": "running"}
            if sum(thread.is_alive() for thread in self._jobs.values()) >= self.max_parallel:
                raise RuntimeError("literature worker capacity reached; poll existing jobs before starting more")
            # Validate the model provider before creating a background job.
            agent = self.agent_factory()
            self._errors.pop(protocol.key, None)
            thread = threading.Thread(target=self._run, args=(protocol, agent), daemon=True)
            self._jobs[protocol.key] = thread
            thread.start()
        return {"job_id": protocol.key, "status": "running"}

    def _run(self, spec, agent):
        try:
            self.runner_factory(self.project, agent).run(spec)
        except Exception as exc:
            with self._lock:
                self._errors[spec.key] = f"{type(exc).__name__}: {exc}"

    def status(self, job_id: str, *, after: int = 0) -> dict:
        if after < 0:
            raise ValueError("event cursor must be non-negative")
        with self._lock:
            local = self._jobs.get(job_id)
            error = self._errors.get(job_id)
        with closing(db.connect(self.project)) as conn:
            row = conn.execute("SELECT * FROM literature_jobs WHERE key=?", (job_id,)).fetchone()
            if row is None:
                if local and local.is_alive():
                    return {"job_id": job_id, "status": "starting", "events": [], "cursor": after}
                if error:
                    return {"job_id": job_id, "status": "failed", "error": error, "events": [], "cursor": after}
                raise ValueError("unknown literature job")
            events = conn.execute("SELECT id,payload_json FROM events WHERE id>? AND kind='literature_review' "
                                  "AND json_extract(payload_json,'$.key')=? ORDER BY id LIMIT 100", (after, job_id)).fetchall()
        result = json.loads(row["result_json"]) if row["result_json"] else None
        status = "failed" if error else row["status"]
        if status == "running" and not (local and local.is_alive()):
            status = "execution_unknown"
        return {"job_id": job_id, "status": status, "error": error,
                "model_calls": row["calls"], "searches": row["searches"], "result": result,
                "events": [{"seq": event["id"], **json.loads(event["payload_json"])} for event in events],
                "cursor": events[-1]["id"] if events else after}

    def list(self) -> dict:
        with closing(db.connect(self.project)) as conn:
            rows = conn.execute("SELECT key,review_id,status FROM literature_jobs ORDER BY created_at").fetchall()
        return {"jobs": [{"job_id": row["key"], "review_id": row["review_id"], "status": row["status"]} for row in rows]}

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        with self._lock:
            thread = self._jobs.get(job_id)
        if thread:
            thread.join(timeout)
        return self.status(job_id)
