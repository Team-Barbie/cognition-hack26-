"""Context packet: the only input an agent ever receives. Not a chat transcript."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ar.models import AgentResult, ClaimDraft, LineSelector, ProposedTask, Record, Requirement, Role, SourceRef, TaskOutcome


class UpstreamState(Record):
    """Controller-recorded progress; payloads remain data, never evidence or authority."""

    task_id: str
    role: Role
    outcome: TaskOutcome
    partial_artifacts: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    proposed_tasks: list[ProposedTask] = Field(default_factory=list)


class HandoffState(Record):
    schema_version: Literal["1"] = "1"
    goal: str
    constraints: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    upstream: list[UpstreamState] = Field(default_factory=list)


class DeliveredRead(Record):
    """Content actually delivered to this task attempt, with its receipt."""

    source_ref: SourceRef
    selector: LineSelector
    content: str
    read_receipt_id: str
    label: str = ""  # e.g. "brief", "paper-a"

    def numbered(self) -> str:
        """Content with one-based line numbers matching `selector`."""
        lines = self.content.splitlines()
        return "\n".join(f"{self.selector.start + i}: {line}" for i, line in enumerate(lines))


class SourceInfo(Record):
    source_id: str
    path: str


class DependencyItem(Record):
    """An accepted upstream output: an artifact version or a committed claim."""

    kind: Literal["artifact", "claim"]
    id: str
    revision: int = 1
    content: str = ""
    claim: ClaimDraft | None = None  # set when kind == "claim"


class Packet(Record):
    schema_version: Literal["1"] = "1"
    packet_id: str
    task_id: str
    attempt_id: str
    role: Role
    objective: str
    requirements: list[Requirement] = Field(default_factory=list)
    reads: list[DeliveredRead] = Field(default_factory=list)
    dependencies: list[DependencyItem] = Field(default_factory=list)
    available_sources: list[SourceInfo] = Field(default_factory=list)
    review_feedback: list[str] = Field(default_factory=list)  # blocking findings on a rewrite
    skills: list[str] = Field(default_factory=list)  # method files to load; they add no capabilities
    model_call_budget: int | None = Field(default=None, ge=0)
    review_context: dict[str, Any] = Field(default_factory=dict)
    handoff: HandoffState | None = None

    def with_handoff(self, previous: list[AgentResult] | tuple[AgentResult, ...]) -> Packet:
        """Carry recorded state across specialties, including incomplete work and gaps.

        Claim drafts are deliberately excluded: only broker-accepted dependencies are evidence.
        """
        state = HandoffState(
            goal=self.handoff.goal if self.handoff else self.objective,
            constraints=(self.handoff.constraints if self.handoff else [
                requirement.interpretation for requirement in self.requirements
            ]),
            requirements=self.handoff.requirements if self.handoff else self.requirements,
            upstream=[UpstreamState(
                task_id=result.task_id, role=result.role, outcome=result.outcome,
                partial_artifacts=result.payload, limitations=result.limitations,
                open_questions=result.unresolved, proposed_tasks=result.proposed_tasks,
            ) for result in previous],
        )
        return self.model_copy(update={"handoff": state})

    def validate_delivery(self, role: Role) -> Packet:
        """Revalidate even model_copy updates before a receiver uses the packet."""
        packet = Packet.model_validate(self.model_dump(mode="json"))
        if packet.role != role:
            raise ValueError(f"packet role {packet.role!r} does not match requested role {role!r}")
        for name in ("packet_id", "task_id", "attempt_id", "objective"):
            if not getattr(packet, name).strip():
                raise ValueError(f"{name} must not be blank")
        identities = [(dep.kind, dep.id) for dep in packet.dependencies]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate dependency identity")
        for dep in packet.dependencies:
            if (dep.kind == "claim") != (dep.claim is not None):
                raise ValueError(f"dependency {dep.id}: kind does not match claim content")
        for read in packet.reads:
            if (read.selector.end - read.selector.start + 1 != len(read.content.splitlines())
                    or not read.read_receipt_id.strip()):
                raise ValueError("delivered read has an invalid line range or missing receipt")
        if packet.handoff:
            if not packet.handoff.goal.strip():
                raise ValueError("handoff goal must not be blank")
            task_ids = [item.task_id for item in packet.handoff.upstream]
            if packet.task_id in task_ids or len(task_ids) != len(set(task_ids)):
                raise ValueError("handoff contains duplicate tasks or a self dependency")
        return packet

    def read_for(self, label: str) -> DeliveredRead | None:
        return next((r for r in self.reads if r.label == label), None)

    def claim_items(self) -> list[DependencyItem]:
        return [d for d in self.dependencies if d.kind == "claim"]

    def audit_view(self) -> dict:
        """JSON-safe view of the exact bounded context delivered to an agent attempt."""
        reads = []
        for read in self.reads:
            reads.append({
                "label": read.label,
                "source": read.source_ref.source_id,
                "revision": read.source_ref.revision,
                "start": read.selector.start,
                "end": read.selector.end,
                "receipt": read.read_receipt_id,
                "lines": [
                    {"n": read.selector.start + offset, "text": line}
                    for offset, line in enumerate(read.content.splitlines())
                ],
            })
        dependencies = []
        for dep in self.dependencies:
            dependencies.append({
                "kind": dep.kind,
                "id": dep.id,
                "revision": dep.revision,
                "content": dep.content,
                "claim": dep.claim.model_dump(mode="json") if dep.claim else None,
            })
        return {
            "schema_version": self.schema_version,
            "handoff": self.handoff.model_dump(mode="json") if self.handoff else None,
            "id": self.packet_id,
            "task": self.task_id,
            "attempt": self.attempt_id,
            "role": self.role,
            "objective": self.objective,
            "requirements": [requirement.model_dump(mode="json") for requirement in self.requirements],
            "reads": reads,
            "dependencies": dependencies,
            "available_sources": [source.model_dump(mode="json") for source in self.available_sources],
            "review_feedback": list(self.review_feedback),
            "skills": list(self.skills),
            "review_context": self.review_context,
            "model_call_budget": self.model_call_budget,
        }
