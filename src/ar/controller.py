"""Research task orchestration boundaries for the Spine track.

Wave 0 deliberately contains no research loop. The controller will coordinate the
broker, agent runner, and quality gates; it must never call a model client itself.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import yaml

from ar import db
from ar.broker import Broker
from ar.completion import assess
from ar.evidence import accept_claims, list_claims
from ar.experiment import ExperimentSpec, list_runs, run_experiment
from ar.export import export_run
from ar.models import AgentResult, ClaimDraft, EvidenceLink, Requirement, ReviewResult, Role
from ar.packet import DependencyItem, Packet, SourceInfo
from ar.permissions import load_policy
from ar.review import GateDecision, gate
from ar.snapshots import BlobStore, sha256_text


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.FAILED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {TaskState.ACCEPTED, TaskState.NEEDS_REVISION, TaskState.FAILED}
    ),
    TaskState.NEEDS_REVISION: frozenset({TaskState.READY, TaskState.FAILED}),
    TaskState.ACCEPTED: frozenset(),
    TaskState.FAILED: frozenset(),
}


def require_transition(current: TaskState, target: TaskState) -> None:
    """Reject task state changes that the controller is not allowed to make."""

    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid task transition: {current} -> {target}")


class AgentRunner(Protocol):
    """The one model boundary supplied by the Agents track."""

    def __call__(self, role: Role, packet: Packet) -> AgentResult: ...


PacketBuilder = Callable[[Sequence[AgentResult], str, str], Packet]
PacketPreparer = Callable[[Role, Packet], Packet]
RESEARCH_ORDER = (
    "source_scout", "evidence_reader", "research_agent", "theorist",
    "experiment_runner", "reproducer", "analyst", "synthesis_agent", "adversarial_effect_agent",
)
NON_BLOCKING_RESEARCH_ROLES = frozenset(RESEARCH_ORDER) - {
    "evidence_reader", "experiment_runner", "reproducer",
}


@dataclass(frozen=True)
class WorkflowStep:
    """One clamped Wave 1 task and the packet builder supplied by integration code."""

    role: Role
    build_packet: PacketBuilder


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    results: tuple[AgentResult, ...]
    model_calls: int


def _validate_workflow(steps: Sequence[WorkflowStep]) -> None:
    roles = [step.role for step in steps]
    if not roles or roles[0] != "planner":
        raise ValueError("workflow must start with one planner")
    if roles == ["planner"]:
        return
    if roles[-1] != "reviewer":
        raise ValueError("workflow must end with one reviewer")
    if roles.count("planner") != 1 or roles.count("writer") != 1 or roles.count("reviewer") != 1:
        raise ValueError("workflow must contain one planner, one writer, and one reviewer")
    writer_index = roles.index("writer")
    reviewer_index = roles.index("reviewer")
    research_roles = set(RESEARCH_ORDER)
    if any(role not in research_roles for role in roles[1:writer_index]):
        raise ValueError("only bounded research specialists may run between planner and writer")
    for singleton in research_roles - {"evidence_reader"}:
        if roles.count(singleton) > 1:
            raise ValueError(f"workflow may contain at most one {singleton}")
    order = {role: i for i, role in enumerate(RESEARCH_ORDER)}
    middle = roles[1:writer_index]
    if [order[role] for role in middle] != sorted(order[role] for role in middle):
        raise ValueError("research specialists are not in dependency order")
    if "analyst" in middle and not any(role in middle for role in ("experiment_runner", "reproducer")):
        raise ValueError("analyst requires a configured experiment run")
    if "reproducer" in middle and "experiment_runner" not in middle:
        raise ValueError("reproducer requires an initial experiment run")
    if reviewer_index != writer_index + 1:
        raise ValueError("reviewer must run immediately after writer")


def run_workflow(
    database: Path,
    steps: Sequence[WorkflowStep],
    agent_runner: AgentRunner,
    *,
    max_model_calls: int = 20,
    max_tasks: int = 12,
    resume_run_id: str | None = None,
    finalize: bool = True,
    idempotency_key: str | None = None,
    packet_preparer: PacketPreparer | None = None,
    max_reader_workers: int = 4,
) -> RunResult:
    """Run a validated bounded sequence and persist each task before dispatch.

    Packet builders receive accepted earlier results. Reading, receipts, claims,
    and artifacts remain the responsibility of their owning integrations.
    """

    _validate_workflow(steps)
    if not 1 <= max_reader_workers <= 8:
        raise ValueError("max_reader_workers must be between 1 and 8")
    if resume_run_id is None:
        run_id = db.create_run(
            database,
            max_model_calls,
            max_tasks=max_tasks,
            idempotency_key=idempotency_key,
        )
        calls = 0
        budget = max_model_calls
        stored_run = db.read_run(database, run_id)
        if stored_run["status"] != "running":
            connection = db.connect(database)
            try:
                rows = connection.execute(
                    """SELECT * FROM tasks WHERE run_id=? AND state='accepted'
                       ORDER BY created_at, id""",
                    (run_id,),
                ).fetchall()
                stored_results = tuple(db.accepted_result(row) for row in rows)
            finally:
                connection.close()
            return RunResult(
                run_id,
                stored_run["outcome"] or stored_run["status"],
                stored_results,
                stored_run["model_calls"],
            )
    else:
        run = db.read_run(database, resume_run_id)
        if run["status"] != "running":
            raise ValueError(f"run {resume_run_id} cannot be resumed from {run['status']}")
        run_id = resume_run_id
        calls = run["model_calls"]
        budget = run["max_model_calls"]
        max_tasks = run["max_tasks"]
        db.clear_pause(database, run_id)
    results: list[AgentResult] = []
    def prepare(index: int, step: WorkflowStep, previous: Sequence[AgentResult], allowance: int):
        """Create one durable attempt before its model call starts."""

        task_id = f"{run_id}-task-{index}"
        task = db.read_task(database, task_id)
        if task is not None and task["state"] == "accepted" and not task["is_stale"]:
            return db.accepted_result(task)
        if task is None:
            db.create_task(database, run_id, task_id, step.role)
        elif task["state"] == "running":
            db.recover_interrupted_task(database, task_id)
        elif task["state"] != "ready":
            raise RuntimeError(f"task {task_id} cannot run from {task['state']}")
        attempt_id = f"attempt-{uuid4()}"
        packet = step.build_packet(previous, task_id, attempt_id)
        if packet.role != step.role:
            raise ValueError(f"packet role {packet.role!r} does not match step role {step.role!r}")
        if packet.task_id != task_id:
            packet = packet.model_copy(update={"task_id": task_id})
        if packet.attempt_id != attempt_id:
            packet = packet.model_copy(update={"attempt_id": attempt_id})
        if packet_preparer is not None:
            packet = packet_preparer(step.role, packet)
        packet = packet.model_copy(update={"model_call_budget": min(
            allowance, packet.model_call_budget if packet.model_call_budget is not None else allowance)})
        lease = db.start_attempt(database, task_id, attempt_id)
        db.record_event(
            database,
            "packet.dispatched",
            {"attempt_id": attempt_id, "packet": packet.audit_view()},
            run_id=run_id,
            task_id=task_id,
        )
        return step, task_id, attempt_id, packet, lease

    def commit(prepared, result: AgentResult) -> bool:
        """Persist one completed call. Return False for a blocking agent outcome."""

        step, task_id, attempt_id, _packet, lease = prepared
        if result.outcome != "produced" and step.role not in NON_BLOCKING_RESEARCH_ROLES:
            db.finish_attempt(database, task_id, attempt_id, "failed")
            return False
        result = result.model_copy(update={"task_id": task_id})
        db.accept_attempt(
            database,
            task_id,
            attempt_id,
            result,
            fencing_token=lease.fencing_token,
        )
        return True

    try:
        index = 1
        while index <= len(steps):
            step = steps[index - 1]
            if db.pause_requested(database, run_id):
                return RunResult(run_id, "paused", tuple(results), calls)
            if index > max_tasks:
                db.finish_run(database, run_id, "budget_exhausted", calls)
                return RunResult(run_id, "budget_exhausted", tuple(results), calls)

            if step.role == "evidence_reader":
                end = index
                while end <= len(steps) and steps[end - 1].role == "evidence_reader":
                    end += 1
                wave_results: dict[int, AgentResult] = {}
                cursor = index
                while cursor < end:
                    if db.pause_requested(database, run_id):
                        return RunResult(run_id, "paused", tuple(results), calls)
                    pending_indexes: list[int] = []
                    while cursor < end and len(pending_indexes) < max_reader_workers:
                        if cursor > max_tasks:
                            db.finish_run(database, run_id, "budget_exhausted", calls)
                            return RunResult(run_id, "budget_exhausted", tuple(results), calls)
                        task_id = f"{run_id}-task-{cursor}"
                        task = db.read_task(database, task_id)
                        if task is not None and task["state"] == "accepted" and not task["is_stale"]:
                            wave_results[cursor] = db.accepted_result(task)
                            cursor += 1
                            continue
                        if len(pending_indexes) >= budget - calls:
                            break
                        pending_indexes.append(cursor)
                        cursor += 1
                    if not pending_indexes:
                        if cursor < end:
                            db.finish_run(database, run_id, "budget_exhausted", calls)
                            ordered = [wave_results[i] for i in sorted(wave_results)]
                            return RunResult(run_id, "budget_exhausted", tuple([*results, *ordered]), calls)
                        break

                    remaining = budget - calls
                    allowance = 2 if remaining >= 2 * len(pending_indexes) else 1
                    previous = tuple(results)
                    prepared = [
                        prepare(i, steps[i - 1], previous, allowance)
                        for i in pending_indexes
                    ]
                    with ThreadPoolExecutor(
                        max_workers=min(max_reader_workers, len(prepared)),
                        thread_name_prefix="ar-reader",
                    ) as pool:
                        futures = [pool.submit(agent_runner, item[0].role, item[3]) for item in prepared]
                        completed = [future.result() for future in futures]

                    calls += sum(result.model_calls for result in completed)
                    db.record_model_calls(database, run_id, calls)
                    blocking = False
                    for item, result, item_index in zip(prepared, completed, pending_indexes, strict=True):
                        wave_results[item_index] = result.model_copy(update={"task_id": item[1]})
                        if not commit(item, result):
                            blocking = True
                    if blocking:
                        ordered = [wave_results[i] for i in sorted(wave_results)]
                        db.finish_run(database, run_id, "failed", calls)
                        return RunResult(run_id, "failed", tuple([*results, *ordered]), calls)

                results.extend(wave_results[i] for i in range(index, end) if i in wave_results)
                index = end
                continue

            task_id = f"{run_id}-task-{index}"
            task = db.read_task(database, task_id)
            if task is not None and task["state"] == "accepted" and not task["is_stale"]:
                results.append(db.accepted_result(task))
                index += 1
                continue
            if calls >= budget:
                db.finish_run(database, run_id, "budget_exhausted", calls)
                return RunResult(run_id, "budget_exhausted", tuple(results), calls)
            prepared = prepare(index, step, tuple(results), budget - calls)
            if isinstance(prepared, AgentResult):
                results.append(prepared)
                index += 1
                continue
            result = agent_runner(step.role, prepared[3])
            calls += result.model_calls
            db.record_model_calls(database, run_id, calls)
            if not commit(prepared, result):
                db.finish_run(database, run_id, "failed", calls)
                return RunResult(run_id, "failed", tuple([*results, result]), calls)
            results.append(result.model_copy(update={"task_id": prepared[1]}))
            index += 1
    except Exception:
        db.finish_run(database, run_id, "failed", calls)
        raise

    if finalize:
        db.finish_run(database, run_id, "completed", calls)
        return RunResult(run_id, "completed", tuple(results), calls)
    return RunResult(run_id, "running", tuple(results), calls)


def _load_experiment_spec(root: Path) -> ExperimentSpec | None:
    """Load the only executable plan the controller may run; model output is never a command authority."""

    path = root / "input" / "experiment.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("experiment"), dict):
        data = data["experiment"]
    if not isinstance(data, dict):
        raise ValueError("input/experiment.yaml must contain an experiment specification mapping")
    return ExperimentSpec.model_validate(data)


def _planned_middle(
    planner: AgentResult,
    source_ids: list[str],
    *,
    experiment_spec: ExperimentSpec | None,
    capacity: int,
) -> tuple[list[tuple[Role, str | None, str]], list[dict]]:
    """Validate and clamp planner proposals into dependency order without inventing capabilities."""

    proposed = planner.proposed_tasks
    known = set(source_ids)
    readers: list[tuple[str, str]] = []
    rejected: list[dict] = []
    wants: set[Role] = set()
    objectives: dict[Role, str] = {}
    for task in proposed:
        if task.role == "evidence_reader":
            valid = [sid for sid in task.source_ids if sid in known]
            if len(valid) != 1:
                rejected.append({"task": task.model_dump(mode="json"), "reason": "reader needs one known source"})
                continue
            if any(source_id == valid[0] for source_id, _ in readers):
                rejected.append({"task": task.model_dump(mode="json"), "reason": "duplicate reader source"})
                continue
            readers.append((valid[0], task.objective))
        elif task.role in set(RESEARCH_ORDER) - {"evidence_reader"}:
            if task.role in {"experiment_runner", "reproducer"}:
                expected = [experiment_spec.id] if experiment_spec else []
                if task.source_ids != expected:
                    rejected.append({
                        "task": task.model_dump(mode="json"),
                        "reason": "execution task must name the configured experiment id",
                    })
                    continue
            if task.role in wants:
                rejected.append({"task": task.model_dump(mode="json"), "reason": "duplicate specialist role"})
                continue
            wants.add(task.role)
            objectives[task.role] = task.objective
        elif task.role not in {"writer", "reviewer"}:
            rejected.append({"task": task.model_dump(mode="json"), "reason": "role is not available in the research workflow"})

    # A writer cannot produce an evidence-backed report without at least one reader. Older planners that
    # know only the MVP roles therefore remain a safe fallback rather than yielding an empty graph.
    if not readers:
        readers = [
            (source_id, f"Extract evidence from {source_id} that bears on the research objective.")
            for source_id in source_ids
        ]

    if experiment_spec is None:
        for role in ("experiment_runner", "reproducer", "analyst"):
            if role in wants:
                rejected.append({"role": role, "reason": "no input/experiment.yaml was configured"})
                wants.discard(role)
    if "reproducer" in wants:
        wants.add("experiment_runner")
    if "analyst" in wants and "experiment_runner" not in wants and "reproducer" not in wants:
        rejected.append({"role": "analyst", "reason": "no experiment run was planned"})
        wants.discard("analyst")

    optional: list[tuple[Role, str | None, str]] = []
    if "source_scout" in wants:
        optional.append(("source_scout", None, objectives["source_scout"]))
    if "research_agent" in wants:
        optional.append(("research_agent", None, objectives["research_agent"]))
    if "theorist" in wants:
        optional.append(("theorist", None, objectives["theorist"]))
    if "experiment_runner" in wants:
        optional.append((
            "experiment_runner", experiment_spec.id if experiment_spec else None,
            objectives.get("experiment_runner", "Run the configured experiment required by the plan."),
        ))
    if "reproducer" in wants:
        optional.append(("reproducer", experiment_spec.id if experiment_spec else None, objectives["reproducer"]))
    if "analyst" in wants:
        optional.append(("analyst", None, objectives["analyst"]))
    for role in ("synthesis_agent", "adversarial_effect_agent"):
        if role in wants:
            optional.append((role, None, objectives[role]))

    # Reserve one evidence reader first, then requested specialists, then additional sources.
    capacity = max(1, capacity)
    kept_optional = optional[: max(0, capacity - 1)]
    for role, sid, _ in optional[len(kept_optional):]:
        rejected.append({"role": role, "source_id": sid, "reason": "task budget clamp"})
    reader_slots = max(1, capacity - len(kept_optional))
    kept_readers = readers[:reader_slots]
    for sid, _ in readers[len(kept_readers):]:
        rejected.append({"role": "evidence_reader", "source_id": sid, "reason": "task budget clamp"})

    scout = [item for item in kept_optional if item[0] == "source_scout"]
    rest = [item for item in kept_optional if item[0] != "source_scout"]
    middle = [*scout, *(("evidence_reader", sid, objective) for sid, objective in kept_readers), *rest]
    return middle, rejected


def run_project(
    project_root: Path,
    agent,
    *,
    max_tasks: int = 12,
    max_model_calls: int = 30,
    idempotency_key: str | None = None,
    literature_runner=None,
) -> RunOutcome:
    """Run the complete managed research loop and export its durable outcome."""

    root = project_root.resolve()
    database = root / ".research" / "state.sqlite"
    brief_path = root / "input" / "brief.md"
    if not brief_path.is_file():
        raise FileNotFoundError(brief_path)
    if max_tasks < 4:
        return RunOutcome.BLOCKED
    if max_model_calls < 0:
        raise ValueError("max_model_calls must be non-negative")
    policy_path = root / "policies" / "permissions.yaml"
    policy = load_policy(policy_path) if policy_path.is_file() else None
    from ar.literature import LiteratureReviewAgent, load_reviews
    review_specs = load_reviews(root)
    conn = db.connect(database)
    broker = Broker(conn, root, policy)
    literature_results = []
    if review_specs:
        runner = literature_runner or LiteratureReviewAgent(root, agent)
        remaining = max_model_calls
        try:
            for index, spec in enumerate(review_specs):
                # Split remaining discovery capacity fairly and reserve calls for the final report.
                allowance = max(0, remaining - 6) // (len(review_specs) - index)
                has_jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='literature_jobs'").fetchone()
                previous = conn.execute("SELECT calls FROM literature_jobs WHERE key=?", (spec.key,)).fetchone() if has_jobs else None
                already_used = previous[0] if previous else 0
                reviewed = runner.run(spec, call_limit=allowance)
                literature_results.append(reviewed)
                remaining = max(0, remaining - max(0, reviewed.model_calls - already_used))
        except BaseException:
            conn.close()
            raise
        max_model_calls = remaining
    paper_paths = sorted((root / "input" / "papers").glob("*.md"))
    if not paper_paths and not any(review.claim_ids for review in literature_results):
        conn.close()
        if review_specs:
            return RunOutcome.BLOCKED
        raise ValueError("the project has no markdown papers in input/papers")
    brief_ref = broker.register_source("brief", brief_path)
    all_paper_refs = [
        (path.stem, broker.register_source(path.stem, path), path)
        for path in paper_paths
    ]
    current_refs = [brief_ref, *[ref for _, ref, _ in all_paper_refs],
                    *[ref for review in literature_results for ref in review.source_refs]]
    versions = {}
    for ref in current_refs:
        row = conn.execute(
            """SELECT representation_sha256 FROM sources
               WHERE source_id=? AND revision=?""",
            (ref.source_id, ref.revision),
        ).fetchone()
        versions[ref.source_id] = row["representation_sha256"]
    if review_specs:
        versions["literature-config"] = sha256_text(json.dumps([spec.model_dump() for spec in review_specs], sort_keys=True))
    changed = db.update_input_versions(database, versions)
    paper_refs = all_paper_refs[: max_tasks - 3]
    requirements = db.refresh_requirements(database, brief_path)
    brief_text = brief_path.read_text(encoding="utf-8")
    question = next(
        (
            line.split(":", 1)[1].strip()
            for line in brief_text.splitlines()
            if line.lower().startswith("question:")
        ),
        "Review the supplied research sources.",
    )
    available = [
        SourceInfo(source_id=source_id, path=str(path.relative_to(root)))
        for source_id, _, path in paper_refs
    ]
    experiment_spec = _load_experiment_spec(root)
    experiment_dependency = (
        DependencyItem(
            kind="artifact",
            id=f"experiment-spec:{experiment_spec.id}",
            content=experiment_spec.model_dump_json(indent=2),
        )
        if experiment_spec else None
    )

    def packet_id(task_id: str, attempt_id: str) -> str:
        return f"packet-{task_id}-{attempt_id}"

    def planner_packet(previous, task_id, attempt_id):
        pid = packet_id(task_id, attempt_id)
        read = broker.read(
            ref=brief_ref,
            task_id=task_id,
            attempt_id=attempt_id,
            packet_id=pid,
            label="brief",
        )
        return Packet(
            packet_id=pid,
            task_id=task_id,
            attempt_id=attempt_id,
            role="planner",
            objective=question,
            requirements=requirements,
            reads=[read],
            available_sources=available,
            dependencies=([experiment_dependency] if experiment_dependency else []) + research_dependencies(previous),
        )

    planner_step = WorkflowStep(role="planner", build_packet=planner_packet)
    reader_steps: dict[str, WorkflowStep] = {}
    for source_id, source_ref, _ in paper_refs:

        def reader_packet(previous, task_id, attempt_id, sid=source_id, ref=source_ref):
            pid = packet_id(task_id, attempt_id)
            read = broker.read(
                ref=ref,
                task_id=task_id,
                attempt_id=attempt_id,
                packet_id=pid,
                label=sid,
            )
            return Packet(
                packet_id=pid,
                task_id=task_id,
                attempt_id=attempt_id,
                role="evidence_reader",
                objective=question,
                requirements=requirements,
                reads=[read],
            )

        reader_steps[source_id] = WorkflowStep(role="evidence_reader", build_packet=reader_packet)

    def accepted_attempt(task_id: str) -> str:
        row = conn.execute(
            """SELECT id FROM attempts WHERE task_id=? AND status='accepted'
               ORDER BY number DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"accepted attempt not found for {task_id}")
        return row["id"]

    def claim_dependencies(previous):
        literature_ids = list(dict.fromkeys(cid for review in literature_results for cid in review.claim_ids))
        dependencies = []
        stored = list_claims(conn)
        for claim in stored:
            if claim.id in literature_ids:
                dependencies.append(DependencyItem(kind="claim", id=claim.id, claim=ClaimDraft.model_validate(
                    claim.model_dump(exclude={"id", "origin_task"}))))
        for result in previous:
            if result.role != "evidence_reader":
                continue
            claims = [claim for claim in stored if claim.origin_task == result.task_id]
            if not claims:
                accepted = accept_claims(
                    conn,
                    broker,
                    task_id=result.task_id,
                    attempt_id=accepted_attempt(result.task_id),
                    drafts=result.claims,
                )
                # Rejected drafts are stored by evidence.accept_claims and reported at export;
                # the run continues with the claims that passed.
                claims = accepted.accepted
                stored.extend(claims)
            for claim in claims:
                dependencies.append(
                    DependencyItem(
                        kind="claim",
                        id=claim.id,
                        claim=ClaimDraft.model_validate(
                            claim.model_dump(exclude={"id", "origin_task"})
                        ),
                    )
                )
        return dependencies

    def research_dependencies(previous):
        dependencies: list[DependencyItem] = [DependencyItem(
            kind="artifact", id=f"literature:{review.key}", content=review.model_dump_json(indent=2)
        ) for review in literature_results]
        for result in previous:
            if result.outcome != "produced":
                continue
            if result.role in {"source_scout", "research_agent", "theorist", "analyst", "synthesis_agent", "adversarial_effect_agent"}:
                dependencies.append(DependencyItem(
                    kind="artifact", id=result.task_id,
                    content=result.model_dump_json(indent=2),
                ))
            elif result.role in {"experiment_runner", "reproducer"}:
                record = result.payload.get("analysis_record")
                if isinstance(record, dict) and record.get("run_id"):
                    dependencies.append(DependencyItem(
                        kind="artifact", id=str(record["run_id"]),
                        content=json.dumps(record, indent=2, sort_keys=True),
                    ))
        return dependencies

    def supporting_reads(claims, task_id, attempt_id):
        """Deliver cited source passages to the specialist under its own read receipts."""
        reads, seen = [], set()
        for item in claims:
            for link in item.claim.evidence_links if item.claim else ():
                key = (link.source_ref.source_id, link.source_ref.revision, link.locator.start, link.locator.end)
                if key in seen:
                    continue
                seen.add(key)
                reads.append(broker.read(
                    task_id=task_id, attempt_id=attempt_id, packet_id=packet_id(task_id, attempt_id),
                    ref=link.source_ref, start=link.locator.start, end=link.locator.end,
                    label=link.source_ref.source_id,
                ))
        return reads

    def source_scout_packet(previous, task_id, attempt_id):
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="source_scout", objective=question, requirements=requirements, available_sources=available,
        )

    def theorist_packet(previous, task_id, attempt_id):
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="theorist", objective=question, requirements=requirements,
            dependencies=claim_dependencies(previous), skills=["hypothesis_design"],
        )

    def research_agent_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="research_agent", objective=question, requirements=requirements,
            dependencies=[*claims, *research_dependencies(previous)],
            reads=supporting_reads(claims, task_id, attempt_id),
            available_sources=available,
            skills=["source_screening", "literature_extraction", "hypothesis_design", "result_analysis"],
        )

    def experiment_packet(role: Role, previous, task_id, attempt_id):
        if experiment_dependency is None:
            raise RuntimeError(f"{role} was dispatched without input/experiment.yaml")
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role=role, objective=question, requirements=requirements,
            dependencies=[experiment_dependency, *research_dependencies(previous)],
        )

    def analyst_packet(previous, task_id, attempt_id):
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="analyst", objective=question, requirements=requirements,
            dependencies=[
                *claim_dependencies(previous), *research_dependencies(previous)
            ], skills=["result_analysis"],
        )

    def synthesis_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="synthesis_agent", objective=question, requirements=requirements,
            dependencies=[*claims, *research_dependencies(previous)],
            reads=supporting_reads(claims, task_id, attempt_id),
            skills=["synthesis"],
        )

    def adversarial_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        research = research_dependencies(previous)
        # Review the most recent accepted synthesis/assessment rather than unrelated metadata.
        candidate = next((dep for dep in reversed(research)), None)
        if candidate is None:
            candidate = DependencyItem(
                kind="artifact", id="accepted-evidence",
                content="\n".join(f"{dep.claim.statement} [claim:{dep.id}]" for dep in claims if dep.claim),
            )
        return Packet(
            packet_id=packet_id(task_id, attempt_id), task_id=task_id, attempt_id=attempt_id,
            role="adversarial_effect_agent", objective=question, requirements=requirements,
            dependencies=[candidate, *claims, *(dep for dep in research if dep is not candidate)],
            skills=["source_screening", "result_analysis"],
        )

    def writer_packet(previous, task_id, attempt_id, feedback=None):
        incomplete = [f"Incomplete research task {result.role}: " + "; ".join(result.unresolved or [result.outcome])
                      for result in previous if result.outcome != "produced"]
        return Packet(
            packet_id=packet_id(task_id, attempt_id),
            task_id=task_id,
            attempt_id=attempt_id,
            role="writer",
            objective=question,
            requirements=requirements,
            dependencies=[*claim_dependencies(previous), *research_dependencies(previous)],
            review_feedback=[*(feedback or []), *incomplete],
        )

    def report_artifact(writer, content, claims, previous=()):
        existing = conn.execute(
            """SELECT artifact_id, revision FROM artifacts
               WHERE producing_task=? ORDER BY revision DESC LIMIT 1""",
            (writer.task_id,),
        ).fetchone()
        if existing:
            return broker.get_artifact(existing["artifact_id"], existing["revision"])[0]
        return broker.create_artifact(
            task_id=writer.task_id,
            attempt_id=accepted_attempt(writer.task_id),
            artifact_type="analysis_report",
            content=content,
            depends_on=[claim.id for claim in claims] + [
                dep.id for dep in research_dependencies(previous) if dep.id.startswith("exp-")
            ],
        )

    def reviewer_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        writer = next(result for result in reversed(previous) if result.role == "writer")
        content = writer.payload["content_markdown"]
        artifact = report_artifact(writer, content, claims, previous)
        return Packet(
            packet_id=packet_id(task_id, attempt_id),
            task_id=task_id,
            attempt_id=attempt_id,
            role="reviewer",
            objective=question,
            requirements=requirements,
            dependencies=[
                DependencyItem(
                    kind="artifact",
                    id=artifact.artifact_id,
                    revision=artifact.revision,
                    content=content,
                ),
                *claims,
                *research_dependencies(previous),
            ],
        )

    specialist_steps: dict[Role, WorkflowStep] = {
        "source_scout": WorkflowStep(role="source_scout", build_packet=source_scout_packet),
        "research_agent": WorkflowStep(role="research_agent", build_packet=research_agent_packet),
        "theorist": WorkflowStep(role="theorist", build_packet=theorist_packet),
        "experiment_runner": WorkflowStep(
            role="experiment_runner",
            build_packet=lambda previous, task_id, attempt_id: experiment_packet(
                "experiment_runner", previous, task_id, attempt_id
            ),
        ),
        "reproducer": WorkflowStep(
            role="reproducer",
            build_packet=lambda previous, task_id, attempt_id: experiment_packet(
                "reproducer", previous, task_id, attempt_id
            ),
        ),
        "analyst": WorkflowStep(role="analyst", build_packet=analyst_packet),
        "synthesis_agent": WorkflowStep(role="synthesis_agent", build_packet=synthesis_packet),
        "adversarial_effect_agent": WorkflowStep(role="adversarial_effect_agent", build_packet=adversarial_packet),
    }

    def checked_agent(role, packet):
        if role in {"experiment_runner", "reproducer"}:
            broker.check_capability(role, ["experiments.execute"], "experiments.execute")
            spec_item = next(
                (item for item in packet.dependencies if item.id.startswith("experiment-spec:")), None
            )
            if spec_item is None:
                raise RuntimeError(f"{role} packet has no configured experiment specification")
            configured = ExperimentSpec.model_validate_json(spec_item.content)
            bound = configured.model_copy(update={"id": f"{configured.id}@{packet.task_id}"})
            prior = next((run for run in list_runs(conn) if run.spec_id == bound.id), None)
            run = prior or run_experiment(root, conn, bound)
            store = BlobStore(root)
            output_sha = next(iter(run.output_hashes.values()), "")
            outputs: dict = {}
            for name, digest in run.output_hashes.items():
                try:
                    decoded = json.loads(store.get(digest).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    outputs[name] = f"sha256:{digest}"
                else:
                    if isinstance(decoded, dict):
                        outputs.update(decoded)
                    else:
                        outputs[name] = decoded
            analysis_record = {
                "run_id": run.id,
                "status": run.outcome,
                "result_acceptable": run.result_acceptable,
                "output_sha256": output_sha,
                "outputs": outputs,
                "output_hashes": run.output_hashes,
                "deviations": run.deviations,
                "assurance": run.assurance,
            }
            result = AgentResult(
                role=role,
                task_id=packet.task_id,
                outcome="produced",
                payload={"spec_id": configured.id, "run": run.model_dump(mode="json"),
                         "analysis_record": analysis_record},
                limitations=[] if run.result_acceptable else [
                    f"Experiment {run.id} was recorded but its result was not acceptable."
                ],
            )
        else:
            result = agent.run(role, packet)
        if role == "writer":
            result = result.model_copy(update={"limitations": [*result.limitations,
                *(note for note in packet.review_feedback if note.startswith("Incomplete research task "))]})
        if role == "writer" and literature_results:
            result = result.model_copy(update={"limitations": [*result.limitations, *[
                f"Literature review {review.id}: {limitation}"
                for review in literature_results for limitation in review.limitations
            ]]})
        unique = {
            (read.source_ref.source_id, read.source_ref.revision): read.source_ref
            for read in packet.reads
        }
        required = list(unique.values())
        if required:
            # Evidence readers may run in worker threads. Never carry the controller's
            # main-thread SQLite connection across that boundary; a short-lived broker
            # connection keeps receipt verification thread-safe and read-only.
            check_conn = db.connect(database)
            try:
                verification = Broker(check_conn, root, policy)
                if role in {"research_agent", "synthesis_agent"}:
                    # These specialists receive cited excerpts, not entire papers.
                    # Verify ownership, locator and quote for each delivered receipt.
                    for read in packet.reads:
                        verification.verify_evidence_link(EvidenceLink(
                            relation="supports", source_ref=read.source_ref, locator=read.selector,
                            quote=read.content, read_receipt_id=read.read_receipt_id,
                        ), task_id=packet.task_id, attempt_id=packet.attempt_id)
                else:
                    verification.check_required_reads(
                        task_id=packet.task_id, attempt_id=packet.attempt_id, required=required,
                    )
            finally:
                check_conn.close()
        return result

    prepare_packet = getattr(agent, "prepare_packet", None)

    db.recover_expired_leases(database)
    active = None if changed else db.latest_active_run(database)
    try:
        planning = run_workflow(
            database,
            [planner_step],
            checked_agent,
            max_model_calls=max_model_calls,
            max_tasks=max_tasks,
            resume_run_id=active,
            finalize=False,
            idempotency_key=idempotency_key,
            packet_preparer=prepare_packet,
        )
        if planning.status == "paused":
            return RunOutcome.CANCELLED
        if planning.status == "budget_exhausted":
            return RunOutcome.BUDGET_EXHAUSTED
        if planning.status == "completed":
            return RunOutcome.COMPLETED
        if planning.status != "running":
            return RunOutcome.FAILED
        if db.pause_requested(database, planning.run_id):
            return RunOutcome.CANCELLED

        planner = next(result for result in planning.results if result.role == "planner")
        middle, rejected = _planned_middle(
            planner,
            [source_id for source_id, _, _ in paper_refs],
            experiment_spec=experiment_spec,
            capacity=max_tasks - 3,
        )
        steps: list[WorkflowStep] = [planner_step]
        selected: list[dict] = []
        def assigned(step: WorkflowStep, objective: str) -> WorkflowStep:
            """Keep the user's objective authoritative while retaining the planner's bounded assignment."""

            def build(previous, task_id, attempt_id):
                packet = step.build_packet(previous, task_id, attempt_id)
                return packet.model_copy(update={
                    "objective": f"{packet.objective}\n\nPlanner assignment: {objective}"
                })

            return WorkflowStep(role=step.role, build_packet=build)

        for role, source_id, objective in middle:
            if role == "evidence_reader":
                steps.append(assigned(reader_steps[source_id], objective))
            else:
                steps.append(assigned(specialist_steps[role], objective))
            selected.append({"role": role, "source_id": source_id, "objective": objective})
        steps += [
            WorkflowStep(role="writer", build_packet=writer_packet),
            WorkflowStep(role="reviewer", build_packet=reviewer_packet),
        ]
        event_key = f"planner-proposals:{planning.run_id}"
        if db.load_idempotent(database, "event", event_key) is None:
            db.record_event(
                database,
                "planner_proposals",
                {
                    "proposals": [task.model_dump(mode="json") for task in planner.proposed_tasks],
                    "selected": selected,
                    "rejected": rejected,
                    "clamped": bool(rejected),
                },
                run_id=planning.run_id,
                task_id=planner.task_id,
            )
            db.save_idempotent(database, "event", event_key, {"stored": True})

        run = run_workflow(
            database,
            steps,
            checked_agent,
            max_model_calls=max_model_calls,
            max_tasks=max_tasks,
            resume_run_id=planning.run_id,
            finalize=False,
            packet_preparer=prepare_packet,
        )
        if run.status == "paused":
            return RunOutcome.CANCELLED
        if run.status == "budget_exhausted":
            writers = [result for result in run.results if result.role == "writer"]
            if writers:
                writer = writers[-1]
                claims = claim_dependencies(run.results)
                content = writer.payload["content_markdown"]
                artifact = report_artifact(writer, content, claims, run.results)
                assessment = assess(
                    conn,
                    requirements=requirements,
                    artifact=artifact,
                    content=content,
                    coverage=writer.payload.get("coverage", {}),
                    limitations=[*writer.limitations, "Review was not run because the budget was exhausted."],
                )
                export_run(
                    conn,
                    project_root=root,
                    artifact=artifact,
                    content=content,
                    assessment=assessment,
                    outcome=RunOutcome.BUDGET_EXHAUSTED.value,
                    broker=broker,
                    assurance="Application-level brokerage; no operating-system sandbox.",
                    run_id=run.run_id,
                    cost={"model_calls": run.model_calls, "tasks": len(run.results),
                          "literature_model_calls": sum(review.model_calls for review in literature_results)},
                )
            return RunOutcome.BUDGET_EXHAUSTED
        if run.status == "completed":
            return RunOutcome.COMPLETED
        if run.status == "blocked":
            return RunOutcome.BLOCKED
        if run.status != "running":
            return RunOutcome.FAILED

        results = list(run.results)
        writer = next(result for result in reversed(results) if result.role == "writer")
        reviewer = next(result for result in reversed(results) if result.role == "reviewer")
        claims = claim_dependencies(results)
        content = writer.payload["content_markdown"]
        artifact = report_artifact(writer, content, claims, results)
        gate_result = gate(
            conn, ReviewResult.model_validate(reviewer.payload), artifact=artifact
        )
        calls = run.model_calls
        stored_budget = db.read_run(database, run.run_id)
        max_model_calls, max_tasks = stored_budget["max_model_calls"], stored_budget["max_tasks"]

        def export_budget_draft(reason: str) -> None:
            assessment = assess(
                conn,
                requirements=requirements,
                artifact=artifact,
                content=content,
                coverage=writer.payload.get("coverage", {}),
                limitations=[*writer.limitations, reason],
            )
            export_run(
                conn,
                project_root=root,
                artifact=artifact,
                content=content,
                assessment=assessment,
                outcome=RunOutcome.BUDGET_EXHAUSTED.value,
                broker=broker,
                assurance="Application-level brokerage; no operating-system sandbox.",
                run_id=run.run_id,
                cost={"model_calls": calls, "tasks": len(results),
                      "literature_model_calls": sum(review.model_calls for review in literature_results)},
            )

        def execute_extra(role, ordinal, make_packet):
            nonlocal calls
            if db.pause_requested(database, run.run_id):
                return None, "paused"
            task_id = f"{run.run_id}-task-{ordinal}"
            task = db.read_task(database, task_id)
            if task is not None and task["state"] == "accepted" and not task["is_stale"]:
                return db.accepted_result(task), "accepted"
            if ordinal > max_tasks or calls >= max_model_calls:
                db.finish_run(database, run.run_id, "budget_exhausted", calls)
                return None, "budget_exhausted"
            if task is None:
                db.create_task(database, run.run_id, task_id, role)
            elif task["state"] == "running":
                db.recover_interrupted_task(database, task_id)
            elif task["state"] != "ready":
                raise RuntimeError(f"task {task_id} cannot run from {task['state']}")
            attempt_id = f"attempt-{uuid4()}"
            packet = make_packet(task_id, attempt_id)
            if prepare_packet is not None:
                packet = prepare_packet(role, packet)
            packet = packet.model_copy(update={"model_call_budget": max_model_calls - calls})
            lease = db.start_attempt(database, task_id, attempt_id)
            db.record_event(
                database,
                "packet.dispatched",
                {"attempt_id": attempt_id, "packet": packet.audit_view()},
                run_id=run.run_id,
                task_id=task_id,
            )
            result = checked_agent(role, packet)
            calls += result.model_calls
            db.record_model_calls(database, run.run_id, calls)
            if result.outcome != "produced":
                db.finish_attempt(database, task_id, attempt_id, "failed")
                db.finish_run(database, run.run_id, "failed", calls)
                return result, "failed"
            result = result.model_copy(update={"task_id": task_id})
            db.accept_attempt(
                database,
                task_id,
                attempt_id,
                result,
                fencing_token=lease.fencing_token,
            )
            return result, "accepted"

        if gate_result.decision is GateDecision.needs_revision:
            writer_ordinal = len(steps) + 1
            revised_writer, state = execute_extra(
                "writer",
                writer_ordinal,
                lambda task_id, attempt_id: writer_packet(
                    results, task_id, attempt_id, gate_result.feedback
                ),
            )
            if state == "paused":
                return RunOutcome.CANCELLED
            if state == "budget_exhausted":
                export_budget_draft("Revision was not run because the budget was exhausted.")
                return RunOutcome.BUDGET_EXHAUSTED
            if state == "failed":
                return RunOutcome.FAILED
            content = revised_writer.payload["content_markdown"]
            writer = revised_writer
            existing = conn.execute(
                """SELECT artifact_id, revision FROM artifacts WHERE producing_task=?
                   ORDER BY revision DESC LIMIT 1""",
                (revised_writer.task_id,),
            ).fetchone()
            if existing:
                artifact = broker.get_artifact(existing["artifact_id"], existing["revision"])[0]
            else:
                artifact = broker.revise_artifact(
                    task_id=revised_writer.task_id,
                    attempt_id=accepted_attempt(revised_writer.task_id),
                    artifact_id=artifact.artifact_id,
                    expected_revision=artifact.revision,
                    content=content,
                    change_reason="Address blocking reviewer findings",
                )
            results.append(revised_writer)
            reviewer_ordinal = writer_ordinal + 1

            def revised_review_packet(task_id, attempt_id):
                return Packet(
                    packet_id=packet_id(task_id, attempt_id),
                    task_id=task_id,
                    attempt_id=attempt_id,
                    role="reviewer",
                    objective=question,
                    requirements=requirements,
                    dependencies=[
                        DependencyItem(
                            kind="artifact",
                            id=artifact.artifact_id,
                            revision=artifact.revision,
                            content=content,
                        ),
                        *claims,
                        *research_dependencies(results),
                    ],
                )

            revised_review, state = execute_extra(
                "reviewer", reviewer_ordinal, revised_review_packet
            )
            if state == "paused":
                return RunOutcome.CANCELLED
            if state == "budget_exhausted":
                export_budget_draft("Re-review was not run because the budget was exhausted.")
                return RunOutcome.BUDGET_EXHAUSTED
            if state == "failed":
                return RunOutcome.FAILED
            gate_result = gate(
                conn,
                ReviewResult.model_validate(revised_review.payload),
                artifact=artifact,
                revision_pass=1,
            )

        limitations = [*writer.limitations, *gate_result.limitations]
        assessment = assess(
            conn,
            requirements=requirements,
            artifact=artifact,
            content=content,
            coverage=writer.payload.get("coverage", {}),
            limitations=limitations,
        )
        for reviewed in literature_results:
            if reviewed.status != "finished":
                assessment.problems.append(f"Literature review {reviewed.id} is {reviewed.status}: {reviewed.stop_reason}")
                assessment.complete = False
        outcome = RunOutcome.COMPLETED if assessment.complete else RunOutcome.BLOCKED
        db.finish_run(
            database,
            run.run_id,
            "completed" if outcome is RunOutcome.COMPLETED else "failed",
            calls,
            outcome=outcome.value,
        )
        export_run(
            conn,
            broker=broker,
            project_root=root,
            artifact=artifact,
            content=content,
            assessment=assessment,
            outcome=outcome.value,
            assurance="Application-level brokerage; no operating-system sandbox.",
            run_id=run.run_id,
            cost={"model_calls": calls, "tasks": len(results),
                  "literature_model_calls": sum(review.model_calls for review in literature_results)},
        )
        return outcome
    finally:
        conn.close()


def export_project(project_root: Path) -> list[Path]:
    """Re-export the newest committed report without running an agent."""

    root = project_root.resolve()
    database = root / ".research" / "state.sqlite"
    conn = db.connect(database)
    try:
        artifact_row = conn.execute(
            "SELECT artifact_id, revision FROM artifacts ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        run = db.latest_run(database)
        writer_row = conn.execute(
            """SELECT result_json FROM tasks WHERE role='writer' AND state='accepted'
               ORDER BY updated_at DESC LIMIT 1"""
        ).fetchone()
        if artifact_row is None or run is None or writer_row is None:
            raise RuntimeError("there is no committed report to export")
        broker = Broker(conn, root)
        artifact, content = broker.get_artifact(
            artifact_row["artifact_id"], artifact_row["revision"]
        )
        writer = AgentResult.model_validate_json(writer_row["result_json"])
        requirements = db.list_requirements(database)
        assessment = assess(
            conn,
            requirements=requirements,
            artifact=artifact,
            content=content,
            coverage=writer.payload.get("coverage", {}),
            limitations=writer.limitations,
        )
        return export_run(
            conn,
            project_root=root,
            artifact=artifact,
            content=content,
            assessment=assessment,
            outcome=run["outcome"] or run["status"],
            broker=broker,
            assurance="Application-level brokerage; no operating-system sandbox.",
            run_id=run["id"],
            cost={"model_calls": run["model_calls"]},
        )
    finally:
        conn.close()
