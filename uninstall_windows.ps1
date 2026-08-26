[CmdletBinding()]
param([switch]$RemoveOutputs)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "VideoMerger.lnk"
Remove-Item $Shortcut -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root ".venv") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "tools\ffmpeg") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "temp") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "logs") -Recurse -Force -ErrorAction SilentlyContinue
if ($RemoveOutputs) {
    Remove-Item (Join-Path $Root "output") -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "VideoMerger-Laufzeitumgebung wurde entfernt. Quellcode und Eingaben bleiben erhalten." -ForegroundColor Green
