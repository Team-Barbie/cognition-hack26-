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
from ar.models import AgentResult, Role
from ar.packet import Packet


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"


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


PacketBuilder = Callable[[Sequence[AgentResult]], Packet]


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
) -> RunResult:
    """Run the fixed Wave 1 sequence and persist each task before dispatch.

    Packet builders receive accepted earlier results. Reading, receipts, claims,
    and artifacts remain the responsibility of their owning integrations.
    """

    _validate_workflow(steps)
    run_id = db.create_run(database, max_model_calls)
    results: list[AgentResult] = []
    calls = 0
    try:
        for index, step in enumerate(steps, start=1):
            task_id = f"{run_id}-task-{index}"
            db.create_task(database, run_id, task_id, step.role)
            packet = step.build_packet(tuple(results))
            if packet.role != step.role:
                raise ValueError(f"packet role {packet.role!r} does not match step role {step.role!r}")
            if packet.task_id != task_id:
                packet = packet.model_copy(update={"task_id": task_id})
            attempt_id = f"attempt-{uuid4()}"
            if packet.attempt_id != attempt_id:
                packet = packet.model_copy(update={"attempt_id": attempt_id})
            db.start_attempt(database, task_id, attempt_id)
            result = agent_runner(step.role, packet)
            calls += result.model_calls
            if calls > max_model_calls:
                db.finish_attempt(database, task_id, attempt_id, "failed")
                db.finish_run(database, run_id, "budget_exhausted", calls)
                return RunResult(run_id, "budget_exhausted", tuple(results), calls)
            if result.outcome != "produced":
                db.finish_attempt(database, task_id, attempt_id, "failed")
                db.finish_run(database, run_id, "failed", calls)
                return RunResult(run_id, "failed", tuple([*results, result]), calls)
            result = result.model_copy(update={"task_id": task_id})
            db.finish_attempt(database, task_id, attempt_id, "accepted")
            results.append(result)
    except Exception:
        db.finish_run(database, run_id, "failed", calls)
        raise

    db.finish_run(database, run_id, "completed", calls)
    return RunResult(run_id, "completed", tuple(results), calls)
