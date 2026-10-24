# Autoresearch with Explicit Contracts and Specialized Agents

**Status:** Proposed project specification  
**Date:** 24 October 2026  
**Purpose:** A standalone build brief for a new research system. This document describes proposed behavior, not features already implemented in the current repository.

## 1. Product definition

Build a local research system that takes a research question, background material, constraints, and requested deliverables, then coordinates specialized agents to investigate the question and produce a reviewed result.

The system should support literature review, theoretical investigation, computational experiments, reproduction, analysis, and synthesis. It must choose and revisit these activities as the research develops. An experiment is optional when the objective can be met through literature or theory.

The central design decision is to make research work explicit and verifiable through contracts. Each task declares what an agent must read, which tools it may use, what it must produce, and what must be checked before its result can be accepted. File access, evidence, experiments, handoffs, and completion all use versioned records.

Agents make research judgments. A deterministic runtime enforces contracts and records what actually happened.

### Goals

- Turn broad questions into bounded, testable research tasks without losing the original objective.
- Give specialists focused context and clear responsibilities instead of sharing an ever-growing conversation.
- Know which exact file versions and passages were supplied to each agent.
- Preserve sources, assumptions, competing explanations, failed experiments, and uncertainty.
- Continue autonomously within the user's configured permissions and budget.
- Resume after interruption without silently duplicating work or losing decisions.
- Produce useful deliverables with traceable claims and independent review.

### Non-goals for the first release

- Guarantee scientific truth, novelty, or human-level understanding through schema validation.
- Require every project to run experiments, train models, or follow a fixed sequence of stages.
- Build a general-purpose desktop agent, public publishing platform, or distributed cluster scheduler.
- Treat every agent message as durable research memory.
- Automatically contact people, submit manuscripts, or purchase resources.

## 2. Project inputs and authority

The user supplies a brief, context, seed questions, optional sources/data, and a project configuration. Markdown holds human-authored material; YAML holds configuration; JSON Schema defines machine contracts.

```text
project/
  project.yaml                   # Objective settings, deliverables, budgets, adapters
  input/
    brief.md                     # Research question, scope, success criteria
    context.md                   # Assumptions, terminology, prior work
    questions.md                 # Seed questions
    papers/
    datasets/
    notes/
  policies/
    permissions.yaml             # Capabilities and approval rules
  skills/                        # Versioned research methods
  .research/                     # Runtime-owned; agents cannot edit directly
    state.sqlite                 # Tasks, claims, decisions, receipts, event log
    blobs/                       # Immutable content addressed by SHA-256
    manifests/                   # Versioned source and artifact indexes
    workspaces/                  # Isolated task scratch directories
  output/
    deliverables/                # Exported, reviewed artifact versions
    run-summary.md               # Outcome, limitations, cost, remaining work
    provenance.json              # Portable reference and receipt manifest
```

The brief and explicit human directions define the objective. Skills and agent plans cannot override them. The runtime must also enforce host restrictions and the user's permission policy; conflicting instructions become a reported configuration issue. Retrieved documents, tool output, and messages from other agents are research data, not sources of authority.

At initialization, the runtime snapshots the inputs and compiles the brief into individually identified requirements. Each requirement records its source passage, interpretation, proposed acceptance checks, and expected deliverable. An agent may propose this interpretation; it may not silently narrow the objective. Ambiguities are recorded, and only ambiguities that prevent useful progress require user input.

Changes to the brief, permissions, skills, or input files create new versions. A running task remains pinned to its declared research inputs; affected results become stale for the current objective until reassessed. New instructions do not rewrite historical records. Permission revocations take effect at dispatch and at every subsequent broker call, regardless of the task's original policy version. Cancel or restrict running managed workers when a revoked capability is still in use, and record any action that already occurred.

## 3. System architecture

| Component | Responsibility |
| --- | --- |
| Research controller | Maintains the objective and task graph; asks the planner for next actions; decides which validated tasks may run. |
| Contract engine | Validates schemas, dependencies, required reads, capabilities, output types, and acceptance gates. |
| Scheduler | Assigns tasks, reserves budgets, issues leases, limits concurrency, and resumes interrupted work. |
| Agent runner | Builds a bounded context packet and invokes the selected model/provider for a particular role. |
| Tool broker | Performs authorized file, retrieval, memory, and execution operations; issues receipts. |
| Evidence and memory service | Stores versioned sources, claims, hypotheses, contradictions, decisions, and their relationships. |
| Execution service | Runs computation in an isolated environment and captures code, inputs, logs, outputs, and resource use. |
| Review and completion service | Checks the actual candidate artifacts and evaluates requirement coverage. |
| CLI and MCP interface | Exposes the same runtime operations to a human, host assistant, or background worker. |

