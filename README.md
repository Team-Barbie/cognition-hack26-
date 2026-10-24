# ar: research and engineering with Codex

The default live runtime is one native Codex agent. Give it an objective and optional papers/data/code;
it chooses how to research, implement, build, simulate and test. It retains context in one resumable
Codex thread and can delegate focused work to native subagents when useful. There is no fixed sequence
of specialist agents in this path. The existing broker-based pipeline remains available for the offline
demo and explicit legacy providers.

Codex runs through its installed App Server, with workspace-write permissions and no automatic escalation.
The application captures native tool events, command exit statuses, output artifacts and SHA-256 hashes.
Codex citations and check summaries remain model-reported; they do not acquire the legacy pipeline's
broker-verified quote/read-receipt guarantees. Codex's installed platform sandbox must be configured.

## Native Codex workflow (default)

Install the Codex CLI, authenticate with `codex login`, then install this Python project as below.
No additional agent SDK is required: the backend speaks the native App Server JSON-RPC protocol over stdio.
The transport was checked against Codex CLI 0.144.5. Set `AR_CODEX_BIN` if Codex is not on PATH.

```text
ar init my-project
# Edit my-project/input/brief.md; put papers, datasets and source code under input/.
ar run my-project --max-agents 3 --timeout 1800
ar resume my-project
ar status my-project
ar pause my-project
ar studio
```

Use `--max-agents 0` to keep all work in the lead agent. Use `--model MODEL` to choose an available
Codex model explicitly; otherwise the runtime selects the default advertised by Codex's model catalog.
An explicit model is never silently substituted. `--effort` sets reasoning effort. The wall-clock
`--timeout` bounds each turn; legacy `--max-model-calls` and `--max-tasks` apply only to the old pipeline.
Run `ar studio --fake` for the offline fixture demo. Studio also defaults to Codex and its backend supports
follow-up steering through `/api/chat` during a run and cancellation through `/api/stop`.

Outputs are `output/report.md`, `run-summary.md`, `codex-result.json`, `provenance.json`, and captured
files in `output/deliverables/`. The native thread id, staged-input hashes and event journal live under
`.research/`; the writable agent workspace is `.research/codex-workspace/`. Resuming reuses the thread
and workspace while refreshing the current inputs. One project permits one active run. If a host crash
leaves `.research/codex.lock`, verify the old worker has stopped before removing that lock.

For long runs use the local persistent server or a dedicated worker host with Codex installed and
authenticated. A serverless deployment does not automatically provide a Codex executable, credentials,
or a working platform sandbox. Sandbox startup failures are reported as failed/blocked work, never as
successful builds or tests. Keep the native sandbox enabled; configure/fix it or use a supported worker.

## Callable by a planner or another agent

The worker is also a Python API and a standalone MCP server. The caller supplies a free-form objective;
it does not need to follow the old role graph or required-reading contracts. Native workspace permissions
and the configured timeout still apply.

```python
from pathlib import Path
from ar.coding_agent import CodingAgent

worker = CodingAgent(Path("my-project"))
job = worker.start("Implement the baseline and test edge cases", input_paths=["src", "tests"])
progress = worker.status(job["job_id"])
worker.steer(job["job_id"], "Also cover empty input")  # while running
result = worker.wait(job["job_id"], timeout=60)
# Resume a finished/interrupted session with a new instruction:
# worker.start("Address the remaining failures", job_id=job["job_id"], input_paths=["src", "tests"])
# worker.cancel(job["job_id"])  # interrupt an active job
# worker.close()                 # cancel remaining active jobs on shutdown
```

To expose the same worker to an agent harness, register a stdio MCP server that launches:

```text
python -m ar.coding_mcp /absolute/path/to/project
```

Use the Python executable where this package is installed. The server exposes `coding.start`,
`coding.status`, `coding.steer`, and `coding.cancel`. Start returns immediately with a job id; poll status
using its returned `cursor` as `after` to receive more captured events and the final result. A caller can
pass `job_id` to start to resume the same Codex session. Input paths are relative to the bound project;
directory requests skip Git/runtime/dependency caches and environment files. Completed jobs and their
artifacts persist under `.research/coding-jobs/`. After an unexpected server restart an in-flight job
is reported as `execution_unknown`, rather than claimed successful or automatically rerun. Keep the
MCP connection open while jobs run; shutdown requests cancellation of active jobs.

## Legacy research pipeline

`PROJECTSPEC.md`, `PLAN.md`, and `INTERFACES.md` describe the earlier contracts-based architecture.
Its role-specific behavior and quote/read-receipt guarantees below apply to `--fake` or explicit
`--provider anthropic` / `--provider openai`, rather than the default Codex runtime.

