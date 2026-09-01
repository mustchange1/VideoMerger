# UTF-8-Prüfzeile: ä ö ü Ä Ö Ü ß
[CmdletBinding()]
param(
    [switch]$NoShortcut,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

trap {
    Write-Host ''
    Write-Host 'SETUP FEHLGESCHLAGEN' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Die Installation wurde angehalten. Beheben Sie den genannten Fehler und starten Sie setup_windows.ps1 erneut.' -ForegroundColor Yellow
    exit 1
}

if ($env:OS -ne 'Windows_NT') {
    throw 'Dieses Setup ist ausschließlich für Windows 10 und Windows 11 vorgesehen.'
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$RootMarker = Join-Path $Root 'PROJECT_ROOT.txt'
if (-not (Test-Path -LiteralPath $RootMarker -PathType Leaf)) {
    throw ('Ungültiger Projektordner. PROJECT_ROOT.txt fehlt neben setup_windows.ps1: ' + $Root)
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ''
    Write-Host ('==> ' + $Text) -ForegroundColor Cyan
}

function Test-DirectoryWriteAccess {
    param([Parameter(Mandatory = $true)][string]$Directory)
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $TestFile = Join-Path $Directory ('.write-test-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($TestFile, 'VideoMerger write test', $Utf8NoBom)
        Remove-Item -LiteralPath $TestFile -Force
    }
    catch {
        throw ('Kein Schreibzugriff auf: ' + $Directory + '. ' + $_.Exception.Message)
    }
}

function Get-CompatiblePython {
    $Candidates = New-Object System.Collections.Generic.List[string]
    foreach ($Version in @('3.12', '3.11', '3.13')) {
        try {
            $FromLauncher = & py ('-' + $Version) -c 'import sys; print(sys.executable)' 2>$null
            if (($LASTEXITCODE -eq 0) -and $FromLauncher) {
                $Candidates.Add(([string]$FromLauncher).Trim())
            }
        }
        catch {}
    }
    try {
        $PythonCommand = Get-Command python.exe -ErrorAction Stop
        $Candidates.Add($PythonCommand.Source)
    }
    catch {}
    foreach ($CandidatePath in @(
        (Join-Path $env:LocalAppData 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LocalAppData 'Programs\Python\Python311\python.exe')
    )) {
        $Candidates.Add($CandidatePath)
    }
    foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        try {
            $Compatible = & $Candidate -c 'import sys; print(int((3, 10) <= sys.version_info[:2] < (3, 14)))'
            if (($LASTEXITCODE -eq 0) -and (([string]$Compatible).Trim() -eq '1')) {
                return $Candidate
            }
        }
        catch {}
    }
    return $null
}

function Test-NativeProgram {
    param(
        [Parameter(Mandatory = $true)][string]$Program,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )
    if (-not (Test-Path -LiteralPath $Program -PathType Leaf)) {
        throw ($DisplayName + ' wurde nicht gefunden: ' + $Program)
    }
    $VersionOutput = & $Program -version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($DisplayName + ' konnte nicht ausgeführt werden. Exit-Code: ' + $LASTEXITCODE)
    }
    $FirstLine = @($VersionOutput)[0]
    Write-Host ($DisplayName + ': ' + $FirstLine) -ForegroundColor Green
}

Write-Host 'VideoMerger Windows Setup' -ForegroundColor White
Write-Host ('Projekt: ' + $Root)
Write-Host 'Plattform: Windows erkannt' -ForegroundColor Green

Write-Step 'Projektordner erstellen und Schreibrechte prüfen'
foreach ($FolderName in @('input', 'output', 'temp', 'logs', 'config', 'tools')) {
    $FolderPath = Join-Path $Root $FolderName
    Test-DirectoryWriteAccess -Directory $FolderPath
    Write-Host ('Schreibzugriff OK: ' + $FolderPath)
}