Use a local SQLite database and immutable blob store for the first implementation. Keep validated state changes and their events in one database transaction. Derive exports from committed state; do not maintain separate, competing sources of truth.

The model adapter accepts a context packet and returns a tool request or typed result. Provider-specific message formats stay behind this adapter. The same model may fill multiple roles, but each assignment receives a separate task context and identity.

### Enforcement boundary

In **managed execution**, agents access project data and tools through the broker. Experiment workers receive only mounted inputs, a writable scratch directory, and explicitly granted network/compute capabilities. The runtime database and evidence store are not writable mounts. If this isolation is unavailable, the runtime must report that limitation and refuse operations whose contract requires it.

In **host-assisted execution**, a connected coding assistant may have tools outside the runtime. Imported actions are labeled `host_reported`; the system cannot claim to have enforced their original execution permissions. It can snapshot and validate imported artifacts, and separately reproduce an action through managed execution.

Tool allowlists alone are not an operating-system sandbox. Both modes must describe their actual assurance level in the run record and final provenance.

## 4. Contract model

Publish authoritative JSON Schemas for all public records and tool arguments. Generate typed runtime models from those schemas, or generate schemas from one authoritative typed model library. Do not maintain two manually synchronized definitions.

All requests carry `schema_version` and `request_id`. Project-scoped operations also require `project_id`; run-scoped operations add `run_id`; task-scoped operations add `task_id`, `contract_revision`, and `attempt_id`. Initialization is the explicit exception that creates a project identity. Each operation's schema defines its scope, so project initialization and status checks do not require fictional task identities. Mutating requests also carry an `idempotency_key`. The broker obtains the authenticated agent identity from its session; an agent-supplied identity is never trusted as authorization.

All responses carry `request_id`, `status`, and either `result` or a structured `error`. Errors include `code`, `message`, `retryable`, and actionable `details`. Timestamps use UTC; hashes identify exact bytes. Unknown fields, unsupported schema versions, and invalid enum values are rejected.

| Record | Required meaning |
| --- | --- |
| `Requirement` | User intent, source passage, acceptance criteria, deliverable mapping, and revision. |
| `TaskContract` | Role, objective, dependencies, input references, required reads, capabilities, budgets, and output contract. |
| `SourceSnapshot` | Original bytes, extracted representations, acquisition details, content hashes, and provenance. |
| `ReadReceipt` | Exact source version and spans delivered to a particular task attempt and context packet. |
| `ArtifactVersion` | Immutable output bytes, producing task, parent version, type, and dependency references. |
| `Claim` | Statement, epistemic status, scope, evidence links, derivation, and limitations. |
| `ExperimentSpec` / `ExperimentRun` | Planned method and a separate record of actual execution. |
| `AgentResult` | Typed outputs, evidence references, limitations, unresolved issues, and proposed follow-up work. |
| `ReviewResult` | Exact reviewed versions, checks, findings, severity, and disposition. |
| `CompletionRecord` | Requirement-by-requirement assessment, reviewed deliverables, outcome, and remaining uncertainty. |

Examples below show selected payloads; the common envelope is omitted for readability. Example identifiers and hashes are illustrative. Production validation must use complete schemas and real server-issued identifiers.

## 5. Explicit reading contracts

Reading must be a first-class dependency, not a sentence in a prompt such as “read the files first.”

### Required-read declaration

Every task declares required reading before it can execute dependent actions or submit a result. Requirements identify immutable snapshots and typed selectors, not merely filenames or tool names.

```yaml
required_reads:
  - id: read-brief
    source_ref: {source_id: src-brief, revision: 3}
    representation: normalized_text
    selector: {kind: whole_document}
    required_before: [execute, submit]
    acknowledgement: constraints
  - id: read-method
    source_ref: {source_id: src-method, revision: 1}
    representation: normalized_text
    selector: {kind: section, section_id: evaluation-protocol}
    required_before: [execute, submit]
    acknowledgement: method_summary
```

