"""Frozen wave 0 records. Pydantic is the only schema source."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

Role = Literal[
    "planner", "evidence_reader", "writer", "reviewer", "source_scout", "theorist", "analyst",
    "experiment_runner", "reproducer", "research_agent", "adversarial_effect_agent",
]
TaskOutcome = Literal["produced", "needs_dependency", "unable_to_complete"]
EvidenceRelation = Literal["supports", "contradicts", "qualifies"]
EpistemicStatus = Literal[
    "user_given", "source_reported", "experiment_observed", "inferred", "hypothesized"
]
Severity = Literal["blocking", "note"]


class Record(BaseModel):
    """Base for every public record: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class SourceRef(Record):
    source_id: str
    revision: int = 1


class LineSelector(Record):
    """One-based inclusive line range."""

    kind: Literal["lines"] = "lines"
    start: int = Field(ge=1)
    end: int = Field(ge=1)


class Requirement(Record):
    id: str
    revision: int = 1
    source_passage: str
    interpretation: str
    acceptance_checks: list[str] = Field(default_factory=list)
    deliverable: str = "report.md"


class ReadReceipt(Record):
    id: str
    source_ref: SourceRef
    representation_sha256: str
    selector: LineSelector
    task_id: str
    attempt_id: str
    packet_id: str
    delivered_at: str


class TaskContract(Record):
    task_id: str
    contract_revision: int = 1
    role: Role
    objective: str
    requirement_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    input_refs: list[SourceRef] = Field(default_factory=list)
    required_reads: list[SourceRef] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    max_model_calls: int = 4


class ArtifactVersion(Record):
    artifact_id: str
    revision: int
    artifact_type: str
    content_type: str = "text/markdown"
    content_sha256: str
    producing_task: str
    parent_revision: int | None = None
    depends_on: list[str] = Field(default_factory=list)


class EvidenceLink(Record):
    relation: EvidenceRelation
    source_ref: SourceRef
    locator: LineSelector
    quote: str
    read_receipt_id: str


class ClaimDraft(Record):
    """A claim as an agent proposes it, before the runtime assigns an id."""

    statement: str
    kind: str = "finding"
    epistemic_status: EpistemicStatus = "source_reported"
    scope: str = ""
    evidence_links: list[EvidenceLink] = Field(default_factory=list)
    derivation: str = ""
    limitations: list[str] = Field(default_factory=list)


class Claim(ClaimDraft):
    id: str
    origin_task: str


class ProposedTask(Record):
    role: Role
    objective: str
    source_ids: list[str] = Field(default_factory=list)


class AgentResult(Record):
    """Typed handoff. `payload` is validated per role by the agents package."""

    schema_version: str = SCHEMA_VERSION
    role: Role
    task_id: str
    outcome: TaskOutcome
    payload: dict[str, Any] = Field(default_factory=dict)
    claims: list[ClaimDraft] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    proposed_tasks: list[ProposedTask] = Field(default_factory=list)
    acceptance_assessment: dict[str, bool] = Field(default_factory=dict)
    model_calls: int = 0


class ReviewFinding(Record):
    severity: Severity
    message: str
    claim_id: str | None = None


class ReviewResult(Record):
    reviewed_artifact_id: str
    reviewed_revision: int
    checks: dict[str, bool] = Field(default_factory=dict)
    findings: list[ReviewFinding] = Field(default_factory=list)
    disposition: Literal["accept", "revise"]
