# VideoMerger Phase 2: controlled Windows baseline

This document and `scripts/phase2_windows_baseline.ps1` prepare the **baseline only**. They do not change the production pipeline, quality settings, cache semantics, or output behavior.

A baseline must be captured before performance code is changed. Do not use the Linux/synthetic figures from the previous audit as the real baseline.

## Required benchmark input

Use a dedicated copy of the current application on the Windows machine that normally exhibits the four-hour runtime. Do not benchmark an unrelated checkout or a synthetic media set.

The project should contain:

- approximately 300 videos in one non-recursive Input Pool;
- approximately 100–150 clips selected by the real Voiceover duration and current pool order;
- a real Voiceover and its real authoritative script;
- real background music;
- the actual Intro and Outro used by the project;
- subtitles enabled;
- current Cross Dissolve settings;
- current resolution, CRF/quality preset, FPS, audio, ducking, watermark, and aspect-ratio settings;
- One-Click mode, so Stage 1 and both Stage 2 variants are exercised.

Do not alter the media, reorder it solely to make the benchmark easier, disable security software, lower quality, or disable chunking. Record any unrelated load on the machine.

## Run protocol

1. Connect AC power and close unrelated CPU-, GPU-, disk-, and network-heavy applications.
2. Record the Windows version, Python version, exact FFmpeg/FFprobe version, CPU, GPU, RAM, storage volume, and whether the source/output folders are local or network-backed.
3. Use a new output directory for each run. Do not overwrite an existing generated bundle.
4. Use a clean benchmark copy for the first run. Keep downloaded alignment model files, but clear only application-generated metadata/alignment/render caches if a cold-cache run is desired. Record precisely which caches were cleared. Never delete caches from the user’s production checkout without a backup.
5. Run the baseline once with the normal current settings. The harness defaults to CPU encoding to avoid hiding the architecture behind an unrecorded hardware fallback; pass the actual current encoder explicitly if it is different.
6. Run a second unchanged warm-cache pass in the same isolated copy. The warm pass documents cache reuse separately and must not replace the cold/current baseline.
7. Preserve the complete result directory, including the application logs, timed log, resource samples, JSON result, and both final MP4 files.

The harness invokes the existing headless One-Click path (`python -u -m app.cli --stage complete`), not a replacement renderer. The GUI and CLI share `MainProjectEngine.create_complete()` and `VideoMergerEngine`.

## Command

Run from the repository root in PowerShell 7:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\phase2_windows_baseline.ps1 `
  -InputFolder 'D:\Bench\InputPool' `
  -Voiceover 'D:\Bench\voiceover.wav' `
  -Script 'D:\Bench\script.txt' `
  -Music 'D:\Bench\music.mp3' `
  -Intro 'D:\Bench\intro.mp4' `
  -Outro 'D:\Bench\outro.mp4' `
  -OutputRoot 'D:\Bench\VideoMergerPhase2Results' `
  -RunId 'baseline_cold_2026-08-31' `
  -Aspect '16:9' `
  -Resolution '1920x1080' `
  -Encoding 'CPU' `
  -Quality 'maximum' `
  -Crf 18 `
  -Transition 1.0