Planning tasks must read the brief and applicable constraints before their plan is accepted. Specialists receive the same authoritative objective plus only the evidence, skills, and dependency outputs needed for their assignment. The runtime may preload required content, but it must record the actual delivered spans just as it would for an explicit read call.

For large files, requirements can specify sections, pages, table ranges, or a declared inspection procedure. Reading a dataset schema and a sample is recorded as that limited inspection; it does not count as reading the entire dataset. Whole-dataset computations instead need execution provenance covering the input snapshot.

### `files.read` request

```json
{
  "source_ref": {"source_id": "src-paper-017", "revision": 2},
  "representation": "normalized_text",
  "selector": {"kind": "lines", "start": 120, "end": 180},
  "max_bytes": 16000,
  "purpose": "Check the baseline definition and exclusions"
}
```

### Successful result

```json
{
  "source_ref": {"source_id": "src-paper-017", "revision": 2},
  "representation_id": "repr-paper-017-text-v1",
  "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "delivered_selector": {"kind": "lines", "start": 120, "end": 142},
  "content": "Exact text from the pinned representation is returned here.",
  "coverage": "partial",
  "truncated": true,
  "next_cursor": "opaque-cursor-bound-to-source-and-selector",
  "read_receipt_id": "read-0081"
}
```

### Required behavior

1. Resolve initial file paths against authorized roots, then snapshot them. Reject traversal, unauthorized absolute paths, and symlink/junction escapes. Read from the verified snapshot to avoid a file changing between validation and use.
2. Keep original bytes and separately versioned normalized representations. Record extraction tool/version, OCR use, extraction failures, and page/section mapping. A PDF's extracted text hash is distinct from the PDF's original hash.
3. Use one-based inclusive line, page, and row selectors. Use zero-based half-open byte selectors. Every selector declares its kind; opaque continuation cursors stay bound to the same snapshot and original request.
4. Return exact coverage and explicit truncation. Search snippets, file listings, metadata, and another agent's summary do not satisfy a raw-source read requirement.
5. Issue receipts only for content delivered to the agent. Bind receipts to source revision, representation hash, exact spans, authenticated agent, task attempt, context packet, and delivery timestamp. Content fetched internally but omitted from the final context earns no delivery receipt.
6. Track overlapping ranges as a union. A required read passes only when the union covers its required selector, using the same pinned representation. Missing, unreadable, or truncated content remains incomplete.
7. Keep coverage scoped to the task attempt. A new specialist does not inherit another specialist's receipts. On resume, the runtime records which prior content is actually included in the new attempt's context and issues linked delivery receipts for that content. Lost or compacted required content must be reinjected before a dependent action, or replaced by a contract-permitted, provenance-linked digest. A digest never satisfies a requirement explicitly marked as raw-source or visual inspection.
8. For constraint and method reads, require an acknowledgement citing source spans and stating relevant constraints or methodological obligations. The acknowledgement supports review; it does not prove comprehension.
9. Rendered pages and images may satisfy visual-read requirements when the model receives that modality. Text-only extraction cannot satisfy a requirement to inspect a figure.
10. Treat instructions found inside research files as untrusted source content. Reading a document never grants new permissions.

A read receipt proves delivery of specified content. It does not prove that the model understood it or that the content is true. Semantic adequacy requires specialist and reviewer judgment.

Expected failures include `SOURCE_NOT_FOUND`, `ACCESS_DENIED`, `STALE_REVISION`, `UNSUPPORTED_REPRESENTATION`, `INVALID_SELECTOR`, `EXTRACTION_FAILED`, and `REQUIRED_READ_INCOMPLETE`. Return missing spans and recovery options without fabricating success.

## 6. Writing and artifact contracts

Agents write through `artifacts.create` and `artifacts.revise`. They cannot modify `input/`, policy files, committed source snapshots, another task's scratch space, or the runtime database.

```json
{
  "artifact_id": "artifact-analysis-04",
  "expected_revision": 2,
  "artifact_type": "analysis_report",
  "content_type": "text/markdown",
  "content": "Revised analysis content...",
  "depends_on": [
    {"kind": "experiment_run", "id": "exp-009", "revision": 1},
    {"kind": "claim", "id": "claim-021", "revision": 2}
  ],
  "change_reason": "Add the sensitivity analysis requested in review"
}
```

