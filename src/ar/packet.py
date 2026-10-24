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