## Install

```text
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m pytest -q                   # no model, no API key
```

## Agents: fake vs live

- `FakeAgent` (`--fake`) replays the packaged `src/ar/fixtures/baselines/answer_key.json` as typed results. No API key, no
  network, deterministic. The demo and the whole test suite use it.
- Live uses the Claude API through the `anthropic` SDK (`AnthropicClient` + `LiveAgent`). Set
  `ANTHROPIC_API_KEY` (or sign in once with `ant auth login`). Options: `--model <id>`,
  `--effort low|medium|high|xhigh|max`, `--max-model-calls N`.
- `python scripts/live_smoke.py [--model M] [--effort E]` runs the evidence reader live on the fixture and
  compares it to the answer key. It calls a real model and is not part of pytest.

Both paths share the same packet and the same `AgentResult` schema.

## Literature review agents

Literature reviews perform real discovery and acquisition before extracting evidence. They search
Crossref and Europe PMC, deduplicate papers by DOI (or normalized title/year), keep screening decisions,
fetch open-access XML/PDF/HTML, and read bounded line windows through the evidence broker. General web
search is available through the `brave` backend when `BRAVE_SEARCH_API_KEY` is set. Crossref and Europe PMC
need no API key; choose databases appropriate to your subject. Set `WEB_SEARCH_CONTACT` for Crossref's
polite-pool contact parameter.

To create one review without preparing a project first:

```text
ar literature my-project --id methods --question "Which forecasting baselines work for weekly demand?" --backend crossref
```

For several independent agents, copy `fixtures/literature.example.yaml` to
`my-project/input/literature.yaml` and edit the questions and limits. Every review needs a unique `id`.

```text
ar literature my-project
ar literature my-project --review effectiveness --review limitations
ar run my-project
ar run my-project --provider anthropic --max-model-calls 80
```

`ar literature` defaults to the existing Codex login. Codex supplies stateless JSON decisions in a
separate read-only workspace with inherited user tool configuration, web search, shell, plugins and
delegation disabled. The literature runtime performs and journals actual retrieval and validates
evidence. One model-call unit is one decision turn, including any provider-internal work/retries.
Use `--provider anthropic` or `--provider openai` for API-based decisions instead.

`ar literature` uses each configured review's budget. `ar run --provider anthropic` (or `openai`) shares its model-call allowance across
configured reviews, reserves six calls for the final report, and passes accepted claims and review
summaries into planning/writing. Partially covered reviews keep the overall run incomplete. The normal
CLI provider/model/base-URL options also apply to `ar literature`.

Each review records its protocol, query variants and contradictory searches, result pages and retrieval
times, exclusions, downloaded-content hashes, extraction method, source access level, read receipts,
accepted/rejected claims, study characteristics, follow-up gaps, and stopping reason. Its report and
portable search ledger are in `output/literature/<id>-<spec-hash>/`. Original response bodies are in the
project's content-addressed blob store. Model-created notes and discovery snippets never become evidence.
Abstract-only access, unavailable papers, unread lines, missing searches, and exhausted budgets are
reported as limitations. A quote match establishes provenance; scientific validity still requires
assessing study design, applicability, uncertainty, and conflicting results.

Repeated runs of an identical specification reuse its journal and result. Interrupted completed
operations are replayed from the journal; an operation with an unknown outcome is not silently charged
again. Use a new review id for a deliberate retry or a fresh search. Changing the specification creates
a distinct version. The runtime leases an active instance to prevent duplicate execution. Independent
instances can run concurrently through the Python API:

```python
from concurrent.futures import ThreadPoolExecutor
from ar.literature import LiteratureReviewAgent, ReviewSpec

reviewer = LiteratureReviewAgent(project_path, configured_agent)
specs = [ReviewSpec(id="methods", question="Which methods work?"),
         ReviewSpec(id="limitations", question="What evidence contradicts the reported gains?")]
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(reviewer.run, specs))
```

In Studio, **Literature reviews** adds/removes named review instances for either runtime. An API-provider
run without uploaded papers automatically creates a review of the brief. Search and acquisition progress appears separately for each
instance on the stage. Demo mode continues to use the built-in fixture and does not perform web research.
These are bounded automated reviews, not a claim of exhaustive or formally systematic coverage.