Write-Step 'Python 3.10 bis 3.13 prüfen'
$Python = Get-CompatiblePython
if (-not $Python) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw 'Kein kompatibles Python gefunden und winget ist nicht verfügbar. Installieren Sie Python 3.12 von python.org und starten Sie das Setup erneut.'
    }
    Write-Host 'Python fehlt. Python 3.12 wird mit winget für den aktuellen Benutzer installiert.'
    & winget.exe install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw ('winget konnte Python 3.12 nicht installieren. Exit-Code: ' + $LASTEXITCODE)
    }
    $Python = Get-CompatiblePython
    if (-not $Python) {
        $ExpectedPython = Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'
        if (Test-Path -LiteralPath $ExpectedPython -PathType Leaf) {
            $Python = $ExpectedPython
        }
    }
    if (-not $Python) {
        throw 'Python wurde installiert, aber in dieser Sitzung nicht gefunden. Öffnen Sie PowerShell neu und starten Sie das Setup erneut.'
    }
}
$PythonVersion = & $Python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw 'Python konnte nicht ausgeführt werden.' }
Write-Host ('Python: ' + $PythonVersion + ' (' + $Python + ')') -ForegroundColor Green

Write-Step 'Virtuelle Python-Umgebung und Abhängigkeiten installieren'
$VenvDirectory = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDirectory 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $Python -m venv $VenvDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Die virtuelle Python-Umgebung konnte nicht erstellt werden.' }
}
& $VenvPython -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { throw 'pip oder wheel konnte nicht aktualisiert werden.' }
$Requirements = Join-Path $Root 'requirements.txt'
& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw 'Die Python-Abhängigkeiten konnten nicht installiert werden.' }
& $VenvPython -c "import PySide6; print('PySide6 OK:', PySide6.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'PySide6 konnte nach der Installation nicht importiert werden.' }

Write-Step 'Lokales Wortausrichtungsmodell vorbereiten'
$AlignmentCache = Join-Path $Root 'tools\alignment_models'
New-Item -ItemType Directory -Path $AlignmentCache -Force | Out-Null
$env:HF_HOME = $AlignmentCache
& $VenvPython -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('Local alignment model OK: small')"
if ($LASTEXITCODE -ne 0) { throw 'Das lokale faster-whisper Wortausrichtungsmodell konnte nicht vorbereitet werden.' }

