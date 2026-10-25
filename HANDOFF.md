# Structured specialist handoffs

Every managed research dispatch carries a versioned `Packet` with a `HandoffState`.
The original project goal and constraints persist across specialist assignments,
resume, and writer/reviewer revisions. Full requirement records preserve source
passages and acceptance checks. Upstream entries carry producing task and role,
outcome, partial payloads, limitations, open questions, and follow-up proposals.
Incomplete optional research remains visible downstream instead of disappearing.

`LiveAgent` appends the same JSON state after each role's existing evidence prompt.
Upstream text is untrusted context: it grants no permissions and cannot override
the project contract. Claim drafts are excluded from upstream state; only accepted
claim dependencies are evidence. Existing broker checks still enforce source
revision, receipt ownership, delivered lines and quote provenance. Structural
validation alone does not authenticate external senders or prove scientific truth.

The receiver revalidates packets, including updates made with `model_copy`, before
calling an agent. Unsupported versions, role mismatches, blank identifiers/goals,
duplicate dependencies, inconsistent claim kinds and invalid read ranges or missing
receipts yield `needs_dependency` with `HANDOFF_REJECTED` and zero model calls.
The controller records the failed attempt and stops, including for optional roles.
Studio does not retry or relabel rejected deliveries as successful skipped sources.
Missing essential context is reported through `needs_dependency` and `unresolved`;
ordinary optional research gaps can continue as explicitly incomplete upstream state.

Studio's packet inspector shows the original goal, constraints, upstream outcomes,
open questions, limitations and partial outputs. `packet.dispatched` contains the
same state in the durable journal. Each `handoff.evaluated` event records outcome,
dependency requests, unresolved-item count, rejected status and model-call count.
These are operational proxies; they do not establish semantic correctness.

## Demonstration and comparison

Run the actual planner, evidence-reader, writer and reviewer modules offline:

```text
.venv/Scripts/python -m pytest -q tests/test_handoff.py
.venv/Scripts/python scripts/handoff_benchmark.py --output .research/handoff-check
```

The offline demo uses deterministic fixture agents and checks wiring only. For a
live comparison using configured credentials:

```text
.venv/Scripts/python scripts/handoff_benchmark.py --live --model gpt-5-nano --output .research/handoff-live
```

Both arms receive the same fixture and the same state values. The baseline renders
handoff state as plain text; the structured arm retains JSON. Evidence prompts and
validation remain identical. Each arm gets a separate project and journal;
`comparison.json` reports completion, dependency requests, unresolved items,
rejections, model calls and participating specialties. The live command makes paid
model calls. Use fresh output directories for repeated comparisons. Planner choices
and model variability may differ between arms; a single run does not establish an
improvement, and report correctness still needs review.