The default native Codex runner executes configured literature specialists before its lead turn and
stages their reports and search ledgers in `literature-evidence/`. Its lead session, native tools,
delegation, coding-worker API and artifacts stay available. Review results are included in the report
and provenance JSON; a partial/failed review keeps the combined outcome blocked. Broker quotation checks
apply to the specialists' accepted claims; the lead's final interpretations retain native citation assurances.
The same specialist is integrated with `ar studio --provider anthropic` and `--provider openai`.
Long multi-review runs are best run
locally or in a worker, since hosted request limits can end an in-flight job. `max_seconds` is checked
between operations; an in-flight model call follows the provider timeout (120 seconds for a Codex decision).

For a planner or any other agent harness, the dedicated specialist has a non-blocking Python API:

```python
from pathlib import Path
from ar.literature_service import LiteratureService

reviews = LiteratureService(Path("my-project"))
methods = reviews.start({"id": "methods", "question": "Which methods work?"})
safety = reviews.start({"id": "safety", "question": "What are the failure modes?"})
progress = reviews.status(methods["job_id"])
# Poll with after=progress["cursor"] to receive only new events.
result = reviews.wait(methods["job_id"], timeout=60)
```

Expose it as a stdio MCP server with the Python executable where this package is installed:

```text
python -m ar.literature_mcp /absolute/path/to/project
```

The tools are `literature.start`, `literature.status`, and `literature.list`. `start` takes a `spec`
following the same review schema as YAML, returns a job id immediately, and permits multiple independent
jobs (four concurrently by default). `status` returns scoped events, counters and the terminal result.
Keep the connection/host alive while jobs run. Restarted in-flight jobs report `execution_unknown`;
completed results and search journals persist. Add `--provider anthropic` or `--provider openai` and
optional `--model` / `--base-url` arguments to choose an API provider. The separate `coding.*` MCP tools
remain available through `ar.coding_mcp`.