Creation omits `artifact_id` and `expected_revision`; the runtime assigns identity. Revision requires optimistic concurrency against the current revision and produces a new immutable version. A stale writer receives `REVISION_CONFLICT`; it cannot overwrite intervening work.

Validate output type, destination, size, references, and task ownership before committing. For multiple outputs, stage blobs and commit the artifact manifests, result, and event atomically. Only committed versions may become dependencies or final deliverables. Orphaned staged blobs are recoverable cleanup work, not accepted outputs.

Every artifact records provenance. Runtime-captured, host-reported, and agent-authored content remain distinguishable. A polished report is not independent evidence for its own claims.

An `imports.register` operation snapshots external or host-produced files and records acquisition URL or command, timestamp, author/executor when known, and assurance level. Importing a successful-looking log does not turn it into a runtime-verified experiment.

## 7. Specialized agents and task contracts

Specialization means separate responsibilities, context, output schemas, and tool capabilities. It does not require a different model for each role.

| Agent | Main responsibility | Required output | Boundary |
| --- | --- | --- | --- |
| Planner | Translate requirements into a dependency graph; choose useful next work and revise priorities. | `ResearchPlan`, requirement coverage map, scheduling proposals. | Cannot grant permissions, approve its own deliverables, or declare completion. |
| Source scout | Search for relevant and contrary literature; acquire permitted sources; record screening. | `SearchRecord`, source snapshots, inclusion/exclusion decisions. | Metadata and abstracts remain labeled as such. |
| Evidence reader | Read assigned material and extract claims, methods, limitations, and contradictions. | `EvidenceExtraction` with exact locators and receipts. | Cannot cite passages it was not supplied or treat a summary as the original source. |
| Theorist / experiment designer | Build hypotheses, competing explanations, falsifiers, and discriminating tests. | `HypothesisSet`, derivations, `ExperimentSpec`. | Proposed outcomes must stay distinct from observations. |
| Implementer / executor | Implement and run an accepted experimental specification. | Versioned code, execution requests, `ExperimentRun` references. | Cannot silently change the hypothesis, metric, or success threshold. |
| Analyst | Interpret recorded outputs; evaluate uncertainty, controls, robustness, and failure modes. | `AnalysisResult`, result claims, limitations. | Cannot rewrite raw outputs or label an unexecuted test as completed. |
| Synthesizer / writer | Connect evidence to the question and produce requested deliverables. | `SynthesisResult`, draft artifacts, claim-to-passage map. | Cannot promote speculation to established fact or invent citations. |
| Adversarial reviewer | Inspect candidate artifacts and evidence; challenge methods, claims, and coverage. | `ReviewResult` with findings tied to exact versions. | Uses a separate task context; cannot approve work it authored. |

Small projects can skip unnecessary roles or run them sequentially. Review remains a separate assignment. Role separation reduces correlated mistakes but does not guarantee independent reasoning when models or sources are shared.

### Task payload

```yaml
task_id: task-024
contract_revision: 1
role: evidence_reader
objective: Compare the definitions and limitations of two proposed baselines.
requirement_ids: [req-02]
depends_on: [task-018]
input_refs:
  - {kind: source, id: src-paper-017, revision: 2}
  - {kind: source, id: src-paper-019, revision: 1}
required_read_contract_id: reads-task-024-v1
skill_refs:
  - {name: literature_extraction, revision: 1}
allowed_tools: [files.read, sources.search_within, memory.query, artifacts.create]
output:
  schema: EvidenceExtraction.v1
  artifact_types: [baseline_comparison]
acceptance:
  - required_reads_complete
  - referenced_spans_verified
  - both_baselines_addressed
  - limitations_explicit
budget:
  max_model_calls: 12
  max_tool_calls: 40
  max_output_bytes: 100000
  max_wall_seconds: 600
```

The capability set is the intersection of role permissions, task permissions, project policy, and host restrictions. Read preconditions and budget checks apply at the broker, including before dependent writes or execution. Loaded skills add methods, not capabilities.

The planner proposes task contracts. The runtime validates their schemas, requirement references, dependency acyclicity, permissions, resource reservations, and feasible inputs before scheduling. An agent cannot spawn unrestricted children; additional work is a scheduling proposal using the same validation path.

### Handoff contract

Every agent submits `AgentResult` containing:

