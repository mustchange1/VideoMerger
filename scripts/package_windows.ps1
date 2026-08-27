# UTF-8-Prüfzeile: ä ö ü Ä Ö Ü ß
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Destination = Join-Path (Split-Path -Parent $Root) "VideoMerger_Final_1.3.0.zip"
$Staging = Join-Path $env:TEMP ("VideoMerger-Package-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $Staging | Out-Null
    robocopy $Root $Staging /E /XD .venv __pycache__ .pytest_cache alignment_models cache dev .git .ruff_cache /XF *.pyc ffmpeg.exe ffprobe.exe settings.json project_order.json generated_outputs.json diagnostic_filtergraph_*.txt *.subtitle_*.png *_burn.ass *.subtitle_timeline.json | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy fehlgeschlagen: $LASTEXITCODE" }
    foreach ($RuntimeName in @("logs", "output", "temp")) {
        $RuntimePath = Join-Path $Staging $RuntimeName
        Remove-Item -LiteralPath $RuntimePath -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null
        New-Item -ItemType File -Path (Join-Path $RuntimePath ".gitkeep") -Force | Out-Null
    }
    $Bin = Join-Path $Staging "tools\ffmpeg\bin"
    New-Item -ItemType Directory -Path $Bin -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $Bin ".gitkeep") -Force | Out-Null
    Compress-Archive -Path (Join-Path $Staging "*") -DestinationPath $Destination -Force
    $Archive = Get-Item -LiteralPath $Destination
    $ArchiveHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "Erstellt: $Destination" -ForegroundColor Green
    Write-Host "Dateiname: $($Archive.Name)" -ForegroundColor Green
    Write-Host "Groesse: $($Archive.Length) Bytes" -ForegroundColor Green
    Write-Host "SHA-256: $ArchiveHash" -ForegroundColor Green
} finally {
    Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
}
