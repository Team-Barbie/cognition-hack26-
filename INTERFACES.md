# Cross-track interfaces (waves 1-2)

Binding signatures between tracks. Names are frozen; add optional fields by asking. `models.py` and
`packet.py` already exist and are the schema source. Python 3.12+, sqlite3 from the stdlib, no new
runtime dependencies beyond pydantic, typer, anthropic.

## Project layout on disk

```text
<project>/input/brief.md, input/papers/*.md   # human-authored, never written by the runtime
<project>/.research/state.sqlite              # one database
<project>/.research/blobs/<sha256>            # immutable content-addressed bytes
<project>/output/                             # report.md, run-summary.md, provenance.json
```

Source ids: `brief` for `input/brief.md`; the file stem for papers (`paper-a`). Revision is always 1
in the MVP. Line numbers are one-based inclusive.

## Database ownership

Each module creates and owns its own tables through `ensure_schema(conn: sqlite3.Connection)`.
Spine's `db.init_project()` calls all of them. Every connection sets `row_factory = sqlite3.Row`,
`PRAGMA foreign_keys=ON`, and uses explicit transactions (`with conn:`).

| Module | Tables |
| --- | --- |
| broker.py (Contracts) | `sources`, `read_receipts`, `artifacts` |
| evidence.py (Quality) | `claims`, `evidence_links` |
| db.py (Spine) | `runs`, `tasks`, `attempts`, `events` |

## Contracts: `ar.broker` and `ar.snapshots`

```python
class BrokerError(Exception):
    code: str          # SOURCE_NOT_FOUND, ACCESS_DENIED, STALE_REVISION, INVALID_SELECTOR,
                       # REQUIRED_READ_INCOMPLETE, REVISION_CONFLICT, EVIDENCE_REJECTED, ...
    message: str
    details: dict      # e.g. {"missing": [[start, end], ...]}

def ensure_schema(conn) -> None

class Broker:
    def __init__(self, conn, project_root: Path) -> None   # blobs in project_root/.research/blobs

    # sources (snapshot on register; reads come from the snapshot, never the live file)
    def register_source(self, source_id: str, path: Path) -> SourceRef
    def source_line_count(self, ref: SourceRef) -> int
    def list_sources(self) -> list[SourceRef]

    # reads: issues a ReadReceipt bound to (task_id, attempt_id, packet_id); returns what was delivered
    def read(self, *, task_id, attempt_id, packet_id, ref: SourceRef,
             start: int | None = None, end: int | None = None, label: str = "") -> DeliveredRead
    def receipts_for(self, task_id: str, attempt_id: str) -> list[ReadReceipt]

    # gate: union of that attempt's receipts must cover each whole required source
    def check_required_reads(self, *, task_id, attempt_id, required: list[SourceRef]) -> None
        # raises BrokerError("REQUIRED_READ_INCOMPLETE", details={"missing": [...]})

    # evidence: receipt must belong to this task+attempt and cover the locator,
    # and the quote must sit inside the cited lines of the snapshot
    def verify_evidence_link(self, link: EvidenceLink, *, task_id, attempt_id) -> None
        # raises BrokerError("EVIDENCE_REJECTED", ...) on a bad quote, foreign receipt, or uncovered range

    # artifacts: immutable versions, optimistic concurrency
    def create_artifact(self, *, task_id, attempt_id, artifact_type, content: str,
                        depends_on: list[str] = []) -> ArtifactVersion
    def revise_artifact(self, *, task_id, attempt_id, artifact_id, expected_revision: int,
                        content: str, change_reason: str = "") -> ArtifactVersion
        # stale expected_revision -> BrokerError("REVISION_CONFLICT"); nothing is overwritten
    def get_artifact(self, artifact_id: str, revision: int | None = None) -> tuple[ArtifactVersion, str]

    # paths: used by anything that touches files
    def resolve_inside(self, rel: str | Path, root: Path) -> Path   # traversal/symlink escape -> ACCESS_DENIED
    def write_project_file(self, rel: str | Path, content: str) -> Path  # under input/ or .research/ -> ACCESS_DENIED
```

Quote matching is deterministic: compare after `str.splitlines()` joined with `\n`, whitespace runs
collapsed to one space, both sides. Version that rule in a constant `QUOTE_NORMALIZATION = "ws-collapse-v1"`.

## Spine: `ar.cli`, `ar.controller`, `ar.db`

