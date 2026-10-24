# Five-minute demo on the baselines fixture. No API key needed (--fake).
# Run from anywhere with the venv active, or point $env:AR / $env:PYTHON elsewhere.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Ar = if ($env:AR) { $env:AR } else { "ar" }
$Py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Project = Join-Path (Join-Path ([System.IO.Path]::GetTempPath()) ("ar-demo-" + [guid]::NewGuid().ToString("N"))) "demo-project"
Set-Location $Root

function Step($title) { Write-Host "`n== $title ==" }
function Run { & $args[0] $args[1..($args.Length - 1)]; if ($LASTEXITCODE -ne 0) { throw "failed: $args" } }

Step "1. The brief"
Get-Content fixtures/baselines/input/brief.md

Step "2. Init and run (fake agent)"
Run $Ar init $Project --from fixtures/baselines
Run $Ar run $Project --fake
Run $Ar status $Project

Step "3. The report (paper B contradicts the exclusion rule)"
Get-Content (Join-Path $Project "output/report.md")
Write-Host "`n-- run-summary.md --"
Get-Content (Join-Path $Project "output/run-summary.md")

Step "4. Why does the report say that?"
Run $Ar inspect claim claim-1 --project $Project
Run $Ar inspect claim claim-2 --project $Project

Step "5. A quote outside the cited lines is rejected"
Run $Py -m pytest -q tests/test_evidence.py -k rejected
if (Test-Path tests/test_broker.py) { Run $Py -m pytest -q tests/test_broker.py -k quote }

Step "6. Kill and resume"
Write-Host "Run again: accepted tasks are skipped and the artifact is not duplicated."
Run $Ar run $Project --fake
Write-Host "To see a real interruption: start 'ar run <dir> --fake', press Ctrl-C mid-run, rerun."

Step "7. A theory-only brief completes"
Run $Py -m pytest -q tests/test_completion.py -k no_experiment

Write-Host "`nProject left at: $Project"
