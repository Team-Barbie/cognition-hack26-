# Four-person plan: working research MVP, then depth

**Status:** Build plan for the team  
**Date:** 24 October 2026  
**Source:** `PROJECTSPEC.md`  
**Repo state at planning:** specification only, no implementation yet

The first thing that has to work is a literature run you can inspect. Four people keep the same ownership from that demo through later hardening, so the second phase is depth on a track they already know.

## Demo promise

`ar init` a brief and three short papers, `ar run`, and export a reviewed markdown report. Every material claim cites lines that agent was actually given. A quote outside that span is rejected. Stop the process and run again, and accepted work stays. A theory-only brief can finish with no experiment.

## Stack

Python 3.12, Pydantic v2 as the only schema source, SQLite, content-addressed blobs, Typer, pytest, and one OpenAI-compatible adapter plus a `FakeAgent` so the demo still runs with no API key. The run record states that enforcement is application-level brokerage.

JSON Schema is generated from the Pydantic models. Do not maintain a second hand-written schema.

## The four tracks

Each person keeps the same ownership from the first commit through later hardening.

### 1 · Spine

Owns `cli.py`, `controller.py`, and `db.py`.

This is `ar init`, `ar run`, `ar status`, `ar inspect`, and `ar export`, the task loop, resume, and the budget cap. The controller never calls a model.

### 2 · Contracts

Owns `models.py`, `broker.py`, and `snapshots.py`.

This is snapshots, `files.read`, read receipts, immutable artifacts, and the gates that reject an unread submit, a bad quote, a stale revision, and a path escape. This is the part that makes the system different from one long prompt.

### 3 · Agents

Owns `packet.py`, `adapter.py`, and the four roles: planner, evidence reader, writer, reviewer.

`agents.run(role, packet)` is the only model entry. The packet holds the objective, that task's required reads, and accepted dependency artifacts. It is not a shared chat transcript.

### 4 · Quality

Owns `fixtures/baselines/`, claims, completion, export, and the demo script.

The fixture is the product test:

- Paper A supports a baseline.
- Paper B contradicts an exclusion.
- Paper C uses the same words and is a miss.

The answer key is written down in wave 0.

## Call graph

Spine calls Contracts for every read and write, Agents for the model, and Quality to accept claims and export.

- Agents do not open the database.
- Quality does not invent a citation that is not a claim id.
- Contracts is the only writer of blobs and receipts.

| Caller | Function | Owner |
| --- | --- | --- |
| Spine | `broker.read` / `broker.create_artifact` / `broker.revise_artifact` | Contracts |
| Spine | `agents.run(role, packet) -> AgentResult` | Agents |
| Spine | `evidence.accept_claims` / `review.gate` / `export_run` | Quality |
| Agents | Reads only what the packet already contains | Contracts preloads via Spine |
| Quality | Quote-at-locator check | Contracts helper, Quality policy |

## Waves

Work is sequential across waves and parallel inside a wave. Merge to main at each exit. Do not open wave 3 until the fake-agent fixture run and the contract tests pass.

### Wave 0 — Freeze (90 minutes, all four together)

Freeze these records:

- `Requirement`
- `TaskContract`
- `ReadReceipt`
- `ArtifactVersion`
- `Claim`
- `AgentResult`
- `ReviewResult`

Commit the fixture papers and the `run(role, packet) -> AgentResult` signature. `ar init` creates `.research/state.sqlite` and the spec directories.

After this, Contracts owns `models.py`. Add fields in a short sync. Leave names alone.

**Exit:** models, CLI stub, fixture papers, and the `FakeAgent` signature are on main. No research behavior yet.

### Wave 1 — It runs (about 3 hours)

`ar run fixtures/baselines --fake` writes `report.md` with two citations and the contradiction from paper B.

The loop is fixed: planner, one reader per source, writer, reviewer. The controller drops any extra tasks the planner proposes.

**Exit:** one pytest passes: submit before the brief is read, and the run is rejected.

| Track | Wave 1 exit |
| --- | --- |
| Spine | `ar init` / `run` / `status` drive the fake loop in order |
| Contracts | Unread submit is rejected; artifacts are immutable versions |
| Agents | `FakeAgent` returns the fixture answer key as typed results |
| Quality | `report.md` cites two passages and names the contradiction |

### Wave 2 — It is good (about 3 hours). This is the demo.

Live model on the same fixture, with one schema-repair retry. The reviewer can send the writer back once. `ar inspect claim` shows the statement, source, lines, quote, receipt, and originating task.

Kill the process after an accepted task and run again. That task is skipped and the artifact is not duplicated. A budget cap stops dispatch, keeps the draft, and reports `budget_exhausted`.

The README is a five-minute script: brief, contradiction, report, inspect, the rejected quote, resume.

| Track | Wave 2 exit |
| --- | --- |
| Spine | Interrupt and rerun keeps accepted tasks; budget stop keeps the draft |
| Contracts | Bad quote and stale revision fail closed; path escape is blocked |
| Agents | Live model on the fixture; reviewer can force one rewrite |
| Quality | `ar inspect claim` shows span, quote, and task; demo script works |

### Wave 3 — Make it better

Start only after the wave 2 path is stable. Same four people, in this order:

1. The next acceptance tests: non-transferable receipts, input-change staleness, cursor bound to one revision.
2. One seeded CSV computation recorded as an `ExperimentRun` and cited from the report.
3. `permissions.yaml` on the broker.
4. A thin MCP server over the same functions: `project.status`, `files.read`, `tasks.submit`.

Label any imported host log `host_reported`. Say in the run record that the experiment is a subprocess with a working-directory jail.