Provider references: [Crossref API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/),
[Europe PMC API](https://europepmc.org/RestfulWebService), and
[Brave Search API](https://api-dashboard.search.brave.com/documentation).

## Studio access

The studio page remains public so it can display the lock screen, while every `/api/*` route follows the
server's access policy. Set `STUDIO_PASSWORD` to protect a deployment; local development stays open when it
is unset, and Vercel uses its built-in password hash unless the environment variable overrides it. The
browser keeps an entered password only for the current tab. This access gate prevents unauthenticated API
use; volumetric DDoS protection still belongs at the hosting or CDN layer.

The live stage is driven by a durable SQLite event journal. The runtime records run, task, attempt, packet,
acceptance, and completion events; the browser consumes them over an authenticated server-sent event stream.
`GET /api/events` remains as a reconnect fallback rather than the primary update loop.

For local live mode, copy `.env.example` to `.env`, fill in `OPENAI_API_KEY` and
`STUDIO_PASSWORD`, then restart Studio without `--fake`. The real `.env` is ignored by Git.
For Vercel, add those same two names under the project's Environment Variables for every target you use
(Production and, if needed, Preview), then redeploy. Never upload `.env` or expose either value through a
client-side variable.

The server rate-limits each client IP to 120 API requests per minute, with tighter caps of 12 new sessions
per minute, 6 agent runs per 10 minutes, and 20 event-stream connections per minute. A rejected request
returns HTTP 429 with `Retry-After`. This in-process layer limits accidental abuse and simple bots; use
Vercel Firewall/WAF rules as the outer layer for distributed traffic and DDoS protection.

## Planner-driven research

The controller commits the planner first, validates its proposed graph, and then dispatches the bounded
specialists it selected. A run may include local source screening, evidence readers, a theorist, a
configured experiment, one independent repeat, and an analyst before the mandatory writer and reviewer.
Unknown sources, unconfigured execution, duplicate specialist roles, and tasks beyond the run budget are
rejected or clamped and recorded in the `planner_proposals` event. The original literature-only sequence
remains the fallback for old planners and simple briefs.

Executable work is never taken from model output. To make an experiment available to the planner, place a
fully declared `ExperimentSpec` in `input/experiment.yaml`, for example:

```yaml
id: weekly-baseline
question: Seasonal-naive MAE on weekly demand
status: pilot
input_files: [input/datasets/data.csv, input/code/baseline.py]
command: [python, baseline.py, --seed, "7"]
working_dir: input/code
seed: 7
metrics: [mae]
success_criteria: [mae computed]
expected_outputs: [results.json]
timeout_seconds: 60
```

The runtime executes the exact configured command in its working-directory jail. A reproduction request
runs the frozen specification again as a distinct recorded run; the analyst receives only captured run
records and hashes. Without this file, experiment and analyst proposals cannot execute.

## The five-minute demo

Run it in one go with `scripts/demo.ps1` (Windows) or `scripts/demo.sh`. Or step by step, from the repo root
with the venv active. The CLI path and generated output are covered end to end; items marked (verified)
also have focused pytest coverage.

1. **The brief.** `fixtures/baselines/input/brief.md` asks whether seasonal naive is an adequate weekly
   baseline and states a constraint: series shorter than 52 weeks are excluded. Paper A supports the
   baseline, paper B contradicts the exclusion, paper C reuses the same words in a clinical trial and is a
   miss (`answer_key.json`).

   ```text
   ar init demo-project --from fixtures/baselines
   ar run demo-project --fake
   ar status demo-project
   ```

2. **The contradiction in paper B.** Paper B line 7: "Excluding short series therefore discards usable
   data and biases results toward mature products." The report must name it. (Fake reviewer logic and
   completion check are verified: `tests/test_reviewer.py`, `tests/test_completion.py`.)

3. **The report.** `demo-project/output/report.md` has each `[claim:id]` rewritten as a footnote
   (`[^claim-1]`) with statement, source id, lines and quote. `run-summary.md` gives the outcome, a
   requirement table, limitations and the assurance note; `provenance.json` is the portable manifest.
   (Export verified: `tests/test_export.py`.)

4. **Why does the report say that?**

   ```text
   ar inspect claim claim-1 --project demo-project
   ```

   Prints the statement, source and revision, lines, quote, read receipt id, and the originating task and
   attempt. (Verified at the function level: `tests/test_evidence.py::test_inspect_claim_has_required_fields`.)

5. **The rejected quote.** A quote that is not at the cited lines is rejected, and a claim with any rejected
   link is rejected whole and never stored.

   ```text
   .venv/Scripts/python -m pytest -q tests/test_evidence.py -k rejected
   .venv/Scripts/python -m pytest -q tests/test_broker.py -k quote
   ```

   `tests/test_broker.py` (Contracts) proves the broker refuses a quote outside the cited lines or a read
   receipt that belongs to another attempt; `tests/test_evidence.py` proves the claim is not stored.

6. **Kill and resume.** Interrupt `ar run` (Ctrl-C) after a task is accepted, then run `ar run demo-project
   --fake` again. Accepted tasks are skipped and the report artifact is not duplicated. Not yet verified
   end to end; the Spine track's resume tests own this.

7. **A theory-only brief completes.** A brief with no experiment requirement is assessed as complete; the
   runtime never manufactures an experiment requirement (verified:
   `tests/test_completion.py::test_no_experiment_brief_completes`). Try it by editing
   `demo-project/input/brief.md` to drop everything but the question and the deliverable, then `ar run`
   (end to end not yet verified).

A budget stop (`--max-model-calls 2`) keeps the draft and exports with outcome `budget_exhausted`; the
summary and report say so and never label the run completed.

## Layout

```text
<project>/input/            brief.md and papers/*.md, human-authored, never written by the runtime
<project>/.research/        state.sqlite and content-addressed blobs
<project>/output/           report.md, run-summary.md, provenance.json
```

## Wave 3: experiments, provenance, MCP

- **Seeded experiment.** `ar.experiment.run_experiment(project, conn, spec)` copies the declared inputs
  (`fixtures/experiment/data.csv`, `baseline_mae.py`) into `.research/workspaces/<run-id>/`, runs the command
  there with a minimal environment and a timeout, stores stdout, stderr and each expected output as blobs, and
  records an `ExperimentRun`. Same seed, same output hashes. This is a subprocess with a working-directory
  jail, **not an OS sandbox**, and the run record says so. Exit 0 with a missing expected output is recorded
  as process success but `result_acceptable=False`; `accept_result(run)` raises. A run that was started and
  never finished is `execution_unknown` (`reconcile`) and is never rerun without `retry_unknown=True`. Cite a
  run in a report with `[run:exp-001]`; it becomes a footnote.
- **Provenance labels.** `ar.imports.register_import(...)` snapshots a host-produced file (an experiment log,
  say) and labels it `host_reported`: the runtime did not run it or enforce its original permissions. Imports
  can never be `runtime_captured`. A claim that cites an import shows the label in the `report.md` footnote,
  `ar inspect claim`, and `provenance.json`. Bytes the project itself generated stay `derived`, and claiming
  them as independent external evidence raises `SELF_CONFIRMATION_REJECTED`.
- **MCP.** `python -m ar.mcp_server <project>` is a stdlib JSON-RPC 2.0 stdio server exposing
  `project.status`, `files.read` and `tasks.submit` over the same broker. `tasks.submit` only checks required
  reads; it runs nothing. Errors use `{code, message, retryable, details}`.