```python
# db.py
def connect(project_root: Path) -> sqlite3.Connection
def init_project(project_root: Path, source_dir: Path | None = None) -> sqlite3.Connection
    # creates .research/, input/ skeleton if missing, state.sqlite; calls broker.ensure_schema,
    # evidence.ensure_schema, and its own ensure_schema; registers brief + papers as sources;
    # compiles brief.md into Requirement rows (one per non-empty "Label: text" line or bullet)

# controller.py
class RunOutcome(StrEnum): completed, blocked, budget_exhausted, failed, cancelled
def run_project(project_root: Path, agent: Agent, *, max_tasks: int = 12,
                max_model_calls: int = 30) -> RunOutcome
def build_packet(...)  # assembles agents.packet.Packet from broker.read results; the controller never calls a model
```

Loop (fixed, clamped): planner -> one `evidence_reader` per source file (planner proposals beyond
that are stored in `events` and ignored) -> writer -> reviewer; the reviewer may send the writer back
once (`needs_revision`), then the controller finishes with remaining blocking findings listed as
limitations. Task states: `pending, ready, running, accepted, needs_revision, failed`. Each execution
is a new `attempts` row. Accepted tasks are skipped on a second `run_project` and their artifact is
not duplicated. A stopped run (killed mid-loop) resumes from committed state. `max_model_calls` is
checked before every dispatch; hitting it keeps the draft and returns `budget_exhausted`.

The controller calls, in order, for each task: `broker.read` (packet build) -> `agents.run` ->
`broker.check_required_reads` -> role-specific accept step:
- planner: store proposed tasks (clamped).
- evidence_reader: `evidence.accept_claims(...)`.
- writer: `broker.create_artifact` / `broker.revise_artifact`.
- reviewer: `review.gate(...)`.
Finally `completion.assess(...)` then `export.export_run(...)`.

CLI (Typer, entry point `ar`): `init <dir> [--from <fixture dir>]`, `run <dir> [--fake] [--model M]
[--effort E] [--max-model-calls N]`, `status <dir>`, `inspect task|claim|reads <id> --project <dir>`,
`export <dir>`. `--fake` configures `FakeAgent`; otherwise `AnthropicClient` + `LiveAgent`.

## Quality: `ar.evidence`, `ar.review`, `ar.export`, `ar.completion`

```python
# evidence.py
class Verifier(Protocol):
    def verify_evidence_link(self, link: EvidenceLink, *, task_id: str, attempt_id: str) -> None  # raises on reject
@dataclass class Rejection: draft: ClaimDraft; reason: str
@dataclass class AcceptResult: accepted: list[Claim]; rejected: list[Rejection]
def ensure_schema(conn) -> None
def accept_claims(conn, verifier: Verifier, *, task_id, attempt_id, drafts: list[ClaimDraft]) -> AcceptResult
    # ids are `claim-<n>`, assigned here; a claim with a rejected link is rejected whole;
    # a claim with no evidence links is rejected unless epistemic_status is inferred/hypothesized
def get_claim(conn, claim_id) -> Claim | None
def list_claims(conn) -> list[Claim]
def inspect_claim(conn, claim_id: str) -> str   # statement, source, lines, quote, receipt id, origin task

# review.py
def gate(conn, review: ReviewResult, *, artifact: ArtifactVersion) -> GateDecision
    # decision in {"accept", "needs_revision", "accept_with_limitations"}; stores findings;
    # a review of a stale artifact revision is rejected

# completion.py
def assess(conn, *, requirements: list[Requirement], artifact: ArtifactVersion, content: str,
           coverage: dict[str, str], limitations: list[str]) -> CompletionAssessment
    # every requirement mapped, deliverable non-empty, every [claim:id] resolves to a committed claim;
    # per-requirement rows plus overall bool; no experiment requirement is manufactured

# export.py
def export_run(conn, *, project_root: Path, artifact: ArtifactVersion, content: str,
               assessment, outcome: str, assurance: str, run_id: str) -> list[Path]
    # writes output/report.md (each [claim:id] -> footnote with source, lines, quote),
    # output/run-summary.md (outcome, limitations, cost, assurance note), output/provenance.json;
    # reads committed claims only. `assurance` must say enforcement is application-level brokerage.
```

`CompletionAssessment` and `GateDecision` are small dataclasses/enums defined by Quality in
`completion.py` / `review.py`.
