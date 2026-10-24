# ar: autoresearch with explicit contracts

Research, literature review, evidence extraction, planning and review use their own agents and model
adapters. `ar run`, `ar resume`, `ar literature` and Studio run that research workflow.

Codex is used only for an explicit coding assignment: implementing or debugging code, builds, tests,
and simulation programs. The planner/orchestrator may call the coding worker when code is needed;
it retains responsibility for the research question, literature and synthesis. Coding jobs do not
launch literature reviews or replace the research controller.

Use `ar code "Implement and test the baseline" --project my-project --input src --input tests`, or call
the Python/MCP worker below. Install and authenticate Codex (`codex login`) only for coding tasks.
`AR_CODEX_BIN` selects its executable. Native coding jobs retain their Codex tools, optional coding
delegates, resumable sessions and captured build/test outputs.

## Coding worker (Codex, explicit tasks only)

Codex is an optional coding specialist exposed through Python and a standalone MCP server. The caller supplies a coding assignment;
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
  compares it to the answer key. It defaults to OpenAI `gpt-5-nano`; another provider/model can be selected
  explicitly. It calls a real model and is not part of pytest.

Both paths share the same packet and the same `AgentResult` schema.

Studio's **Load demo**, **Watch showcase**, and **Low-cost workflow test** use `gpt-5-nano` when an
OpenAI API key is configured. Set `STUDIO_TEST_MODEL` to override only that test model. Normal research
retains its configured provider/model; explicit `--fake` and offline demo mode still make no model calls.
The active test model appears in the page's mode label. API clients can pass `test_run: true` to
`POST /api/run`, or `false` to return to normal research.

To verify the complete research/synthesis handoff with real low-cost model calls:

```text
python scripts/live_workflow_smoke.py --env-file .env
```

This uses the supplied demo papers and checks that the planner, readers, research agent, synthesis,
writer and reviewer actually produce results. It retains the run journal under `.research/live-workflows/`
and exits unsuccessfully if a specialist requests missing dependencies or the report cannot complete.

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

`ar literature` uses the research model adapter (Anthropic by default, or `--provider openai`).
It never starts Codex or invokes the coding worker.

`ar literature` uses each configured review's budget. `ar run` shares its model-call allowance across
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

In Studio, **Literature reviews** adds/removes named review instances. An API-provider
run without uploaded papers automatically creates a review of the brief. Search and acquisition progress appears separately for each
instance on the stage. Demo mode continues to use the built-in fixture and does not perform web research.
These are bounded automated reviews, not a claim of exhaustive or formally systematic coverage.

Literature specialists run through the research controller or their own service. Their retrieval,
claims, review results and budgets are independent of coding jobs. Long reviews are best run locally
or in a persistent worker because hosted request limits can end an in-flight job.

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
specialists it selected. A run may include local source screening, evidence readers, a research assessment,
a theorist, a configured experiment, one independent repeat, an analyst, synthesis, and adversarial review
before the mandatory writer and reviewer. Synthesis receives accepted evidence and upstream research;
adversarial review challenges that synthesis, and both outputs reach the writer and reviewer.
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
