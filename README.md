# VideoMerger 1.2.4 for Windows

VideoMerger 1.2.4 is an additive local release built directly from the tested 1.2.3 application. Existing Basic Merge, separate Stage 1/Stage 2, active manual ordering, four transitions, audio modes, watermarking, validation, hardware selection, non-overwriting exports and Windows setup remain available.

## New in 1.2.4

### Large Video Pool — Required-Only processing

The Input Folder is a source library, not a render queue. Discovery uses lightweight `ffprobe` metadata only (duration, resolution, fps, codec, audio presence, size — never a full decode of every file) and caches the result. The selection stops as soon as the **current active order** (Natural / Manual / Randomized) covers the voiceover-derived target duration: only the required clips are rendered. With 300 available and ~14 needed, exactly ~14 clips enter the pipeline and the rest never appear in any decode, filter, transition or encode stage. The final clip is trimmed to fit; if the material is still short, the Full-Timeline Loop repeats the selected A-B-C sequence and Hold Last Frame holds only the final frame. Pre-processing time does not scale with the unused pool size, and changing subtitle style/quote text/Intro/Outro never re-analyzes the pool. The GUI shows `Videos in Input Folder / Required / Selected / Not Used / Target Duration` and updates after Analyze, voiceover changes, Randomize and manual reorder.

### Quote Card (optional, silent)

A new optional section between Intro and Main: `Intro → (transition) → Quote → (transition) → Main → (transition) → Outro`. Enabled with `[ ] Add Quote`; duration 1.0–3.0 s (default 2.0 s). Cinematic/editorial design: dark neutral background with subtle vignette, the quote as a single focal point slightly above mathematical center, automatic balanced line breaks (word/phrase boundaries, never a broken word, no lone-word lines) and resolution-aware font size rendered at native resolution (1080p / 1440p / 4K, 16:9 and 9:16). It uses the existing transition system. **Quote Card Audio is Silent by design**: no voiceover, no generated music, no subtitles — the quote never enters the SRT/VTT/burn-in timeline and never shifts Main Video subtitle timing. A live GUI quote preview reuses the same layout and line-break logic as the renderer. The background architecture is extensible to custom backgrounds.

### Real subtitle preview (Preview ≈ Final Render)

The GUI subtitle preview renders the exact demo cue through the same line-breaking, font-metrics, safe-area and position logic as the burned-in renderer — no fake GUI text, no FFmpeg render. Font, style, animation, position, wrapping, the max-two-line behavior and word highlighting update instantly.

### Four additional fonts

Inter, Manrope, Lora and Roboto (Regular + Bold) join the existing Noto Sans fallback: readable, professional long-form look with German and English support and strong bolds. All fonts are legally redistributable (OFL / Apache-2.0, licenses shipped in `tools/fonts/`); proprietary fonts remain detection-only with a legal fallback. The selector lists all available fonts and the renderer stays resolution-aware.

### 1.2.4 defaults

Intro/Main/Outro Original Audio all default to **Original** (Mute/Low/Original remain, independently settable); subtitle animation default is **Static Phrase** (Long-Form / YouTube Landscape, all 5 animations still selectable); Output Preset **YouTube Landscape** + Quality **Maximum** are unchanged. The one-click workflow now covers Video Pool + Voiceovers + Scripts + Music + Intro + optional Quote + Main + Outro + Subtitles + Watermark → FinalVideo, still handing the actual rendered `MainVideo.mp4` to Stage 2.

## New in 1.2.3

### Random video ordering

**Randomize Order** performs a genuine unbiased Fisher-Yates permutation of the current active clip list only — it never re-adds removed files and never inspects filenames. The shuffled sequence becomes the active preview/export order immediately and persists across restarts. **Reset to Default Order** restores the natural numeric/alphabetical order (1, 2, 3, 10 — never 1, 10, 2, 3), never the last random sequence. Manual drag-and-drop and the move buttons remain the highest control and always override automatic ordering.

