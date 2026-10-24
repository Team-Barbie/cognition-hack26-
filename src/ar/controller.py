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
    resume_run_id: str | None = None,
) -> RunResult:
    """Run the fixed Wave 1 sequence and persist each task before dispatch.

    Packet builders receive accepted earlier results. Reading, receipts, claims,
    and artifacts remain the responsibility of their owning integrations.
    """

    _validate_workflow(steps)
    if resume_run_id is None:
        run_id = db.create_run(database, max_model_calls)
        calls = 0
        budget = max_model_calls
    else:
        run = db.read_run(database, resume_run_id)
        if run["status"] != "running":
            raise ValueError(f"run {resume_run_id} cannot be resumed from {run['status']}")
        run_id = resume_run_id
        calls = run["model_calls"]
        budget = run["max_model_calls"]
    results: list[AgentResult] = []
    try:
        for index, step in enumerate(steps, start=1):
            task_id = f"{run_id}-task-{index}"
            task = db.read_task(database, task_id)
            if task is not None and task["state"] == "accepted":
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
            db.start_attempt(database, task_id, attempt_id)
            result = agent_runner(step.role, packet)
            calls += result.model_calls
            db.record_model_calls(database, run_id, calls)
            if result.outcome != "produced":
                db.finish_attempt(database, task_id, attempt_id, "failed")
                db.finish_run(database, run_id, "failed", calls)
                return RunResult(run_id, "failed", tuple([*results, result]), calls)
            result = result.model_copy(update={"task_id": task_id})
            db.accept_attempt(database, task_id, attempt_id, result)
            results.append(result)
    except Exception:
        db.finish_run(database, run_id, "failed", calls)
        raise

    db.finish_run(database, run_id, "completed", calls)
    return RunResult(run_id, "completed", tuple(results), calls)


def run_project(
    project_root: Path,
    agent,
    *,
    max_tasks: int = 12,
    max_model_calls: int = 30,
) -> RunOutcome:
    """Run the inspectable markdown fixture path through the real broker boundary."""

    root = project_root.resolve()
    database = root / ".research" / "state.sqlite"
    conn = db.connect(database)
    broker = Broker(conn, root)
    brief_path = root / "input" / "brief.md"
    paper_paths = sorted((root / "input" / "papers").glob("*.md"))
    if not paper_paths:
        raise ValueError("the project has no markdown papers in input/papers")
    brief_ref = broker.register_source("brief", brief_path)
    paper_refs = [(path.stem, broker.register_source(path.stem, path), path) for path in paper_paths]
    if max_tasks < 3:
        conn.close()
        return RunOutcome.BLOCKED
    paper_refs = paper_refs[: max_tasks - 3]
    brief_text = brief_path.read_text(encoding="utf-8")
    question = next(
        (line.split(":", 1)[1].strip() for line in brief_text.splitlines() if line.startswith("Question:")),
        "Review the supplied research sources.",
    )
    requirements = [
        Requirement(
            id="req-baseline",
            source_passage=question,
            interpretation="Identify the baseline supported by the literature.",
        ),
        Requirement(
            id="req-exclusion",
            source_passage="Series shorter than 52 weeks are excluded from the study.",
            interpretation="Check whether the exclusion rule holds and report contradictions.",
        ),
    ]
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
            "SELECT id FROM attempts WHERE task_id=? AND status='accepted' ORDER BY number DESC LIMIT 1",
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
                draft = ClaimDraft.model_validate(
                    claim.model_dump(exclude={"id", "origin_task"})
                )
                dependencies.append(DependencyItem(kind="claim", id=claim.id, claim=draft))
        return dependencies

    def writer_packet(previous, task_id, attempt_id):
        claims = claim_dependencies(previous)
        return Packet(
            packet_id=packet_id(task_id, attempt_id),
            task_id=task_id,
            attempt_id=attempt_id,
            role="writer",
            objective=question,
            requirements=requirements,
            dependencies=claims,
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
        writer = next(result for result in previous if result.role == "writer")
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
    active = db.latest_active_run(database)
    try:
        run = run_workflow(
            database,
            steps,
            agent.run,
            max_model_calls=max_model_calls,
            resume_run_id=active,
        )
        if run.status == "budget_exhausted":
            return RunOutcome.BUDGET_EXHAUSTED
        if run.status != "completed":
            return RunOutcome.FAILED
        writer = next(result for result in run.results if result.role == "writer")
        reviewer = next(result for result in run.results if result.role == "reviewer")
        content = writer.payload["content_markdown"]
        claims = claim_dependencies(run.results)
        artifact = report_artifact(writer, content, claims)
        gate_result = gate(
            conn, ReviewResult.model_validate(reviewer.payload), artifact=artifact
        )
        if gate_result.decision is GateDecision.needs_revision:
            return RunOutcome.BLOCKED
        assessment = assess(
            conn,
            requirements=requirements,
            artifact=artifact,
            content=content,
            coverage=writer.payload.get("coverage", {}),
            limitations=[*writer.limitations, *gate_result.limitations],
        )
        if not assessment.complete:
            return RunOutcome.BLOCKED
        export_run(
            conn,
            project_root=root,
            artifact=artifact,
            content=content,
            assessment=assessment,
            outcome=RunOutcome.COMPLETED.value,
            assurance="Application-level brokerage; no operating-system sandbox.",
            run_id=run.run_id,
        )
        return RunOutcome.COMPLETED
    finally:
        conn.close()
