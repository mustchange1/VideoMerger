[CmdletBinding()]
param([switch]$UnitOnly, [switch]$Benchmark)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$FFmpegBin = Join-Path $Root "tools\ffmpeg\bin"
if (-not (Test-Path $Python)) { throw "Zuerst setup_windows.ps1 ausführen." }
$env:VIDEOMERGER_FFMPEG_DIR = $FFmpegBin
$env:HF_HOME = Join-Path $Root 'tools\alignment_models'
$env:VIDEOMERGER_TEST_REAL_ALIGNMENT = '1'
$env:VIDEOMERGER_TEST_ALIGNMENT_MODEL = 'small'
if ($Benchmark) {
    $env:VIDEOMERGER_RUN_2MIN_BENCHMARK = '1'
    $env:VIDEOMERGER_BENCHMARK_RESULT = Join-Path $Root 'test_evidence\1.3.0\benchmark_windows_result.json'
}
& $Python -m pip install -r (Join-Path $Root "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($UnitOnly) {
    & $Python -m pytest -m "not e2e" -v
} else {
    & $Python -m pytest -v
}
exit $LASTEXITCODE