- A task outcome: `produced`, `needs_dependency`, or `unable_to_complete`.
- Typed payload and immutable artifact references.
- Claims and supporting evidence references, with read receipts where applicable.
- Limitations, contradictions, unresolved questions, and proposed next tasks.
- An assessment against every task acceptance criterion.

The runtime adds measured usage and execution metadata. A self-reported success is only a candidate result. Schema/reference validation and required semantic review determine acceptance. Downstream agents receive accepted output references and can retrieve the supporting source spans; they do not inherit authority from another agent's confidence.

## 8. Evidence, claims, and research memory

Use explicit records for requirements, questions, hypotheses, claims, contradictions, decisions, experiments, reviews, and artifacts. Chat summaries are navigational aids, not the canonical record.

A claim contains `statement`, `kind`, `epistemic_status`, `scope`, `evidence_links`, `derivation`, `limitations`, and `origin_task`. Suggested epistemic statuses are `user_given`, `source_reported`, `experiment_observed`, `inferred`, and `hypothesized`. These describe how a statement was obtained, not a numerical probability of truth.

Evidence links distinguish `supports`, `contradicts`, and `qualifies` and use typed references:

- A source passage: source revision, representation, exact locator, quote, and read receipt.
- A computational observation: experiment run, output hash, metric/table location, and analysis provenance.
- A derivation: upstream claim revisions, stated assumptions, and reasoning or proof artifact.

The runtime verifies reference existence, quote presence at the specified location, receipt coverage, and graph integrity. The reviewer checks whether the evidence actually supports the claim. Quote matching alone does not establish entailment, methodological soundness, or external validity.

Preserve original and normalized text; any quote-normalization rules must be deterministic and versioned. Duplicate papers or versions of the same underlying study must be linked so repeated citations are not counted as independent support.

Hypotheses require competing explanations and potential falsifiers. Computational claims must identify data, code, environment, and execution. Simulations support claims about the model under its assumptions; they do not, by themselves, validate a real-world mechanism.

Memory updates create revisions and record rationale. Claims may be challenged, superseded, or retracted without erasing their history. Unsupported ideas may remain as hypotheses but cannot pass as established findings. Cycles in claim-support relationships are rejected; derived claims must ultimately reach evidence or explicit assumptions.

## 9. Experiment contracts

Separate planned experiments from execution and interpretation.

An `ExperimentSpec` declares the hypothesis/question, pilot or confirmatory status, input versions, code version, command and arguments, working directory, dependency/environment manifest, seeds, controls, metrics, success criteria, resource limits, expected outputs, and stopping conditions. Criteria for confirmatory work are frozen before execution; subsequent changes create a new specification and disclose the deviation.

An `ExperimentRun` records actual start/end times, executor identity, environment, command, exit status, stdout/stderr references, output hashes, resource use, and deviations. Outcomes include `succeeded`, `failed`, `timed_out`, `cancelled`, and `execution_unknown`.

Successful process exit is not scientific success. Missing expected outputs, invalid measurements, absent controls, or failed analysis checks prevent acceptance of the corresponding research claim.

The analyst receives the specification and immutable raw results. Follow-up analysis has its own code and execution record. The reviewer can request reruns or targeted checks when necessary. Failed and null runs remain visible in the research history.

## 10. Research lifecycle and recovery

The controller operates a dependency graph, not a mandatory pipeline:

1. Snapshot inputs, compile requirements, and validate configuration.
2. Have the planner propose an initial investigation and deliverable plan.
3. Schedule ready specialist tasks within capability and budget limits.
4. Deliver required readings and collect structured outputs.
5. Validate results and update durable evidence/memory.
6. Ask the planner to address new questions, contradictions, or review findings.
7. Produce candidate deliverables and review the exact candidate versions.
8. Complete only when the objective gates pass; otherwise continue, report a genuine blocker, or stop at a configured limit.

Literature and design work may proceed in parallel. Analysis waits for its experiment outputs. Final review waits for candidate deliverables. A theory-only project may never schedule an executor.

### State and retry rules

Task states are `pending`, `ready`, `running`, `validating`, `accepted`, `needs_revision`, `blocked`, `failed`, `cancelled`, and `stale`. Each execution has its own attempt identity. Only accepted, non-stale dependency versions may satisfy a new task contract. Resuming or revising creates a fresh attempt with explicit inherited context and references.

Run outcomes distinguish `completed`, `blocked`, `budget_exhausted`, `failed`, and `cancelled`. `paused` is a resumable user-controlled state. Running out of tokens or time is not objective completion.