### Maximum Quality and YouTube Landscape defaults

The application default is **Maximum Quality**: real `libx264` encoding with CRF 16, preset `slow`, High Profile and yuv420p — not a cosmetic label. The default output preset is **YouTube Landscape**: 16:9, Auto/highest appropriate source resolution, Maximum Quality, Source/Auto FPS, H.264 High, AAC-LC 48 kHz, Fast Start. Source resolution is preserved (4K stays 4K) and source FPS is preserved unless explicitly changed. Both presets are visible in the main GUI (Output Preset & Quality group) and map to real encoder arguments; **Custom** keeps the explicit CRF 14–28 / fast–slow advanced settings.

### Optional Intro

An independent **Intro** can be assigned in Stage 2. The final composition is **Intro → Main → Outro**; each section keeps its own original audio with an independent Mute / Low / **Original** (default) setting. The Intro receives no main voiceover, no main generated background music and no subtitles. The selected transition and duration apply between every adjacent section.

### Multiple voiceover / script files

A dedicated **Voiceover Order** list supports Add / Remove / Move Up / Down / Top / Bottom / **Reset to Default Order**. New units are inserted in natural numeric/alphabetical order; the order is independent of the video order and persists. Scripts auto-associate by normalized basename (e.g. `intro.wav` ↔ `intro.txt`) and can be overridden per row.

- **Single Global Script** (default): one text file drives the complete concatenated voiceover timeline.
- **Multiple Matched Scripts**: every voiceover needs its own script; a missing script aborts with a clear `SUBTITLE GENERATION FAILED [script matching]` error — never a silent captionless output.

Each Voiceover/Script pair is aligned separately (reusing the per-audio transcription cache) and concatenated with cumulative offsets into **one canonical subtitle timeline** for SRT/VTT/burn-in. Voiceover is never looped; background music spans the complete Main Video only.

### One-click complete workflow

**CREATE FINAL VIDEO – ONE CLICK** still performs the whole pipeline: ordered videos, optional Intro/Outro, multi-voiceover order and scripts, per-pair alignment, canonical subtitle timeline, Main duration, trim/loop, voiceover/music/ducking, original-audio settings, transitions, subtitles, watermark — rendering the actual validated `MainVideo.mp4` and automatically feeding that exact file into the Intro → Main → Outro Stage-2 composition. Outputs: `MainVideo.mp4`, `MainVideo.srt`, `MainVideo.vtt`, `FinalVideo.mp4`. Stage 1 (`CREATE MAIN VIDEO`) and Stage 2 (`ADD INTRO/OUTRO`) remain separately usable.

Performance: changing only the video order, subtitle style/animation, Intro or Outro never reruns ASR/alignment; all existing caches remain intact.

### New in 1.2.2

### Professional Long-Form captions

The 16:9 defaults are **Clean Editorial + Type Reveal + Bottom**. The subtitle engine now uses punctuation, phrase boundaries, selected-font advance metrics and visual balance to create stable sentence/phrase blocks. Long-Form output normally uses one line and is strictly limited to two explicit lines. It repairs isolated one-word blocks where surrounding words exist.

Every visual event retains the complete final phrase and canonical line break. Reveal/highlight changes therefore do not resize, recenter or reflow the caption region.

Animations:

- Type Reveal
- Color Change
- Word Highlight
- Outline Highlight
- Static Phrase

For synchronized animations, event boundaries come only from the canonical acoustic voiceover word timeline. The authoritative script still controls visible spelling, punctuation and umlauts. There is no character-count or equal-duration timing.

Positions: **Bottom, Medium-Low, Middle, Top**. Font size, measured wrapping, safe margins, outline and position scale for 1920×1080, 3840×2160 and 1080×1920.

Exactly ten presets remain: five improved calm Long-Form styles and the existing five readable Short-Form styles.

### Legal font handling and preview