Write-Step 'Lokales FFmpeg und FFprobe einrichten'
$FFmpegBin = Join-Path $Root 'tools\ffmpeg\bin'
$FFmpegExe = Join-Path $FFmpegBin 'ffmpeg.exe'
$FFprobeExe = Join-Path $FFmpegBin 'ffprobe.exe'
$DownloadUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
if (-not ((Test-Path -LiteralPath $FFmpegExe -PathType Leaf) -and (Test-Path -LiteralPath $FFprobeExe -PathType Leaf))) {
    New-Item -ItemType Directory -Path $FFmpegBin -Force | Out-Null
    $DownloadDirectory = Join-Path $env:TEMP ('VideoMerger-FFmpeg-' + [guid]::NewGuid().ToString('N'))
    $ZipPath = Join-Path $DownloadDirectory 'ffmpeg-release-essentials.zip'
    $ExtractPath = Join-Path $DownloadDirectory 'extracted'
    New-Item -ItemType Directory -Path $DownloadDirectory -Force | Out-Null
    try {
        Write-Host ('HTTPS-Download: ' + $DownloadUrl)
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
        $ZipInfo = Get-Item -LiteralPath $ZipPath
        if ($ZipInfo.Length -lt 1000000) {
            throw ('Der FFmpeg-Download ist unerwartet klein: ' + $ZipInfo.Length + ' Bytes.')
        }
        $Stream = [IO.File]::OpenRead($ZipPath)
        try {
            $Byte0 = $Stream.ReadByte()
            $Byte1 = $Stream.ReadByte()
        }
        finally {
            $Stream.Dispose()
        }
        if (($Byte0 -ne 0x50) -or ($Byte1 -ne 0x4B)) {
            throw 'Der FFmpeg-Download besitzt keine gültige ZIP-Signatur.'
        }
        $DownloadHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
        Write-Host ('Download SHA-256: ' + $DownloadHash)
        Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractPath -Force
        $FoundFFmpeg = Get-ChildItem -LiteralPath $ExtractPath -Filter 'ffmpeg.exe' -File -Recurse | Select-Object -First 1
        $FoundFFprobe = Get-ChildItem -LiteralPath $ExtractPath -Filter 'ffprobe.exe' -File -Recurse | Select-Object -First 1
        if ((-not $FoundFFmpeg) -or (-not $FoundFFprobe)) {
            throw 'Das heruntergeladene Archiv enthält ffmpeg.exe oder ffprobe.exe nicht.'
        }
        Copy-Item -LiteralPath $FoundFFmpeg.FullName -Destination $FFmpegExe -Force
        Copy-Item -LiteralPath $FoundFFprobe.FullName -Destination $FFprobeExe -Force
        $SourceLines = @(
            'FFmpeg wurde von setup_windows.ps1 heruntergeladen.',
            ('Quelle: ' + $DownloadUrl),
            ('Download SHA-256: ' + $DownloadHash),
            ('Datum: ' + (Get-Date -Format o)),
            'Lizenzinformationen: https://ffmpeg.org/legal.html'
        )
        $SourceFile = Join-Path $Root 'tools\ffmpeg\SOURCE.txt'
        [IO.File]::WriteAllLines($SourceFile, $SourceLines, $Utf8NoBom)
    }
    finally {
        Remove-Item -LiteralPath $DownloadDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
else {
    Write-Host 'Lokale FFmpeg-Dateien sind bereits vorhanden. Download wird übersprungen.'
}
Test-NativeProgram -Program $FFmpegExe -DisplayName 'FFmpeg'
Test-NativeProgram -Program $FFprobeExe -DisplayName 'FFprobe'

if (-not $SkipSelfTest) {
    Write-Step 'End-to-End-Selbsttest ausführen'
    $env:VIDEOMERGER_FFMPEG_DIR = $FFmpegBin
    $env:PYTHONUTF8 = '1'
    & $VenvPython -m app.selftest
    if ($LASTEXITCODE -ne 0) {
        throw 'Der End-to-End-Selbsttest ist fehlgeschlagen. Die Anwendung wurde nicht als bereit markiert.'
    }
}
else {
    Write-Warning 'Der End-to-End-Selbsttest wurde auf ausdrücklichen Wunsch übersprungen.'
}

Write-Step 'Deterministische Windows-Pfade diagnostizieren'
$DiagnosticsScript = Join-Path $Root 'diagnostics_windows.ps1'
if (-not (Test-Path -LiteralPath $DiagnosticsScript -PathType Leaf)) {
    throw ('Diagnoseskript fehlt: ' + $DiagnosticsScript)
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DiagnosticsScript
if ($LASTEXITCODE -ne 0) {
    throw 'Die Windows-Pfaddiagnose ist fehlgeschlagen. Das Setup wird nicht als erfolgreich markiert.'
}

if (-not $NoShortcut) {
    Write-Step 'Desktop-Verknüpfung erstellen'
    try {
        $Desktop = [Environment]::GetFolderPath('Desktop')
        $ShortcutPath = Join-Path $Desktop 'VideoMerger.lnk'
        $PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $RunScript = Join-Path $Root 'run_windows.ps1'
        $Shell = New-Object -ComObject WScript.Shell
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $PowerShellExe
        $Shortcut.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $RunScript)
        $Shortcut.WorkingDirectory = $Root
        $Shortcut.Description = 'VideoMerger - lokale Videoverarbeitung'
        $Shortcut.Save()
        Write-Host ('Desktop-Verknüpfung: ' + $ShortcutPath) -ForegroundColor Green
    }
    catch {
        Write-Warning ('Die optionale Desktop-Verknüpfung konnte nicht erstellt werden: ' + $_.Exception.Message)
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host 'SETUP ERFOLGREICH - ALL SYSTEMS READY' -ForegroundColor Green
Write-Host 'Start: .\run_windows.ps1' -ForegroundColor White
Write-Host 'Oder doppelklicken: VideoMerger starten.cmd' -ForegroundColor White
Write-Host '============================================================' -ForegroundColor Green
exit 0