Persist task transitions, tool calls, receipts, artifacts, and decisions before advancing dependent work. Use leases and fencing tokens to reject late writes from a worker whose assignment has expired.

For mutating requests, scope each idempotency key to the project, run, task, and logical operation. Bind it to a digest of the canonical operation payload, exact input versions, and task contract revision; exclude transport request IDs, timestamps, and attempt IDs from that digest. After checking current authorization and the worker's lease, a matching duplicate returns the original committed result, including after resume. The same key with a different digest is rejected. A materially changed operation needs a new key. Transient tool failures receive bounded retries. Invalid model output receives structured validation feedback with bounded repair attempts.

External side effects cannot generally promise exactly-once execution. After a crash between execution and receipt persistence, reconcile the worker/job record. If the outcome cannot be established, record `execution_unknown` and require a deliberate retry decision; do not silently rerun it.

Reserve global and task budgets atomically before dispatch. Include model calls/tokens, cost when available, tool calls, compute time, wall time, and concurrency. Reconcile actual usage afterward. Reject new work that exceeds a hard limit, while retaining committed partial results.

Track repeated plans and evidence progress. After a configurable number of attempts with no new evidence, artifact improvement, or resolved criterion, require a changed strategy or an explicit blocked/limited outcome. Ordinary scientific uncertainty is not itself a blocker.

Ask the user only for essential missing information, changes outside the authorized scope, or actions that the configured policy actually requires them to approve. Continue independent work while such a dependency is pending.

## 11. Completion and review contracts

Completion is a runtime decision over evidence and accepted artifacts, never a phrase such as “research complete” in a model response.

A `CompletionRecord` must establish that:

1. Every current requirement maps to a concrete deliverable and an assessment. A requirement cannot disappear merely because the planner omitted it.
2. Deliverables exist as immutable, non-empty versions and satisfy their required formats and substantive acceptance criteria.
3. Citations resolve to exact source, claim, and experiment versions. Material claims have appropriate support, and speculation is labeled.
4. A reviewer has inspected the exact deliverable versions and relevant supporting evidence after their last revision.
5. Findings marked `blocking` are resolved with evidence or receive an explicit, recorded user waiver. An agent cannot waive them itself.
6. Contrary evidence, alternative explanations, methodological limitations, and uncertainty are addressed where applicable. Inapplicable checks include a reason.
7. The final export includes a provenance manifest, run outcome, limitations, and unresolved scientific questions.

The review identity must differ from the authoring assignment. Review contracts require reads of the candidate artifacts and relevant evidence. Reviews reference immutable versions; changing a reviewed artifact or a material dependency invalidates acceptance of the affected candidate until re-reviewed. Unrelated changes do not invalidate all prior reviews.

Completion does not require exhausting every possible research question. It requires meeting the scoped objective and deliverable criteria. Remaining uncertainty can be a valid finding when the evidence is reported honestly.

A blocked outcome identifies the affected requirements, failed dependencies, alternatives tried, and the essential external change needed to continue. A budget-limited outcome exports the best available drafts and an accurate unfinished-work list without labeling them completed.

## 12. Interfaces and observability

Proposed CLI commands:

```text
ar init <project-directory>
ar validate <project-directory>
ar run <project-directory>
ar status <project-directory>
ar inspect task <task-id>
ar inspect claim <claim-id>
ar inspect reads <task-id>
ar pause <run-id>
ar resume <run-id>
ar export <run-id>
```

The MCP interface exposes the same typed operations, including `project.status`, `tasks.next`, `tasks.submit`, `files.read`, `sources.search`, `sources.acquire`, `sources.search_within`, `artifacts.create`, `artifacts.revise`, `memory.query`, `memory.propose_update`, `experiments.execute`, `imports.register`, and `reviews.submit`.

Human-only actions such as changing permissions or recording a human direction use an authenticated control channel. Agents cannot label their own plans as human instructions. Both CLI and MCP call the same validation and persistence services.

Expose the current objective revision, task graph, input coverage, missing reads, pending approvals, budget consumption, review findings, and deliverable status. An inspect operation should answer: “Why did this agent make this claim, and what exact material was it given?”

Store model/provider configuration, context packet manifests, tool receipts, and redacted traces for debugging. Never include credentials in artifacts or exports. Reproducibility means preserving inputs and execution provenance; it does not promise identical model responses.

