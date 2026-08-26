# UTF-8-Prüfzeile: ä ö ü Ä Ö Ü ß
[CmdletBinding()]
param([switch]$Console)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Dieses Startskript ist ausschließlich für Windows 10 und Windows 11 vorgesehen.'
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$RootMarker = Join-Path $Root 'PROJECT_ROOT.txt'
if (-not (Test-Path -LiteralPath $RootMarker -PathType Leaf)) {
    throw ('Ungültiger Projektordner. PROJECT_ROOT.txt fehlt neben run_windows.ps1: ' + $Root)
}
Write-Host ('VideoMerger Project Root: ' + $Root)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
$FFmpegBin = Join-Path $Root 'tools\ffmpeg\bin'
$FFmpegExe = Join-Path $FFmpegBin 'ffmpeg.exe'
$FFprobeExe = Join-Path $FFmpegBin 'ffprobe.exe'
$MainScript = Join-Path $Root 'app\main.py'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'VideoMerger ist noch nicht eingerichtet. Führen Sie zuerst setup_windows.ps1 aus.'
}
if (-not (Test-Path -LiteralPath $PythonW -PathType Leaf)) {
    throw 'pythonw.exe fehlt in der virtuellen Umgebung. Führen Sie setup_windows.ps1 erneut aus.'
}
if (-not ((Test-Path -LiteralPath $FFmpegExe -PathType Leaf) -and (Test-Path -LiteralPath $FFprobeExe -PathType Leaf))) {
    throw 'Lokales FFmpeg oder FFprobe fehlt. Führen Sie setup_windows.ps1 erneut aus.'
}
if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
    throw ('GUI-Startdatei fehlt: ' + $MainScript)
}

$env:VIDEOMERGER_FFMPEG_DIR = $FFmpegBin
$env:HF_HOME = Join-Path $Root 'tools\alignment_models'
$env:PYTHONUTF8 = '1'

if ($Console) {
    & $Python $MainScript
    exit $LASTEXITCODE
}

$QuotedMainScript = '"{0}"' -f $MainScript
$GuiProcess = Start-Process -FilePath $PythonW -ArgumentList $QuotedMainScript -WorkingDirectory $Root -PassThru
Start-Sleep -Milliseconds 1200
if ($GuiProcess.HasExited -and ($GuiProcess.ExitCode -ne 0)) {
    throw ('Die GUI wurde unerwartet beendet. Exit-Code: ' + $GuiProcess.ExitCode + '. Starten Sie .\run_windows.ps1 -Console für Details.')
}
Write-Host 'VideoMerger wurde gestartet.' -ForegroundColor Green
exit 0
