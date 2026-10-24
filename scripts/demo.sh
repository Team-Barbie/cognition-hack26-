#!/usr/bin/env bash
# Five-minute demo on the baselines fixture. No API key needed (--fake).
# Run from anywhere; uses `ar` from the active venv (override with AR="python -m ar.cli").
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AR="${AR:-ar}"
PY="${PYTHON:-python}"
PROJECT="$(mktemp -d)/demo-project"
cd "$ROOT"

step() { printf '\n== %s ==\n' "$1"; }

step "1. The brief"
cat fixtures/baselines/input/brief.md

step "2. Init and run (fake agent)"
$AR init "$PROJECT" --from fixtures/baselines
$AR run "$PROJECT" --fake
$AR status "$PROJECT"

step "3. The report (paper B contradicts the exclusion rule)"
cat "$PROJECT/output/report.md"
echo; echo "-- run-summary.md --"; cat "$PROJECT/output/run-summary.md"

step "4. Why does the report say that?"
$AR inspect claim claim-1 --project "$PROJECT"
$AR inspect claim claim-2 --project "$PROJECT"

step "5. A quote outside the cited lines is rejected"
$PY -m pytest -q tests/test_evidence.py -k rejected
if [ -f tests/test_broker.py ]; then $PY -m pytest -q tests/test_broker.py -k quote; fi

step "6. Kill and resume"
echo "Run again: accepted tasks are skipped and the artifact is not duplicated."
$AR run "$PROJECT" --fake
echo "To see a real interruption: start 'ar run <dir> --fake', press Ctrl-C mid-run, rerun."

step "7. A theory-only brief completes"
$PY -m pytest -q tests/test_completion.py -k no_experiment

echo; echo "Project left at: $PROJECT"