## 13. Implementation sequence

| Milestone | Scope | Exit condition |
| --- | --- | --- |
| 1. Contract foundation | Authoritative schemas, project validation, snapshots, blob storage, transaction/event model, read/write broker. | Required reads, range coverage, permissions, and version conflicts work with a fake agent. |
| 2. Specialist coordination | Task graph, scheduler, budgets, leases, context packets, model adapter, typed handoffs. | Planner, reader, and writer complete a small fixture project; restart preserves progress. |
| 3. Research integrity | Claims, source provenance, hypotheses, contradiction tracking, reviewer, completion gates. | A literature-only project exports a reviewed report with verifiable citations. |
| 4. Computational research | Isolated executor, experiment manifests, analyst workflow, execution recovery. | A small reproducible experiment links raw outputs to analysis and the final report. |
| 5. Host integration and hardening | MCP adapter, labeled host imports, export bundles, failure injection, observability. | A host-assisted run and a managed run expose truthful, distinct provenance and recover cleanly. |

Keep the first release local and small. One scheduler process and a configurable small number of concurrent workers are sufficient. Prioritize reliable contracts and useful research output before a dashboard, distributed workers, or automatic model routing.

## 14. Acceptance tests

These tests define observable product behavior; they are requirements for implementation, not claims that this specification implements them.

| Scenario | Required result |
| --- | --- |
| Agent submits without reading the brief | Reject with the unmet read requirement and missing coverage. |
| Required content was fetched but removed during context packing | No delivered receipt is credited for omitted content. |
| Large read is truncated | Return partial coverage and a continuation cursor; dependent gate stays closed. |
| A read cursor is reused against another revision | Reject; never mix coverage across snapshots. |
| Agent B supplies Agent A's receipt | Reject as evidence of Agent B's own required reading. |
| An input changes during a run | Preserve old task provenance and mark affected current-objective results for reassessment. |
| Two agents revise the same artifact | Accept one matching revision; reject the stale writer without data loss. |
| A quote exists elsewhere but not at the cited locator | Reject the evidence link or require an accurate locator. |
| A quote matches but does not support the claim | Deterministic checks may pass; semantic review flags the unsupported claim. |
| A generated report is cited as independent external confirmation | Reject that provenance classification and retain it as derived material. |
| A paper contains instructions to alter permissions | Preserve them as document content; no capability changes occur. |
| A worker attempts an input write or path/junction escape | Block the operation; managed sandbox also prevents direct bypass. |
| A capability is revoked while a task is running | Deny subsequent broker actions and cancel or restrict affected managed work. |
| A model retries the same committed mutation | Return the existing result without duplicate artifacts or ledger updates. |
| A lease expires and its old worker submits | Reject the stale write using the fencing token. |
| Process crashes after launching computation | Reconcile execution or mark it unknown; no blind duplicate execution. |
| Experiment exits successfully but required outputs are absent | Record process success separately and reject experiment-result acceptance. |
| Host imports an experiment log | Label it host-reported; do not claim runtime execution. |
| Writer revises a reviewed deliverable | Require review of the new version before completion. |
| One required deliverable is missing | Keep the run incomplete even if every scheduled task finished. |
| A theory-only project meets its requirements | Allow completion without manufacturing an experiment requirement. |
| Global budget is exhausted with tasks still ready | Stop dispatch, preserve drafts, and report `budget_exhausted`. |
| Run resumes after interruption | Restore accepted work and valid references; refresh required context without duplicating committed actions. |

Use deterministic fake agents and fixture sources for contract tests. Add a small end-to-end research fixture with known supporting and contradicting passages, plus an inexpensive seeded computation. Evaluate research usefulness separately from mechanical compliance: compare requirement coverage, citation correctness, reviewer findings, and cost against a simple single-agent baseline.

## 15. Definition of done for the first release

The first release is ready when a user can initialize a project, provide a brief and source material, run a bounded investigation, interrupt and resume it, and receive a reviewed deliverable with traceable claims. They can inspect each specialist's assignment, the exact input versions and spans delivered, the artifacts it produced, and why those artifacts were accepted.

The system must pass the contract and recovery tests above, demonstrate both literature-only and computational workflows, and clearly distinguish managed execution from host-reported evidence. Its success is a useful, inspectable research result with honest limitations.
