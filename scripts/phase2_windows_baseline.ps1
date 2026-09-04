# VideoMerger Phase 2 baseline benchmark
# This is a benchmark harness only. It does not change application code or settings.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputFolder,

    [Parameter(Mandatory = $true)]
    [string[]]$Voiceover,

    [Parameter(Mandatory = $true)]
    [string]$Script,

    [Parameter(Mandatory = $true)]
    [string]$Music,

    [Parameter(Mandatory = $true)]
    [string]$Intro,

    [Parameter(Mandatory = $true)]
    [string]$Outro,

    [string]$OutputRoot = '',
    [string]$RunId = '',
    [ValidateSet('16:9', '9:16')]
    [string]$Aspect = '16:9',
    [string]$Resolution = 'Auto',
    [ValidateSet('CPU', 'Auto', 'NVIDIA NVENC', 'Intel Quick Sync', 'AMD AMF')]
    [string]$Encoding = 'CPU',
    [ValidateSet('maximum', 'high', 'balanced', 'fast', 'custom')]
    [string]$Quality = 'maximum',
    [int]$Crf = 18,
    [double]$Transition = 1.0,
    [ValidateSet('German', 'English', 'Auto')]
    [string]$Language = 'German',
    [ValidateSet('long_1', 'long_2', 'long_3', 'long_4', 'long_5', 'short_1', 'short_2', 'short_3', 'short_4', 'short_5')]
    [string]$SubtitleStyle = 'long_1',
    [ValidateSet('type_reveal', 'color_change', 'word_highlight', 'outline_highlight', 'static_phrase')]
    [string]$SubtitleAnimation = 'type_reveal',
    [ValidateSet('eveleth_clean', 'modern_sans_bold', 'clean_sans')]
    [string]$SubtitleFont = 'modern_sans_bold',
    [ValidateSet('Bottom', 'Medium-Low', 'Middle', 'Top')]
    [string]$SubtitlePosition = 'Bottom',
    [string]$Watermark = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$NotMeasured = 'not measured'

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($env:OS -ne 'Windows_NT') {
    throw 'This benchmark must run on Windows.'
}
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Run this benchmark with PowerShell 7 or newer.'
}
Set-Location -LiteralPath $Root

$Python = Join-Path $Root '.venv\Scripts\python.exe'
$FfmpegBin = Join-Path $Root 'tools\ffmpeg\bin'
$Ffmpeg = Join-Path $FfmpegBin 'ffmpeg.exe'
$Ffprobe = Join-Path $FfmpegBin 'ffprobe.exe'
foreach ($RequiredPath in @($Python, $Ffmpeg, $Ffprobe)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file is missing: $RequiredPath"
    }
}

