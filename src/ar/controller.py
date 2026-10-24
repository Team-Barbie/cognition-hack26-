"""Research task orchestration boundaries for the Spine track.

Wave 0 deliberately contains no research loop. The controller will coordinate the
broker, agent runner, and quality gates; it must never call a model client itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ar import db
from ar.broker import Broker
from ar.completion import assess
from ar.evidence import accept_claims, list_claims
from ar.export import export_run
from ar.models import AgentResult, ClaimDraft, Requirement, ReviewResult, Role
from ar.packet import DependencyItem, Packet, SourceInfo
from ar.review import GateDecision, gate


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
    if roles[-1] != "reviewer":
        raise ValueError("workflow must end with one reviewer")
    if roles.count("planner") != 1 or roles.count("writer") != 1 or roles.count("reviewer") != 1:
        raise ValueError("workflow must contain one planner, one writer, and one reviewer")
    writer_index = roles.index("writer")
    reviewer_index = roles.index("reviewer")
    if any(role != "evidence_reader" for role in roles[1:writer_index]):
        raise ValueError("only evidence readers may run between planner and writer")
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
) -> RunResult:
    """Run the fixed Wave 1 sequence and persist each task before dispatch.

    Packet builders receive accepted earlier results. Reading, receipts, claims,
    and artifacts remain the responsibility of their owning integrations.
    """

    _validate_workflow(steps)
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
    try:
        for index, step in enumerate(steps, start=1):
            if db.pause_requested(database, run_id):
                return RunResult(run_id, "paused", tuple(results), calls)
            if index > max_tasks:
                db.finish_run(database, run_id, "budget_exhausted", calls)
                return RunResult(run_id, "budget_exhausted", tuple(results), calls)
            task_id = f"{run_id}-task-{index}"
            task = db.read_task(database, task_id)
            if task is not None and task["state"] == "accepted" and not task["is_stale"]:
                results.append(db.accepted_result(task))
                continue
            if calls >= budget:
                db.finish_run(database, run_id, "budget_exhausted", calls)
                return RunResult(run_id, "budget_exhausted", tuple(results), calls)
            if task is None:
                db.create_task(database, run_id, task_id, step.role)
            elif task["state"] == "running":
                db.recover_interrupted_task(database, task_id)
            else:
                raise RuntimeError(f"task {task_id} cannot run from {task['state']}")
            attempt_id = f"attempt-{uuid4()}"
            packet = step.build_packet(tuple(results), task_id, attempt_id)
            if packet.role != step.role:
                raise ValueError(f"packet role {packet.role!r} does not match step role {step.role!r}")
            if packet.task_id != task_id:
                packet = packet.model_copy(update={"task_id": task_id})
            if packet.attempt_id != attempt_id:
                packet = packet.model_copy(update={"attempt_id": attempt_id})
            lease = db.start_attempt(database, task_id, attempt_id)
            result = agent_runner(step.role, packet)
            calls += result.model_calls
            db.record_model_calls(database, run_id, calls)
            if result.outcome != "produced":
                db.finish_attempt(database, task_id, attempt_id, "failed")
                db.finish_run(database, run_id, "failed", calls)
                return RunResult(run_id, "failed", tuple([*results, result]), calls)
            result = result.model_copy(update={"task_id": task_id})
            db.accept_attempt(
                database,
                task_id,
                attempt_id,
                result,
                fencing_token=lease.fencing_token,
            )
            results.append(result)
    except Exception:
        db.finish_run(database, run_id, "failed", calls)
        raise

    if finalize:
        db.finish_run(database, run_id, "completed", calls)
        return RunResult(run_id, "completed", tuple(results), calls)
    return RunResult(run_id, "running", tuple(results), calls)


def run_project(
    project_root: Path,
    agent,
    *,
    max_tasks: int = 12,
    max_model_calls: int = 30,
    idempotency_key: str | None = None,
) -> RunOutcome:
    """Run the complete managed research loop and export its durable outcome."""

    root = project_root.resolve()
    database = root / ".research" / "state.sqlite"
    conn = db.connect(database)
    broker = Broker(conn, root)
    brief_path = root / "input" / "brief.md"
    paper_paths = sorted((root / "input" / "papers").glob("*.md"))
    if not brief_path.is_file():
        conn.close()
        raise FileNotFoundError(brief_path)
    if not paper_paths:
        conn.close()
        raise ValueError("the project has no markdown papers in input/papers")
    if max_tasks < 4:
        conn.close()
        return RunOutcome.BLOCKED

    brief_ref = broker.register_source("brief", brief_path)
    all_paper_refs = [
        (path.stem, broker.register_source(path.stem, path), path)
        for path in paper_paths
    ]
    current_refs = [brief_ref, *[ref for _, ref, _ in all_paper_refs]]
    versions = {}
    for ref in current_refs:
        row = conn.execute(
            """SELECT representation_sha256 FROM sources
               WHERE source_id=? AND revision=?""",
            (ref.source_id, ref.revision),
        ).fetchone()
        versions[ref.source_id] = row["representation_sha256"]
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
        )

    steps = [WorkflowStep(role="planner", build_packet=planner_packet)]
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

        steps.append(WorkflowStep(role="evidence_reader", build_packet=reader_packet))

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
        dependencies = []
        stored = list_claims(conn)
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
                if accepted.rejected:
                    raise RuntimeError(accepted.rejected[0].reason)
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

    def writer_packet(previous, task_id, attempt_id, feedback=None):
        return Packet(
            packet_id=packet_id(task_id, attempt_id),
            task_id=task_id,
            attempt_id=attempt_id,
            role="writer",
            objective=question,
            requirements=requirements,
            dependencies=claim_dependencies(previous),
            review_feedback=feedback or [],
        )

    def report_artifact(writer, content, claims):
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
            depends_on=[claim.id for claim in claims],
        )

    def reviewer_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        writer = next(result for result in reversed(previous) if result.role == "writer")
        content = writer.payload["content_markdown"]
        artifact = report_artifact(writer, content, claims)
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
            ],
        )

    steps.extend(
        [
            WorkflowStep(role="writer", build_packet=writer_packet),
            WorkflowStep(role="reviewer", build_packet=reviewer_packet),
        ]
    )

    def checked_agent(role, packet):
        result = agent.run(role, packet)
        unique = {
            (read.source_ref.source_id, read.source_ref.revision): read.source_ref
            for read in packet.reads
        }
        required = list(unique.values())
        if required:
            broker.check_required_reads(
                task_id=packet.task_id,
                attempt_id=packet.attempt_id,
                required=required,
            )
        return result

    db.recover_expired_leases(database)
    active = None if changed else db.latest_active_run(database)
    try:
        run = run_workflow(
            database,
            steps,
            checked_agent,
            max_model_calls=max_model_calls,
            max_tasks=max_tasks,
            resume_run_id=active,
            finalize=False,
            idempotency_key=idempotency_key,
        )
        if run.status == "paused":
            return RunOutcome.CANCELLED
        if run.status == "budget_exhausted":
            writers = [result for result in run.results if result.role == "writer"]
            if writers:
                writer = writers[-1]
                claims = claim_dependencies(run.results)
                content = writer.payload["content_markdown"]
                artifact = report_artifact(writer, content, claims)
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
                    assurance="Application-level brokerage; no operating-system sandbox.",
                    run_id=run.run_id,
                    cost={"model_calls": run.model_calls, "tasks": len(run.results)},
                )
            return RunOutcome.BUDGET_EXHAUSTED
        if run.status == "completed":
            return RunOutcome.COMPLETED
        if run.status == "blocked":
            return RunOutcome.BLOCKED
        if run.status != "running":
            return RunOutcome.FAILED

        planner = next(result for result in run.results if result.role == "planner")
        event_key = f"planner-proposals:{run.run_id}"
        if db.load_idempotent(database, "event", event_key) is None:
            proposals = [task.model_dump(mode="json") for task in planner.proposed_tasks]
            db.record_event(
                database,
                "planner_proposals",
                {"proposals": proposals, "clamped": True},
                run_id=run.run_id,
                task_id=planner.task_id,
            )
            db.save_idempotent(database, "event", event_key, {"stored": True})

        results = list(run.results)
        writer = next(result for result in reversed(results) if result.role == "writer")
        reviewer = next(result for result in reversed(results) if result.role == "reviewer")
        claims = claim_dependencies(results)
        content = writer.payload["content_markdown"]
        artifact = report_artifact(writer, content, claims)
        gate_result = gate(
            conn, ReviewResult.model_validate(reviewer.payload), artifact=artifact
        )
        calls = run.model_calls

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
                assurance="Application-level brokerage; no operating-system sandbox.",
                run_id=run.run_id,
                cost={"model_calls": calls, "tasks": len(results)},
            )

        def execute_extra(role, ordinal, make_packet):
            nonlocal calls
            if db.pause_requested(database, run.run_id):
                return None, "paused"
            if ordinal > max_tasks or calls >= max_model_calls:
                db.finish_run(database, run.run_id, "budget_exhausted", calls)
                return None, "budget_exhausted"
            task_id = f"{run.run_id}-task-{ordinal}"
            task = db.read_task(database, task_id)
            if task is not None and task["state"] == "accepted" and not task["is_stale"]:
                return db.accepted_result(task), "accepted"
            if task is None:
                db.create_task(database, run.run_id, task_id, role)
            elif task["state"] == "running":
                db.recover_interrupted_task(database, task_id)
            else:
                raise RuntimeError(f"task {task_id} cannot run from {task['state']}")
            attempt_id = f"attempt-{uuid4()}"
            packet = make_packet(task_id, attempt_id)
            lease = db.start_attempt(database, task_id, attempt_id)
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
            project_root=root,
            artifact=artifact,
            content=content,
            assessment=assessment,
            outcome=outcome.value,
            assurance="Application-level brokerage; no operating-system sandbox.",
            run_id=run.run_id,
            cost={"model_calls": calls, "tasks": len(results)},
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
            assurance="Application-level brokerage; no operating-system sandbox.",
            run_id=run["id"],
            cost={"model_calls": run["model_calls"]},
        )
    finally:
        conn.close()