| Track | Wave 3 deepens |
| --- | --- |
| Spine | Leases, fencing, idempotency, staleness, pause |
| Contracts | Cursors, permissions, revocation, packing receipts |
| Agents | Scout, theorist, analyst; skill files; repair loop |
| Quality | Seeded experiment, provenance labels, MCP status |

Leave these until after wave 3 is already useful:

- An OS sandbox
- PDF and OCR
- Web acquire
- Distributed workers
- A dashboard
- Automatic model routing

## What is in the MVP

- SQLite plus sha256 blobs.
- Pydantic models as the schema source, with generated JSON Schema.
- Task states: `pending`, `ready`, `running`, `accepted`, `needs_revision`, `failed`.
- Concurrency is one.
- Budgets are max tasks and max model calls.
- Roles are planner, evidence reader, writer, and reviewer.
- Citations are claim ids.
- Completion means every requirement is mapped, the deliverable is non-empty, and blocking review findings are fixed once or listed as limitations.

## Acceptance tests, in demo order

From `PROJECTSPEC.md` section 14, in the order to implement them.

| When | Scenario | Required result |
| --- | --- | --- |
| Wave 1 | Submit before the brief is read | Reject with the missing read |
| Wave 2 | Quote is not in the cited line range | Reject the evidence link |
| Wave 2 | Two writes of the same artifact revision | One commit; stale writer gets a conflict; no data loss |
| Wave 2 | Process stopped after an accepted task | Resume skips that task and does not duplicate the artifact |
| Wave 2 | No experiment in the brief | Run can complete |
| Wave 3 | Global budget hit with tasks still ready | Stop dispatch, keep drafts, outcome `budget_exhausted` |
| Wave 3 | Host log imported as if the runtime ran it | Label `host_reported` |

Later rows from the spec, after the table above: cursor reuse across revisions, non-transferable receipts, lease expiry, and experiment exit 0 with missing outputs.

## Integration rules

- One database, one blob directory, one models module.
- Contracts owns `models.py` after the freeze. Other tracks add fields by asking. They do not rename.
- The controller clamps the planner: at most one reader task per source file, one writer, one reviewer, and one revision pass. Extra proposed tasks are stored and ignored until wave 3.
- Merge to main at every wave exit.
- `FakeAgent` stays green so a live provider outage does not block the demo.

## File ownership

```text
src/ar/
  cli.py            # Spine
  controller.py     # Spine
  db.py             # Spine
  models.py         # Contracts
  broker.py         # Contracts
  snapshots.py      # Contracts
  packet.py         # Agents
  adapter.py        # Agents
  agents/
    planner.py      # Agents
    reader.py       # Agents
    writer.py       # Agents
    reviewer.py     # Agents
    fake.py         # Agents
  evidence.py       # Quality
  export.py         # Quality
fixtures/baselines/ # Quality
tests/              # the track that owns the behavior
```

## Per-track detail

### Spine

| | |
| --- | --- |
| Wave 0 | `ar init` creates `.research/state.sqlite` and the spec directories |
| Wave 1 | Loop runs planner, then readers, then writer, then reviewer; status shows state |
| Wave 2 | New attempt on resume; accepted tasks skipped; `max_model_calls` stops as `budget_exhausted` |
| Wave 3 | Leases with fencing tokens, idempotency keys, mark stale when an input hash changes |

Done when a second `ar run` after a crash does not create a second copy of an accepted artifact.

### Contracts

| | |
| --- | --- |
| Wave 0 | Pydantic records listed in wave 0 |
| Wave 1 | Snapshot inputs by sha256; `files.read` by line range; submit blocked when required reads are incomplete |
| Wave 2 | Quote must sit in the cited lines; revise conflicts; reject traversal and writes under `input/` |
| Wave 3 | Cursors bound to one revision, receipts scoped to the attempt, `permissions.yaml` intersection |

Done when the wave 1–2 pytest cases pass against the broker with no model in the loop.

### Agents

| | |
| --- | --- |
| Wave 0 | `run(role, packet) -> AgentResult` agreed and committed as a Protocol |
| Wave 1 | `FakeAgent` emits the answer key: support in paper A, contradiction in paper B, paper C unused |
| Wave 2 | OpenAI-compatible adapter, one schema repair, reviewer returns blocking or note on the exact artifact version |
| Wave 3 | Local source scout, theorist and analyst roles, skills as method text without extra tools |

Done when swapping `--fake` for a live model uses the same packet and the same result schema.

### Quality

| | |
| --- | --- |
| Wave 0 | Three short markdown papers plus a brief whose answer key is written down |
| Wave 1 | Export `report.md`, `run-summary.md`, and `provenance.json` from committed claims only |
| Wave 2 | `inspect claim` prints statement, source, lines, quote, receipt, and originating task |
| Wave 3 | Tiny seeded CSV computation recorded as `ExperimentRun` and cited by the analyst section |

Done when a new teammate clones, installs, runs `ar run fixtures/baselines --fake`, and the citations match the answer key.

## MVP bar

A new teammate clones, installs, and runs `ar run fixtures/baselines --fake`. Citations match the answer key. These checks pass without a model in the loop:

- Submit with the brief unread is rejected.
- A quote that is not in the cited lines is rejected.
- Two writes of the same artifact revision keep one version and return a conflict to the stale writer.
- Resume does not duplicate an accepted artifact.
- A brief with no experiment can still complete.

The planner stays clamped the whole time: at most one reader per file, one writer, one reviewer, one revision. That clamp keeps the first demo bounded while the contracts stay real.