```

For the warm pass, use a new `-RunId` and the same input/settings. If the real project uses 9:16, 4K, NVENC, a watermark, Quote/Flyer artwork, or another subtitle preset, pass those exact values. The harness supports `-Watermark` and `-QuoteArtwork`; Quote/Flyer behavior is not changed by the harness.

If `-OutputRoot` is omitted, results are stored under the repository’s ignored
`benchmark_results\phase2_baseline` directory. The input assets may be on another
local drive, but the harness itself uses only the repository’s Python, FFmpeg,
FFprobe, application, and cache/model paths.

The result directory contains:

```text
application.stdout.log       raw application log
application.stderr.log       raw error log
application.timed.tsv         timestamped stdout/stderr markers
resource.samples.jsonl        approximately 0.5-second resource samples
baseline.result.json          machine-readable baseline report
output\                      generated FinalVideo MP4 files
```

`baseline.result.json` contains a `metrics` object with these required keys:

```text
discovered_pool_count
selected_clip_count
total_wall_clock_seconds
ffmpeg_process_count
ffprobe_process_count
full_video_encode_count
stream_copy_assembly_count
chunk_count
per_chunk_runtime
chunk_assembly_runtime
stage1_runtime_seconds
subtitle_burn_runtime_seconds
stage2_runtime_seconds
validation_runtime_seconds
cpu_usage
peak_ram_bytes
output_facts
```

A value is the literal string `not measured` whenever the harness cannot
capture it. The report never fills gaps with estimates. `output_facts` contains
FFprobe-derived duration, resolution, FPS, video/audio codecs, audio properties,
pixel format, and file size for every MP4 produced in the run.

## Measurements that must be reported

### 1. Input and selection

Record:

- discovered video count;
- selected/rendered clip count from the first Stage 1 `Eingabedateien:` line;
- selected order, or a stable digest of that order;
- target duration, effective duration, transition duration, and end padding;
- whether chunking was triggered and the actual chunk count;
- clip occurrences per chunk and overlap occurrences.

The harness records the discovered pool count. The application logs the actual Stage 1 render count and every active render order. Do not infer the selected count from the number of files in the Input Pool.

### 2. Process and encode accounting

Report separately:

- all FFmpeg process-start events captured during the run, including preflight and encoder probes;
- application render invocations (`Starte FFmpeg` markers);
- full video encode commands (`-c:v` commands);
- stream-copy assembly commands (`-c copy` commands);
- visual verification frame commands (`-frames:v 1` commands);
- hardware fallback attempts, if any;
- FFprobe process count, including input analysis and output validation;
- whether any process event capture was unavailable.

The command classification in `baseline.result.json` is based on the actual command lines emitted by the application. If process-event capture is unavailable, the report must say so; application render markers are not a substitute for the total process count because version, preflight, encoder-probe, and validation processes are separate.

### 3. Wall time and stage time

Measure and report in seconds:

- complete wall-clock runtime;
- input discovery time;
- video metadata/FFprobe analysis time;
- Voiceover audio probing;
- music probing;
- model loading;
- ASR/transcription;
- script/alignment mapping;
- subtitle cue, SRT, VTT, canonical timeline, and ASS generation;
- Stage 1 total;
- each chunk independently, including and excluding its validation if available;
- chunk assembly independently;
- Stage 1 subtitle burn independently;
- burned Stage 2 composition independently;
- clean Stage 2 composition independently;
- Stage 2 total;
- final output validation;
- visual verification frame extraction;
- finalization and total runtime.

The current application logs the Stage 1 `PERFORMANCE` values and emits coarse Stage 2/chunk markers. The harness timestamps those markers and records them as coarse windows. These coarse windows must not be mislabeled as exact FFmpeg-only time: they can include validation and Python setup. Exact per-process intervals come from the FFmpeg process event capture. Any timing not exposed or captured must be reported as **not measured**, not estimated.

### 4. Resource usage

Report:

- peak descendant working set in bytes and GiB;
- maximum concurrent FFmpeg descendants;
- average and maximum total CPU utilization samples;
- sample count and sampling interval;
- GPU utilization and GPU memory where practical using the machine’s existing monitoring tool;
- storage read/write throughput where practical;
- whether the disk or CPU was saturated;
- thermal throttling or power-mode changes if observed.

The harness samples the application process tree, descendant working set, FFmpeg concurrency, and total CPU. It does not claim to measure GPU counters or disk throughput; collect those separately when practical.

### 5. Output correctness and invariants

For both final outputs, record:

- path and file size;
- FFprobe success;
- duration;
- resolution and aspect ratio;
- FPS and pixel format;
- video codec and encoder settings;
- audio codec, sample rate, channel count, and duration;
- Fast Start/MP4 atom validation;
- existence and validity of SRT and VTT;
- subtitle end time relative to the Voiceover and quiet end padding;
- absence of black frames, freezes, visible chunk seams, audio gaps, or timing drift.

The burned output must visibly contain subtitles. The clean output must not contain burned subtitles. Their durations must match within the existing validation tolerance. Do not compare encoded bytes: the subtitle layer is intentionally different, while both outputs must preserve the same timeline and audio requirements.

## Baseline result template

The benchmark handoff must include a table like this, populated only with captured values:

| Metric | Cold run | Warm run | Capture source |
|---|---:|---:|---|
| Discovered pool files |  |  | application log |
| Selected Stage 1 clips |  |  | application log |
| Target / output duration |  |  | FFprobe + log |
| Total wall time |  |  | harness |
| FFmpeg process starts |  |  | process events |
| FFprobe process count |  |  | process trace or explicit trace |
| Full video encode commands |  |  | command log |
| Chunk count |  |  | command log |
| Stage 1 total |  |  | application/harness |
| Chunk 1 … N |  |  | process/log trace |
| Chunk assembly |  |  | process/log trace |
| Subtitle burn |  |  | application/harness |
| Burned Stage 2 |  |  | process/log trace |
| Clean Stage 2 |  |  | process/log trace |
| Final validation |  |  | application/harness |
| Peak RAM |  |  | resource samples |
| CPU utilization |  |  | resource samples |
| Final output sizes |  |  | filesystem |

Attach `baseline.result.json` and the complete logs to the result. Do not declare a performance improvement until an equivalent post-change run has the same input/settings and proves the difference with these same measurements.

## Scope gate after the baseline

No production optimization is authorized by this preparation alone. After the real baseline is captured, use the measured encode/process breakdown to select the smallest safe optimization. Preserve the current pool selection, cache invalidation rules, transitions, audio, subtitles, watermark, Quote/Flyer, Intro, Outro, SRT/VTT, dual outputs, resolution, FPS, CRF, and preset.