function Require-File {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

$InputFolder = (Resolve-Path -LiteralPath $InputFolder).Path
$Script = Require-File $Script 'Script'
$Music = Require-File $Music 'Music'
$Intro = Require-File $Intro 'Intro'
$Outro = Require-File $Outro 'Outro'
$Voiceover = @($Voiceover | ForEach-Object { Require-File $_ 'Voiceover' })
if ($Watermark) { $Watermark = Require-File $Watermark 'Watermark' }

if (-not $RunId) {
    $RunId = Get-Date -Format 'yyyyMMdd_HHmmss'
}
$RunId = $RunId -replace '[^A-Za-z0-9_.-]', '_'
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $Root 'benchmark_results\phase2_baseline'
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$RunDirectory = Join-Path $OutputRoot $RunId
if (Test-Path -LiteralPath $RunDirectory) {
    throw "Benchmark output already exists; use a new -RunId: $RunDirectory"
}
New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
$RenderOutput = Join-Path $RunDirectory 'output'
New-Item -ItemType Directory -Path $RenderOutput -Force | Out-Null
$StdoutPath = Join-Path $RunDirectory 'application.stdout.log'
$StderrPath = Join-Path $RunDirectory 'application.stderr.log'
$TimedLogPath = Join-Path $RunDirectory 'application.timed.tsv'
$SamplesPath = Join-Path $RunDirectory 'resource.samples.jsonl'
$ResultPath = Join-Path $RunDirectory 'baseline.result.json'

$VideoExtensions = @('.mp4', '.mov', '.mkv', '.m4v', '.avi', '.webm')
$PoolFiles = @(Get-ChildItem -LiteralPath $InputFolder -File | Where-Object {
    $VideoExtensions -contains $_.Extension.ToLowerInvariant()
})

function Get-VersionLine {
    param([string]$Executable)
    $Output = @(& $Executable -version 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Could not execute $Executable -version" }
    if ($Output.Count -eq 0) { return '' }
    return [string]$Output[0]
}

$Cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfLogicalProcessors, MaxClockSpeed
$Computer = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 TotalPhysicalMemory
$GpuNames = @(Get-CimInstance Win32_VideoController | ForEach-Object { [string]$_.Name })
$Metadata = [ordered]@{
    schema = 1
    run_id = $RunId
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    project_root = $Root
    powershell = $PSVersionTable.PSVersion.ToString()
    python = $Python
    ffmpeg = $Ffmpeg
    ffprobe = $Ffprobe
    ffmpeg_version = Get-VersionLine $Ffmpeg
    ffprobe_version = Get-VersionLine $Ffprobe
    os = (Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 Caption, Version, BuildNumber)
    cpu = $Cpu
    total_physical_memory_bytes = [int64]$Computer.TotalPhysicalMemory
    gpu_names = $GpuNames
    input_folder = $InputFolder
    discovered_pool_files_before_export = $PoolFiles.Count
    selected_settings = [ordered]@{
        aspect = $Aspect
        resolution = $Resolution
        encoding = $Encoding
        quality = $Quality
        crf = $Crf
        transition_seconds = $Transition
        transition_effect = 'cross_dissolve'
        transition_ease = 'ease_in_out'
        subtitles = $true
        language = $Language
        subtitle_style = $SubtitleStyle
        subtitle_animation = $SubtitleAnimation
        subtitle_font = $SubtitleFont
        subtitle_position = $SubtitlePosition
        music = $Music
        voiceover_count = $Voiceover.Count
        intro = $Intro
        outro = $Outro
        watermark = [bool]$Watermark
        one_click = $true
    }
}

$env:VIDEOMERGER_FFMPEG_DIR = $FfmpegBin
$env:HF_HOME = Join-Path $Root 'tools\alignment_models'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

$CliArguments = [System.Collections.Generic.List[string]]::new()
foreach ($Value in @('-u', '-m', 'app.cli', '--stage', 'complete', '--input', $InputFolder,
        '--output', $RenderOutput, '--aspect', $Aspect, '--resolution', $Resolution,
        '--encoding', $Encoding, '--crf', [string]$Crf, '--quality', $Quality,
        '--transition', [string]$Transition, '--transition-effect', 'cross_dissolve',
        '--transition-ease', 'ease_in_out', '--music', $Music, '--subtitles',
        '--language', $Language, '--subtitle-style', $SubtitleStyle,
        '--subtitle-animation', $SubtitleAnimation, '--subtitle-font', $SubtitleFont,
        '--subtitle-position', $SubtitlePosition, '--global-script', $Script,
        '--script-mode', 'single', '--original-audio', 'mute', '--music-volume', '44',
        '--voiceover-pause', '0.7', '--pause', '1.0', '--short-video', 'hold',
        '--duration-fit', 'cut', '--video-speed', '1.0', '--voiceover-order', 'natural')) {
    [void]$CliArguments.Add([string]$Value)
}
foreach ($Path in $Voiceover) {
    [void]$CliArguments.Add('--voiceover')
    [void]$CliArguments.Add($Path)
}
if ($Watermark) {
    [void]$CliArguments.Add('--watermark')
    [void]$CliArguments.Add($Watermark)
}

$Records = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
$ResourceSamples = [System.Collections.Generic.List[object]]::new()
$FfmpegProcessStarts = [System.Collections.Generic.List[object]]::new()
$FfmpegProcessStops = [System.Collections.Generic.List[object]]::new()
$FfprobeProcessStarts = [System.Collections.Generic.List[object]]::new()
$FfprobeProcessStops = [System.Collections.Generic.List[object]]::new()
$FfmpegStartSource = "VideoMergerPhase2Start_$RunId"
$FfmpegStopSource = "VideoMergerPhase2Stop_$RunId"
$FfprobeStartSource = "VideoMergerPhase2ProbeStart_$RunId"
$FfprobeStopSource = "VideoMergerPhase2ProbeStop_$RunId"
$ProcessEventsAvailable = $false

try {
    try {
        Register-CimIndicationEvent -Namespace root/cimv2 `
            -Query "SELECT * FROM Win32_ProcessStartTrace WHERE ProcessName='ffmpeg.exe'" `
            -SourceIdentifier $FfmpegStartSource | Out-Null
        Register-CimIndicationEvent -Namespace root/cimv2 `
            -Query "SELECT * FROM Win32_ProcessStopTrace WHERE ProcessName='ffmpeg.exe'" `
            -SourceIdentifier $FfmpegStopSource | Out-Null
        Register-CimIndicationEvent -Namespace root/cimv2 `
            -Query "SELECT * FROM Win32_ProcessStartTrace WHERE ProcessName='ffprobe.exe'" `
            -SourceIdentifier $FfprobeStartSource | Out-Null
        Register-CimIndicationEvent -Namespace root/cimv2 `
            -Query "SELECT * FROM Win32_ProcessStopTrace WHERE ProcessName='ffprobe.exe'" `
            -SourceIdentifier $FfprobeStopSource | Out-Null
        $ProcessEventsAvailable = $true
    }
    catch {
        Write-Warning ('FFmpeg process event capture unavailable: ' + $_.Exception.Message)
    }

    $Psi = [Diagnostics.ProcessStartInfo]::new()
    $Psi.FileName = $Python
    $Psi.WorkingDirectory = $Root
    $Psi.UseShellExecute = $false
    $Psi.CreateNoWindow = $true
    $Psi.RedirectStandardOutput = $true
    $Psi.RedirectStandardError = $true
    $Psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $Psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    foreach ($Value in $CliArguments) { [void]$Psi.ArgumentList.Add($Value) }
    $Psi.Environment['VIDEOMERGER_FFMPEG_DIR'] = $FfmpegBin
    $Psi.Environment['HF_HOME'] = Join-Path $Root 'tools\alignment_models'
    $Psi.Environment['PYTHONUTF8'] = '1'
    $Psi.Environment['PYTHONUNBUFFERED'] = '1'

    $App = [Diagnostics.Process]::new()
    $App.StartInfo = $Psi
    $OutputHandler = [Diagnostics.DataReceivedEventHandler]{
        param($Sender, $Event)
        if ($null -ne $Event.Data) {
            $Records.Enqueue([pscustomobject]@{
                timestamp = (Get-Date).ToUniversalTime()
                stream = 'stdout'
                message = [string]$Event.Data
            })
        }
    }
    $ErrorHandler = [Diagnostics.DataReceivedEventHandler]{
        param($Sender, $Event)
        if ($null -ne $Event.Data) {
            $Records.Enqueue([pscustomobject]@{
                timestamp = (Get-Date).ToUniversalTime()
                stream = 'stderr'
                message = [string]$Event.Data
            })
        }
    }
    [void]$App.add_OutputDataReceived($OutputHandler)
    [void]$App.add_ErrorDataReceived($ErrorHandler)

    $StartedAt = Get-Date
    $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
    if (-not $App.Start()) { throw 'The benchmark application could not be started.' }
    $RootProcessId = $App.Id
    $App.BeginOutputReadLine()
    $App.BeginErrorReadLine()

    $StdoutWriter = [IO.StreamWriter]::new($StdoutPath, $false, [Text.Encoding]::UTF8)
    $TimedWriter = [IO.StreamWriter]::new($TimedLogPath, $false, [Text.Encoding]::UTF8)
    $SampleWriter = [IO.StreamWriter]::new($SamplesPath, $false, [Text.Encoding]::UTF8)
    $StdoutWriter.AutoFlush = $true
    $TimedWriter.AutoFlush = $true
    $SampleWriter.AutoFlush = $true

    function Drain-Records {
        $Record = $null
        while ($Records.TryDequeue([ref]$Record)) {
            $Timestamp = ([datetime]$Record.timestamp).ToString('o')
            $StdoutWriter.WriteLine($Record.message)
            $TimedWriter.WriteLine($Timestamp + "`t" + $Record.stream + "`t" + $Record.message)
            $Record = $null
        }
    }

    function Drain-ProcessEvents {
        if (-not $ProcessEventsAvailable) { return }
        foreach ($Event in @(Get-Event -SourceIdentifier $FfmpegStartSource -ErrorAction SilentlyContinue)) {
            $NewEvent = $Event.SourceEventArgs.NewEvent
            $FfmpegProcessStarts.Add([pscustomobject]@{
                process_id = [int]$NewEvent.ProcessID
                parent_process_id = [int]$NewEvent.ParentProcessID
                observed_utc = (Get-Date).ToUniversalTime().ToString('o')
            })
            Remove-Event -EventIdentifier $Event.EventIdentifier -ErrorAction SilentlyContinue
        }
        foreach ($Event in @(Get-Event -SourceIdentifier $FfmpegStopSource -ErrorAction SilentlyContinue)) {
            $NewEvent = $Event.SourceEventArgs.NewEvent
            $FfmpegProcessStops.Add([pscustomobject]@{
                process_id = [int]$NewEvent.ProcessID
                observed_utc = (Get-Date).ToUniversalTime().ToString('o')
            })
            Remove-Event -EventIdentifier $Event.EventIdentifier -ErrorAction SilentlyContinue
        }
        foreach ($Event in @(Get-Event -SourceIdentifier $FfprobeStartSource -ErrorAction SilentlyContinue)) {
            $NewEvent = $Event.SourceEventArgs.NewEvent
            $FfprobeProcessStarts.Add([pscustomobject]@{
                process_id = [int]$NewEvent.ProcessID
                parent_process_id = [int]$NewEvent.ParentProcessID
                observed_utc = (Get-Date).ToUniversalTime().ToString('o')
            })
            Remove-Event -EventIdentifier $Event.EventIdentifier -ErrorAction SilentlyContinue
        }
        foreach ($Event in @(Get-Event -SourceIdentifier $FfprobeStopSource -ErrorAction SilentlyContinue)) {
            $NewEvent = $Event.SourceEventArgs.NewEvent
            $FfprobeProcessStops.Add([pscustomobject]@{
                process_id = [int]$NewEvent.ProcessID
                observed_utc = (Get-Date).ToUniversalTime().ToString('o')
            })
            Remove-Event -EventIdentifier $Event.EventIdentifier -ErrorAction SilentlyContinue
        }
    }

    function Get-ResourceSample {
        param([int]$ParentId, [double]$ElapsedSeconds)
        $AllProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $Ids = [System.Collections.Generic.HashSet[int]]::new()
        [void]$Ids.Add($ParentId)
        $Changed = $true
        while ($Changed) {
            $Changed = $false
            foreach ($Item in $AllProcesses) {
                if ($Ids.Contains([int]$Item.ParentProcessId) -and $Ids.Add([int]$Item.ProcessId)) {
                    $Changed = $true
                }
            }
        }
        $WorkingSet = [int64]0
        $FfmpegCount = 0
        foreach ($Id in $Ids) {
            try {
                $Process = Get-Process -Id $Id -ErrorAction Stop
                $WorkingSet += [int64]$Process.WorkingSet64
                if ($Process.ProcessName -ieq 'ffmpeg') { $FfmpegCount++ }
            }
            catch { }
        }
        $CpuPercent = $null
        try {
            $CpuPercent = [double](Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples[0].CookedValue
        }
        catch { }
        return [pscustomobject]@{
            elapsed_seconds = [math]::Round($ElapsedSeconds, 3)
            sampled_utc = (Get-Date).ToUniversalTime().ToString('o')
            descendant_count = $Ids.Count
            descendant_working_set_bytes = $WorkingSet
            ffmpeg_descendant_count = $FfmpegCount
            total_cpu_percent = if ($null -eq $CpuPercent) { $NotMeasured } else { [math]::Round($CpuPercent, 2) }
        }
    }

    while (-not $App.HasExited) {
        Drain-Records
        Drain-ProcessEvents
        $Sample = Get-ResourceSample $RootProcessId $Stopwatch.Elapsed.TotalSeconds
        $ResourceSamples.Add($Sample)
        $SampleWriter.WriteLine(($Sample | ConvertTo-Json -Compress -Depth 4))
        Start-Sleep -Milliseconds 500
    }
    $App.WaitForExit()
    Start-Sleep -Milliseconds 500
    Drain-Records
    Drain-ProcessEvents
    $Stopwatch.Stop()
    $AppExitCode = $App.ExitCode
    $FinishedAt = Get-Date
    $StdoutWriter.Dispose()
    $TimedWriter.Dispose()
    $SampleWriter.Dispose()
    $App.Dispose()
}
finally {
    foreach ($SourceIdentifier in @($FfmpegStartSource, $FfmpegStopSource, $FfprobeStartSource, $FfprobeStopSource)) {
        Unregister-Event -SourceIdentifier $SourceIdentifier -ErrorAction SilentlyContinue
        Get-Event -SourceIdentifier $SourceIdentifier -ErrorAction SilentlyContinue | Remove-Event -ErrorAction SilentlyContinue
    }
}

function Get-TimedRows {
    param([string]$Path)
    $Rows = [System.Collections.Generic.List[object]]::new()
    if (-not (Test-Path -LiteralPath $Path)) { return @($Rows) }
    foreach ($Line in @(Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $Parts = $Line -split "`t", 3
        if ($Parts.Count -eq 3) {
            try {
                $Rows.Add([pscustomobject]@{
                    timestamp = [datetime]::Parse($Parts[0]).ToUniversalTime()
                    stream = $Parts[1]
                    message = $Parts[2]
                })
            }
            catch { }
        }
    }
    return @($Rows)
}

function Get-LogWindow {
    param(
        [object[]]$Rows,
        [scriptblock]$StartPredicate,
        [scriptblock]$EndPredicate,
        [int]$FromIndex = 0
    )
    for ($Index = $FromIndex; $Index -lt $Rows.Count; $Index++) {
        if (-not (& $StartPredicate $Rows[$Index].message)) { continue }
        for ($EndIndex = $Index + 1; $EndIndex -lt $Rows.Count; $EndIndex++) {
            if (& $EndPredicate $Rows[$EndIndex].message) {
                return [ordered]@{
                    start_utc = $Rows[$Index].timestamp.ToString('o')
                    end_utc = $Rows[$EndIndex].timestamp.ToString('o')
                    seconds = [math]::Round(($Rows[$EndIndex].timestamp - $Rows[$Index].timestamp).TotalSeconds, 3)
                }
            }
        }
        return [ordered]@{
            start_utc = $Rows[$Index].timestamp.ToString('o')
            end_utc = $NotMeasured
            seconds = $NotMeasured
        }
    }
    return [ordered]@{
        start_utc = $NotMeasured
        end_utc = $NotMeasured
        seconds = $NotMeasured
    }
}

$TimedRows = @(Get-TimedRows $TimedLogPath)
$RawLines = @(Get-Content -LiteralPath $StdoutPath -Encoding UTF8 -ErrorAction SilentlyContinue)
$CommandRecords = [System.Collections.Generic.List[object]]::new()
$CurrentContext = 'unknown'
for ($Index = 0; $Index -lt $TimedRows.Count; $Index++) {
    $Message = [string]$TimedRows[$Index].message
    if ($Message -match '^Chunk (\d+)/(\d+):') {
        $CurrentContext = "Chunk $($Matches[1])/$($Matches[2])"
    }
    elseif ($Message -match '^Chunk assembly:') {
        $CurrentContext = 'Chunk assembly'
    }
    elseif ($Message -match '^Stage 2 –') {
        $CurrentContext = 'Stage 2 primary'
    }
    elseif ($Message -match '^Clean-variant Stage 2 input:') {
        $CurrentContext = 'Stage 2 clean'
    }
    elseif ($Message -match '^Chunked Rendering: burn subtitles once') {
        $CurrentContext = 'Stage 1 subtitle burn'
    }
    elseif ($Message -match '^Starte FFmpeg') {
        $CommandLine = ''
        if (($Index + 1) -lt $TimedRows.Count) {
            $CommandLine = [string]$TimedRows[$Index + 1].message
        }
        $Category = if ($CommandLine -match '\s-c\s+copy(?:\s|$)') {
            'stream_copy_assembly'
        }
        elseif ($CommandLine -match '\s-frames:v\s+1(?:\s|$)') {
            'visual_verification_frame'
        }
        elseif ($CommandLine -match '\s-c:v\s+') {
            'full_video_encode'
        }
        else {
            'unclassified'
        }
        $CommandRecords.Add([ordered]@{
            context = $CurrentContext
            marker_utc = $TimedRows[$Index].timestamp.ToString('o')
            category = $Category
            command_length = $CommandLine.Length
            command = $CommandLine
        })
    }
}

$PerformanceMarkers = [ordered]@{}
foreach ($Line in $RawLines) {
    if ($Line -match '^PERFORMANCE\s+([^=]+)=([0-9.]+)$') {
        $PerformanceMarkers[$Matches[1]] = [double]$Matches[2]
    }
}

$ChunkWindows = [System.Collections.Generic.List[object]]::new()
foreach ($Row in $TimedRows) {
    if ([string]$Row.message -match '^Chunk (\d+)/(\d+):') {
        $ChunkNumber = [int]$Matches[1]
        $ChunkTotal = [int]$Matches[2]
        $Next = Get-LogWindow $TimedRows `
            { param($Message) $Message -match "^Chunk $ChunkNumber/$ChunkTotal:" } `
            { param($Message) $Message -match '^Chunk (assembly|\d+/\d+):' }
        if ($null -ne $Next) {
            $ChunkWindows.Add([ordered]@{ number = $ChunkNumber; total = $ChunkTotal; log_window = $Next })
        }
    }
}

$StageWindows = [ordered]@{
    stage1 = Get-LogWindow $TimedRows `
        { param($Message) $Message -match '^ONE-CLICK COMPLETE WORKFLOW – START$' } `
        { param($Message) $Message -match '^actual MainVideo input = ' }
    stage2_primary = Get-LogWindow $TimedRows `
        { param($Message) $Message -match '^Stage 2 –' } `
        { param($Message) $Message -match '^Validiere Ausgabedatei mit FFprobe' }
    stage2_clean = Get-LogWindow $TimedRows `
        { param($Message) $Message -match '^Clean-variant Stage 2 input:' } `
        { param($Message) $Message -match '^Validiere Ausgabedatei mit FFprobe' }
}

$RenderCommandCounts = [ordered]@{
    total_application_render_invocations = @($CommandRecords).Count
    full_video_encode_commands = @($CommandRecords | Where-Object { $_.category -eq 'full_video_encode' }).Count
    stream_copy_assembly_commands = @($CommandRecords | Where-Object { $_.category -eq 'stream_copy_assembly' }).Count
    visual_verification_commands = @($CommandRecords | Where-Object { $_.category -eq 'visual_verification_frame' }).Count
    unclassified_commands = @($CommandRecords | Where-Object { $_.category -eq 'unclassified' }).Count
}

function New-ProcessIntervals {
    param(
        [object[]]$Starts,
        [object[]]$Stops
    )
    $Intervals = [System.Collections.Generic.List[object]]::new()
    $StopByPid = @{}
    foreach ($Stop in $Stops) { $StopByPid[[int]$Stop.process_id] = $Stop }
    foreach ($Start in $Starts) {
        $Pid = [int]$Start.process_id
        $End = $StopByPid[$Pid]
        $Seconds = $NotMeasured
        if ($null -ne $End) {
            $Seconds = [math]::Round(((Get-Date $End.observed_utc) - (Get-Date $Start.observed_utc)).TotalSeconds, 3)
        }
        $Intervals.Add([ordered]@{
            process_id = $Pid
            parent_process_id = [int]$Start.parent_process_id
            start_observed_utc = $Start.observed_utc
            stop_observed_utc = if ($null -eq $End) { $NotMeasured } else { $End.observed_utc }
            seconds = $Seconds
        })
    }
    return @($Intervals | Sort-Object { [datetime]$_.start_observed_utc })
}

$FfmpegProcessIntervals = @(New-ProcessIntervals $FfmpegProcessStarts $FfmpegProcessStops)
$FfprobeProcessIntervals = @(New-ProcessIntervals $FfprobeProcessStarts $FfprobeProcessStops)

# Match each application render marker to the next observed FFmpeg process.
# Startup/preflight FFmpeg processes occur before the first marker and remain
# counted in ffmpeg_process_capture, but are not misclassified as renders.
$UsedFfmpegPids = [System.Collections.Generic.HashSet[int]]::new()
foreach ($CommandRecord in $CommandRecords) {
    $MarkerTime = [datetime]$CommandRecord.marker_utc
    $Candidate = $null
    foreach ($Interval in $FfmpegProcessIntervals) {
        if ($UsedFfmpegPids.Contains([int]$Interval.process_id)) { continue }
        $StartTime = [datetime]$Interval.start_observed_utc
        if ($StartTime -ge $MarkerTime.AddSeconds(-2)) {
            $Candidate = $Interval
            break
        }
    }
    if ($null -eq $Candidate) {
        $CommandRecord['process_runtime_seconds'] = $NotMeasured
        $CommandRecord['process_id'] = $NotMeasured
    }
    else {
        [void]$UsedFfmpegPids.Add([int]$Candidate.process_id)
        $CommandRecord['process_runtime_seconds'] = $Candidate.seconds
        $CommandRecord['process_id'] = $Candidate.process_id
        $CommandRecord['process_start_observed_utc'] = $Candidate.start_observed_utc
        $CommandRecord['process_stop_observed_utc'] = $Candidate.stop_observed_utc
    }
}

# Validation markers exist for direct/final and subtitle-burn validation. The
# chunk segment/assembly validators currently have no preceding log marker, so
# they remain visible in the total FFprobe process count but are not guessed
# into this separately classified validation duration.
$ValidationRecords = [System.Collections.Generic.List[object]]::new()
$UsedValidationProbePids = [System.Collections.Generic.HashSet[int]]::new()
foreach ($Row in $TimedRows) {
    if ([string]$Row.message -notmatch '^Validiere .*FFprobe') { continue }
    $MarkerTime = [datetime]$Row.timestamp
    $Candidate = $null
    foreach ($Interval in $FfprobeProcessIntervals) {
        if ($UsedValidationProbePids.Contains([int]$Interval.process_id)) { continue }
        $StartTime = [datetime]$Interval.start_observed_utc
        if ($StartTime -ge $MarkerTime.AddSeconds(-2)) {
            $Candidate = $Interval
            break
        }
    }
    if ($null -eq $Candidate) {
        $ValidationRecords.Add([ordered]@{ marker = $Row.message; runtime_seconds = $NotMeasured })
    }
    else {
        [void]$UsedValidationProbePids.Add([int]$Candidate.process_id)
        $ValidationRecords.Add([ordered]@{
            marker = $Row.message
            runtime_seconds = $Candidate.seconds
            process_id = $Candidate.process_id
        })
    }
}

$MeasuredValidationSeconds = @($ValidationRecords | Where-Object {
    $_.runtime_seconds -is [int] -or $_.runtime_seconds -is [long] -or $_.runtime_seconds -is [double]
} | ForEach-Object { [double]$_.runtime_seconds })
$ValidationRuntime = if ($MeasuredValidationSeconds.Count -eq 0) {
    $NotMeasured
} else {
    [math]::Round(($MeasuredValidationSeconds | Measure-Object -Sum).Sum, 3)
}

$SelectedClipCount = $NotMeasured
foreach ($Line in $RawLines) {
    if ($Line -match '^Eingabedateien:\s*(\d+)') {
        $SelectedClipCount = [int]$Matches[1]
        break
    }
}
$ChunkNumbers = @($TimedRows | ForEach-Object {
    if ([string]$_.message -match '^Chunk (\d+)/(\d+):') {
        [pscustomobject]@{ number = [int]$Matches[1]; total = [int]$Matches[2] }
    }
})
$ChunkCount = if ($ChunkNumbers.Count -eq 0) { $NotMeasured } else { [int](($ChunkNumbers | Measure-Object -Property total -Maximum).Maximum) }
$PerChunkRuntime = @($CommandRecords | Where-Object { $_.context -match '^Chunk \d+/\d+$' } | ForEach-Object {
    [ordered]@{
        chunk = $_.context
        runtime_seconds = $_.process_runtime_seconds
        runtime_source = 'FFmpeg process event; encode only, validation excluded'
        command_length = $_.command_length
        process_id = $_.process_id
    }
})
$AssemblyRuntime = @($CommandRecords | Where-Object { $_.category -eq 'stream_copy_assembly' } | ForEach-Object {
    [ordered]@{ runtime_seconds = $_.process_runtime_seconds; runtime_source = 'FFmpeg process event'; process_id = $_.process_id }
})
$Stage2RuntimeValues = @($StageWindows.Values | Where-Object {
    $null -ne $_ -and ($_.seconds -is [int] -or $_.seconds -is [long] -or $_.seconds -is [double])
} | ForEach-Object { [double]$_.seconds })
$Stage2Runtime = if ($Stage2RuntimeValues.Count -eq 0) {
    $NotMeasured
} else {
    [math]::Round(($Stage2RuntimeValues | Measure-Object -Sum).Sum, 3)
}
$Stage1Runtime = if ($PerformanceMarkers.Contains('total_pipeline_seconds')) {
    [double]$PerformanceMarkers['total_pipeline_seconds']
} else { $NotMeasured }
$SubtitleBurnRuntime = if ($PerformanceMarkers.Contains('subtitle_burn_seconds')) {
    [double]$PerformanceMarkers['subtitle_burn_seconds']
} else { $NotMeasured }

function Get-OutputFacts {
    param([string]$Path)
    $Facts = [ordered]@{
        path = $Path
        size_bytes = [int64](Get-Item -LiteralPath $Path).Length
        ffprobe_ok = $false
        duration_seconds = $NotMeasured
        video = $NotMeasured
        audio = $NotMeasured
    }
    try {
        $ProbeText = (& $Ffprobe -v error -show_streams -show_format -of json $Path 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0) {
            $Probe = $ProbeText | ConvertFrom-Json
            $Format = $Probe.format
            $Video = @($Probe.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1)
            $Audio = @($Probe.streams | Where-Object { $_.codec_type -eq 'audio' } | Select-Object -First 1)
            $Facts.ffprobe_ok = $true
            $Facts.duration_seconds = if ($Format.duration) { [double]$Format.duration } else { $null }
            if ($Video.Count -gt 0) {
                $Facts.video = [ordered]@{ codec = $Video[0].codec_name; width = $Video[0].width; height = $Video[0].height; fps = $Video[0].avg_frame_rate; pix_fmt = $Video[0].pix_fmt }
            }
            if ($Audio.Count -gt 0) {
                $Facts.audio = [ordered]@{ codec = $Audio[0].codec_name; sample_rate = $Audio[0].sample_rate; channels = $Audio[0].channels; duration = $Audio[0].duration }
            }
        }
    }
    catch { }
    return $Facts
}

$Outputs = [System.Collections.Generic.List[object]]::new()
foreach ($OutputFile in @(Get-ChildItem -LiteralPath $RenderOutput -Filter '*.mp4' -File -ErrorAction SilentlyContinue)) {
    $Outputs.Add((Get-OutputFacts $OutputFile.FullName))
}
$PeakMemory = $NotMeasured
$PeakFfmpeg = $NotMeasured
$CpuSamples = @($ResourceSamples | Where-Object {
    $_.total_cpu_percent -is [int] -or $_.total_cpu_percent -is [long] -or $_.total_cpu_percent -is [double]
} | ForEach-Object { [double]$_.total_cpu_percent })
if ($ResourceSamples.Count -gt 0) {
    $PeakMemory = [int64](@($ResourceSamples | Measure-Object -Property descendant_working_set_bytes -Maximum).Maximum)
    $PeakFfmpeg = [int](@($ResourceSamples | Measure-Object -Property ffmpeg_descendant_count -Maximum).Maximum)
}

$Result = [ordered]@{
    schema = 1
    run = $Metadata
    finished_utc = $FinishedAt.ToUniversalTime().ToString('o')
    application_exit_code = $AppExitCode
    wall_clock_seconds = [math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 3)
    logs = [ordered]@{ stdout = $StdoutPath; stderr = $StderrPath; timed = $TimedLogPath; samples = $SamplesPath }
    ffmpeg_process_capture = [ordered]@{
        event_capture_available = $ProcessEventsAvailable
        starts = if ($ProcessEventsAvailable) { @($FfmpegProcessStarts).Count } else { $NotMeasured }
        stops = if ($ProcessEventsAvailable) { @($FfmpegProcessStops).Count } else { $NotMeasured }
        intervals = if ($ProcessEventsAvailable) { @($FfmpegProcessIntervals) } else { $NotMeasured }
        peak_concurrent_descendants = $PeakFfmpeg
    }
    ffprobe_process_capture = [ordered]@{
        event_capture_available = $ProcessEventsAvailable
        starts = if ($ProcessEventsAvailable) { @($FfprobeProcessStarts).Count } else { $NotMeasured }
        stops = if ($ProcessEventsAvailable) { @($FfprobeProcessStops).Count } else { $NotMeasured }
        intervals = if ($ProcessEventsAvailable) { @($FfprobeProcessIntervals) } else { $NotMeasured }
    }
    command_counts = $RenderCommandCounts
    command_records = @($CommandRecords)
    performance_markers = $PerformanceMarkers
    coarse_stage_windows = $StageWindows
    chunk_log_windows = @($ChunkWindows)
    resource_summary = [ordered]@{
        peak_descendant_working_set_bytes = $PeakMemory
        max_total_cpu_percent = if ($CpuSamples.Count -eq 0) { $NotMeasured } else { [math]::Round(($CpuSamples | Measure-Object -Maximum).Maximum, 2) }
        average_total_cpu_percent = if ($CpuSamples.Count -eq 0) { $NotMeasured } else { [math]::Round(($CpuSamples | Measure-Object -Average).Average, 2) }
        sample_count = $ResourceSamples.Count
    }
    metrics = [ordered]@{
        discovered_pool_count = $PoolFiles.Count
        selected_clip_count = $SelectedClipCount
        total_wall_clock_seconds = [math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 3)
        ffmpeg_process_count = if ($ProcessEventsAvailable) { $FfmpegProcessStarts.Count } else { $NotMeasured }
        ffprobe_process_count = if ($ProcessEventsAvailable) { $FfprobeProcessStarts.Count } else { $NotMeasured }
        full_video_encode_count = $RenderCommandCounts.full_video_encode_commands
        stream_copy_assembly_count = $RenderCommandCounts.stream_copy_assembly_commands
        chunk_count = $ChunkCount
        per_chunk_runtime = $PerChunkRuntime
        chunk_assembly_runtime = if ($AssemblyRuntime.Count -eq 0) { $NotMeasured } else { $AssemblyRuntime }
        stage1_runtime_seconds = $Stage1Runtime
        subtitle_burn_runtime_seconds = $SubtitleBurnRuntime
        stage2_runtime_seconds = $Stage2Runtime
        validation_runtime_seconds = $ValidationRuntime
        validation_records = @($ValidationRecords)
        cpu_usage = [ordered]@{
            average_total_percent = if ($CpuSamples.Count -eq 0) { $NotMeasured } else { [math]::Round(($CpuSamples | Measure-Object -Average).Average, 2) }
            maximum_total_percent = if ($CpuSamples.Count -eq 0) { $NotMeasured } else { [math]::Round(($CpuSamples | Measure-Object -Maximum).Maximum, 2) }
        }
        peak_ram_bytes = $PeakMemory
        output_facts = @($Outputs)
    }
    outputs = @($Outputs)
}
$Result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ResultPath -Encoding UTF8

Write-Host ('Baseline result: ' + $ResultPath) -ForegroundColor Cyan
Write-Host ('Wall clock seconds: ' + $Result.wall_clock_seconds)
Write-Host ('Discovered pool files: ' + $PoolFiles.Count)
Write-Host ('Application FFmpeg render markers: ' + $RenderCommandCounts.total_application_render_invocations)
Write-Host ('Full video encode commands: ' + $RenderCommandCounts.full_video_encode_commands)
Write-Host ('Stream-copy assembly commands: ' + $RenderCommandCounts.stream_copy_assembly_commands)
Write-Host ('FFmpeg process start events: ' + $FfmpegProcessStarts.Count)
Write-Host ('FFprobe process start events: ' + $FfprobeProcessStarts.Count)
Write-Host ('Output files: ' + $Outputs.Count)
if ($AppExitCode -ne 0) {
    throw "Benchmark application exited with code $AppExitCode. See $StdoutPath and $StderrPath"
}
