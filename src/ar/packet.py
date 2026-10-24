"""Context packet: the only input an agent ever receives. Not a chat transcript."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ar.models import ClaimDraft, LineSelector, Record, Requirement, Role, SourceRef


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
        }
