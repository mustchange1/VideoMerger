# UTF-8-Prüfzeile: ä ö ü Ä Ö Ü ß
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$script:Failed = $false

function Report-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail)
    if ($Ok) {
        Write-Host ('[PASS] ' + $Name + ': ' + $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ('[FAIL] ' + $Name + ': ' + $Detail) -ForegroundColor Red
        $script:Failed = $true
    }
}

function Program-Version {
    param([string]$Name, [string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Report-Check $Name $false ('nicht gefunden: ' + $Path)
        return
    }
    try {
        $Output = & $Path -version 2>&1
        $Ok = $LASTEXITCODE -eq 0
        $FirstLine = if ($Output) { [string](@($Output)[0]) } else { 'keine Ausgabe' }
        Report-Check $Name $Ok ($Path + ' | ' + $FirstLine)
    }
    catch {
        Report-Check $Name $false ($Path + ' | ' + $_.Exception.Message)
    }
}

Write-Host 'VideoMerger Windows Diagnostics 1.2.4' -ForegroundColor Cyan
Write-Host ('Project Root: ' + $ProjectRoot)
Write-Host ''

$Marker = Join-Path $ProjectRoot 'PROJECT_ROOT.txt'
$SetupScript = Join-Path $ProjectRoot 'setup_windows.ps1'
$RunScript = Join-Path $ProjectRoot 'run_windows.ps1'
$MainScript = Join-Path $ProjectRoot 'app\main.py'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$FFmpegBin = Join-Path $ProjectRoot 'tools\ffmpeg\bin'
$FFmpeg = Join-Path $FFmpegBin 'ffmpeg.exe'
$FFprobe = Join-Path $FFmpegBin 'ffprobe.exe'
$AlignmentCache = Join-Path $ProjectRoot 'tools\alignment_models'

Report-Check 'Project Root Marker' (Test-Path -LiteralPath $Marker -PathType Leaf) $Marker
Report-Check 'setup_windows.ps1' (Test-Path -LiteralPath $SetupScript -PathType Leaf) $SetupScript
Report-Check 'run_windows.ps1' (Test-Path -LiteralPath $RunScript -PathType Leaf) $RunScript
Report-Check 'GUI entry point' (Test-Path -LiteralPath $MainScript -PathType Leaf) $MainScript
Report-Check 'Virtual Environment' (Test-Path -LiteralPath $Python -PathType Leaf) $Python

if (Test-Path -LiteralPath $Python -PathType Leaf) {
    try {
        $PythonVersion = & $Python --version 2>&1
        Report-Check 'Python Version' ($LASTEXITCODE -eq 0) ([string]$PythonVersion)
        $PySideOutput = & $Python -c "import PySide6; print('PySide6 OK:', PySide6.__version__)" 2>&1
        Report-Check 'PySide6' ($LASTEXITCODE -eq 0) ([string]$PySideOutput)
        $env:HF_HOME = $AlignmentCache
        $AlignmentOutput = & $Python -c "import faster_whisper; print('faster-whisper OK:', faster_whisper.__version__)" 2>&1
        Report-Check 'Local Word Alignment' ($LASTEXITCODE -eq 0) (([string]$AlignmentOutput) + ' | cache: ' + $AlignmentCache)
    }
    catch {
        Report-Check 'Python/PySide6' $false $_.Exception.Message
    }
}

Program-Version 'FFmpeg' $FFmpeg
Program-Version 'FFprobe' $FFprobe

if ((Test-Path -LiteralPath $FFmpeg -PathType Leaf) -and (Test-Path -LiteralPath $FFprobe -PathType Leaf)) {
    try {
        $FilterOutput = & $FFmpeg -hide_banner -loglevel error -f lavfi -i 'color=c=black:s=16x16:r=1:d=0.05' -filter_complex '[0:v]null[vout]' -map '[vout]' -frames:v 1 -f null - 2>&1
        Report-Check 'FFmpeg direct -filter_complex' ($LASTEXITCODE -eq 0) 'Mini-Render erfolgreich'
    }
    catch {
        Report-Check 'FFmpeg direct -filter_complex' $false $_.Exception.Message
    }
}

if (Test-Path -LiteralPath $Python -PathType Leaf) {
    try {
        $env:VIDEOMERGER_FFMPEG_DIR = $FFmpegBin
        $PythonPaths = & $Python -c "from app.video_merger.paths import locate_ffmpeg; f,p=locate_ffmpeg(); print('Python FFmpeg:', f); print('Python FFprobe:', p)" 2>&1
        Report-Check 'Python local FFmpeg resolution' ($LASTEXITCODE -eq 0) (($PythonPaths | Out-String).Trim())
    }
    catch {
        Report-Check 'Python local FFmpeg resolution' $false $_.Exception.Message
    }
}

foreach ($FolderName in @('input', 'output', 'temp', 'logs', 'config')) {
    $Folder = Join-Path $ProjectRoot $FolderName
    try {
        New-Item -ItemType Directory -Path $Folder -Force | Out-Null
        $TestFile = Join-Path $Folder ('.diagnostic-' + [guid]::NewGuid().ToString('N') + '.tmp')
        [IO.File]::WriteAllText($TestFile, 'write test')
        Remove-Item -LiteralPath $TestFile -Force
        Report-Check ('Write Access ' + $FolderName) $true $Folder
    }
    catch {
        Report-Check ('Write Access ' + $FolderName) $false $_.Exception.Message
    }
}

Write-Host ''
if ($script:Failed) {
    Write-Host 'DIAGNOSTICS FAILED' -ForegroundColor Red
    exit 1
}
Write-Host 'ALL DIAGNOSTICS PASSED' -ForegroundColor Green
exit 0
