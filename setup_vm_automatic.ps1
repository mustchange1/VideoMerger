# UTF-8-Prüfzeile: ä ö ü Ä Ö Ü ß
# VM Automatic – Setup (baut auf dem VideoMerger-Setup auf: gleiche .venv,
# zusätzlich die watchdog-Abhängigkeit des Dateiwächters)
[CmdletBinding()]
param(
    [switch]$NoShortcut,
    [switch]$SkipVenv
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

trap {
    Write-Host ''
    Write-Host 'SETUP FEHLGESCHLAGEN' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

if ($env:OS -ne 'Windows_NT') {
    throw 'Dieses Setup ist ausschließlich für Windows 10 und Windows 11 vorgesehen.'
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$RootMarker = Join-Path $Root 'PROJECT_ROOT.txt'
if (-not (Test-Path -LiteralPath $RootMarker -PathType Leaf)) {
    throw ('Ungültiger Projektordner. PROJECT_ROOT.txt fehlt neben setup_vm_automatic.ps1: ' + $Root)
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ''
    Write-Host ('==> ' + $Text) -ForegroundColor Cyan
}

Write-Step 'Projektordner'
Write-Host $Root

if (-not $SkipVenv) {
    Write-Step 'Python-Umgebung (gemeinsam mit VideoMerger)'
    $Python = Join-Path $Root '.venv\Scripts\python.exe'
    $Pip = Join-Path $Root '.venv\Scripts\pip.exe'
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host 'Virtuelle Umgebung fehlt – VideoMerger-Setup wird zuerst ausgeführt (setup_windows.ps1) …'
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'setup_windows.ps1') -SkipSelfTest
        if ($LASTEXITCODE -ne 0) { throw 'VideoMerger-Setup fehlgeschlagen.' }
    }
    Write-Host 'VM Automatic-Abhängigkeiten installieren (u. a. watchdog) …'
    & $Pip install --quiet -r (Join-Path $Root 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip-Installation fehlgeschlagen.' }
    & $Python -c "import watchdog" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'watchdog ist nicht importierbar.' }
    Write-Host 'watchdog: OK (ereignisgesteuerter Dateiwächter)' -ForegroundColor Green
}

Write-Step 'FFmpeg-Prüfung (VideoMerger-Installation)'
$FFmpegExe = Join-Path $Root 'tools\ffmpeg\bin\ffmpeg.exe'
$FFprobeExe = Join-Path $Root 'tools\ffmpeg\bin\ffprobe.exe'
if (-not ((Test-Path -LiteralPath $FFmpegExe) -and (Test-Path -LiteralPath $FFprobeExe))) {
    throw 'Lokales FFmpeg/FFprobe fehlt. Bitte setup_windows.ps1 ausführen.'
}
Write-Host 'FFmpeg/FFprobe: OK' -ForegroundColor Green

Write-Step 'VM Automatic Import-Selbsttest'
& (Join-Path $Root '.venv\Scripts\python.exe') -c "import app.vm_automatic.controller, app.vm_automatic.runner, app.vm_automatic.gui; print('VM Automatic import OK')"
if ($LASTEXITCODE -ne 0) { throw 'VM Automatic Import-Selbsttest fehlgeschlagen.' }

if (-not $NoShortcut) {
    Write-Step 'Desktop-Shortcut'
    $WshShell = New-Object -ComObject WScript.Shell
    $Desktop = [Environment]::GetFolderPath('Desktop')
    $Shortcut = $WshShell.CreateShortcut((Join-Path $Desktop 'VM Automatic starten.lnk'))
    $Shortcut.TargetPath = Join-Path $Root 'VM Automatic starten.cmd'
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.Save()
    Write-Host ('Shortcut erstellt: ' + (Join-Path $Desktop 'VM Automatic starten.lnk'))
}

Write-Host ''
Write-Host 'SETUP ERFOLGREICH' -ForegroundColor Green
Write-Host 'VM Automatic starten:  .\VM Automatic starten.cmd'
exit 0
