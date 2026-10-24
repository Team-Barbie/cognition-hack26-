# ar: autoresearch with explicit contracts

A research run you can inspect. Give it a brief and a few papers; it plans, reads each source through a
broker, extracts claims whose quotes are checked against the exact lines the agent was handed, drafts a
report that cites only claim ids, has it reviewed, and exports the report with a provenance manifest.
`PROJECTSPEC.md` is the design, `PLAN.md` the build plan, `INTERFACES.md` the binding signatures.

Enforcement is application-level brokerage through the runtime broker; there is no OS sandbox.

## Install

```text
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m pytest -q                   # no model, no API key
```

## Agents: fake vs live

- `FakeAgent` (`--fake`) replays `fixtures/baselines/answer_key.json` as typed results. No API key, no
  network, deterministic. The demo and the whole test suite use it.
- Live uses the Claude API through the `anthropic` SDK (`AnthropicClient` + `LiveAgent`). Set
  `ANTHROPIC_API_KEY` (or sign in once with `ant auth login`). Options: `--model <id>`,
  `--effort low|medium|high|xhigh|max`, `--max-model-calls N`.
- `python scripts/live_smoke.py [--model M] [--effort E]` runs the evidence reader live on the fixture and
  compares it to the answer key. It calls a real model and is not part of pytest.

Both paths share the same packet and the same `AgentResult` schema.

## The five-minute demo

Run it in one go with `scripts/demo.ps1` (Windows) or `scripts/demo.sh`. Or step by step, from the repo root
with the venv active. Status: the CLI commands below follow INTERFACES.md; end-to-end output has **not yet
been verified** against the Spine track's CLI. Items marked (verified) are covered by pytest today.

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