Font choices are **Eveleth Clean**, **Modern Sans Bold** and **Clean Sans**. Eveleth is commercial and is never bundled. VideoMerger detects a user-installed licensed copy; otherwise it reports that state and uses bundled Noto Sans under the SIL Open Font License. Bundled/discoverable fonts use their real cmap/hmtx advances for layout, with Qt metrics as the live-GUI fallback.

The GUI has an immediate embedded preview plus a larger preview for style, font, animation and position. Bundled fonts are registered process-locally for the preview. Optional **Subtitle Debug Overlay** is off by default and can burn the current word and exact start/end timestamps.

### Actual one-click workflow

The preserved buttons **CREATE MAIN VIDEO** and **CREATE FINAL VIDEO** still run the separate stages. The new **CREATE FINAL VIDEO – ONE CLICK** performs:

```text
active manual clip order + configured Stage 1 roles
→ actual validated MainVideo file
→ that exact generated path passed to the existing Stage 2 renderer
→ validated FinalVideo
```

It includes configured voiceover, script, music, subtitle presentation, watermark, output format/resolution, transitions, quiet gap, outro and outro audio mode. Stage 2 clears generated voiceover, music and subtitle roles. The quiet pre-outro gap remains free of those streams. Outro original audio defaults to Original and supports Mute, Low and Original.

The headless CLI supports `--stage complete`, `--subtitle-animation`, `--subtitle-font` and `--subtitle-debug-overlay` as well.

## Local subtitle pipeline

```text
voiceover only → local faster-whisper acoustic word timestamps
→ canonical reusable word timeline → measured phrase/two-line layout
→ SRT + VTT + ASS burn-in → one-pass MainVideo encode
→ decoded first/middle/final verification frames
```

Visual-only changes to style, font, animation, color or position reuse the cached ASR/alignment. Transition blur **and its per-pixel ramp blend** are now both timeline-gated to visible transition windows. The executed two-minute 1280×720 CPU/libx264 slow benchmark with cold local `small` ASR, captions, voiceover and music measured **115.559 seconds / 1.926 minutes analysis-to-final** on the Linux development host (not a native Windows hardware guarantee), improving the 1.2.1 reference of about 5.7 minutes.

If a required subtitle stage fails, the incomplete bundle is removed and the error begins with:

```text
SUBTITLE GENERATION FAILED [actual stage]: actual error
```

## Local setup

No cloud API or remote renderer is required. Initial setup creates the project-local `.venv`, installs dependencies, downloads the local `small` model and obtains project-local FFmpeg/FFprobe. It requires no administrator account, global PATH edit, manual virtual-environment activation or manual FFmpeg relocation.

Save `VideoMerger_Final_1.2.4.zip` in Downloads and run Windows PowerShell:

```powershell
$Zip = Join-Path $HOME "Downloads\VideoMerger_Final_1.2.4.zip"
$ProjectRoot = Join-Path $HOME "Downloads\VideoMerger_Final_1.2.4"
if (Test-Path -LiteralPath $ProjectRoot) { Remove-Item -LiteralPath $ProjectRoot -Recurse -Force }
New-Item -ItemType Directory -Path $ProjectRoot -Force | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $ProjectRoot -Force
$Required = @("PROJECT_ROOT.txt", "setup_windows.ps1", "run_windows.ps1", "diagnostics_windows.ps1", "app\main.py", "requirements.txt")
foreach ($Name in $Required) { if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $Name))) { throw "ZIP structure error: $Name is missing" } }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "setup_windows.ps1")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "diagnostics_windows.ps1")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "run_windows.ps1")
```

`-ExecutionPolicy Bypass` applies only to each launched process. Originals are never modified. Exports use deterministic non-overwriting names.

See [README_DE.md](README_DE.md), [docs/ARCHITECTURE_DE.md](docs/ARCHITECTURE_DE.md), [BUILD_REPORT.md](BUILD_REPORT.md) and `test_evidence/1.2.4/` for detailed operation, architecture and executed evidence.
