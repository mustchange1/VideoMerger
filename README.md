# VideoMerger 1.4.0 for Windows

## New in 1.4.0

### Multiple source folders and folder-aware selection

Add any number of configured video folders with **Add Folder**, **Remove Folder**, and **Clear All**. Folders persist in project settings; each clip keeps its resolved source-folder identity. Automatic selection uses deterministic randomized folder alternation, never repeats a folder while another configured folder still has usable clips, and falls back only when no alternative remains. An explicit manual order disables alternation; Required-Only, Hold Last Frame, Full-Timeline Loop, and Smart Last-Clip Stretch keep their existing semantics.

### Independent merge-duration controls

**Duration Before Merge** defaults to `0.70x` and applies to each normal selected visual clip (`timeline_duration = source_duration / 0.70`) before timeline construction. **Duration After Merge** defaults to disabled / `1.00x` and runs as a separate post-merge operation on the complete Stage-1 master. Smart Last-Clip Stretch remains after timeline construction and before rendering; Stage-2 Intro, Flyer, and Outro are not altered by Before Merge.

### Flyer, Image Insertion and subtitle output defaults

Quote/Flyer remains an independent artwork-only Stage-2 section and defaults to 4.0 seconds. **Image Insertion** is a separate optional, silent Stage-2 section: PNG/JPG/JPEG/WEBP, After Intro by default, 4.0 seconds, Fit, 100% zoom, Natural look, and the existing Cross Dissolve/Crossfade with a 1.0 second boundary request. Its settings never enter the Stage-1 render-cache fingerprint.

Subtitle output is explicit and defaults to **With Burned-in Subtitles + SRT + VTT** whenever a subtitle source is requested. **With Burned-in Subtitles only** burns the same aligned ASS timeline but creates no SRT/VTT files. **Without Subtitles** skips alignment, burn-in, SRT and VTT generation while preserving voiceover/audio timing. Landscape long-form subtitles default to **Center**; vertical short-form subtitles default to **Bottom Center**. Saved/manual position overrides remain authoritative.

## New in 1.3.0

### Windows subtitle filtergraph fix (root cause)

FFmpeg now always runs with the project root as its working directory and every render-time file the filtergraph references (the staged ASS subtitle file and the bundled `tools/fonts` directory) is app-staged under that root with ASCII names — the `subtitles=`/`fontsdir=`/`fontfile=` values become plain relative POSIX paths (`temp/MainVideo_16x9_burn.ass`, `tools/fonts`). No drive-letter colon, backslash, space or umlaut can appear in the value on ANY Windows machine, whatever the unpack path (`C:\Users\Jürgen Müller\Downloads\…` works). Paths outside the anchor are emitted UNQUOTED with forward slashes and the verified two-level escape table — the old quoted form could not represent an apostrophe at all (`C:/Users/O'Brien/…` aborted the render) and fed absolute Windows paths through both parser passes and the libass code-page `fopen`. Works with all subtitle fonts/animations/positions at 16:9, 9:16, 1080p and 4K; covered by real libass burn regression tests over hostile paths (umlauts, spaces, apostrophes) and a non-ASCII working directory.

### Automatic Chunked Rendering for large projects

On Windows, ordinary renders still use the existing single FFmpeg command whenever it is below the conservative safety target. Larger commands automatically use transition-aware Chunked Rendering: segments contain only the required active clips, preserve the exact visual/audio timeline, overlap a boundary clip only to render its existing transition, then trim that overlap before stream-copy assembly. Subtitles are burned once after the complete clean master is assembled, so SRT/VTT/ASS timing remains global and continuous. Failed segments, cancellation, assembly errors and invalid final output are cleaned up and reported; the legacy approximately 30,000-character guard remains as the final backstop rather than the normal large-project workflow.

### Smart Last-Clip Stretch (Duration Fit Mode)

New **Duration Fit Mode**: `Cut Last Clip` (default, exactly the proven behavior) or `Stretch Last Clip`. In stretch mode only the final selected clip is slowed as much as necessary so its complete content fills the voiceover target — often one clip fewer is rendered (no short sliver clip). The maximum stretch is configurable: `5 % / 10 % (default) / 15 % / 20 % / Custom`. Transitions, order and visual continuity are preserved; a required stretch beyond the limit falls back to the normal last-clip trimming — never to Hold Last Frame.

### Global Video Speed

**Main Video Speed** 0.50x–2.00x (default 1.00x). The voiceover remains the timing authority: subtitle timing, voiceover and music behavior are unchanged; clip playback rate (setpts/atempo) and the required clip selection adapt. Verified by byte-identical SRT output at 1.00x and 1.50x.

### Main Video End Padding (manual)

The short visual gap after the voiceover is now a free manual setting (0.0–5.0 s). The existing default of ~1 second is preserved exactly.

### Quote / Flyer artwork (optional, silent)

The optional Stage-2 section is composed as `Intro → Cross Dissolve → Quote/Flyer → Cross Dissolve → Main → Cross Dissolve → Outro`. It is disabled by default. Enable it and choose a finished PDF, PNG, JPG, JPEG, or WEBP artwork. PDFs expose their page count and selected page; Fit, Fill, and Crop preserve the artwork aspect ratio for 16:9, 9:16, 1080p, and 4K outputs. The artwork duration defaults to 4.0 seconds and uses the existing transition safety/clamping logic.

The Quote/Flyer is visual-only: no voiceover, music, subtitles, or Main Video audio is routed into that section. PDF pages are rasterized internally with PyMuPDF into render-time temporary files, which are removed automatically and never written to the normal Output folder. The live preview updates for artwork, PDF page, Fit/Fill/Crop, aspect ratio, and output resolution.

### Image Insertion (optional, silent Stage 2)

Image Insertion is independent of Quote/Flyer/PDF. Enable it in the Stage-2 panel, choose one PNG, JPG, JPEG, or WEBP, and place it **After Intro** or **Before Outro** (the default is After Intro). The image uses a practical editable duration with a 4.0 second default, the selected Cross Dissolve/Crossfade family with a separately clamped 1.0 second boundary default, Fit/Fill/Crop framing, non-distorting 100% default zoom, and five deterministic looks: Natural, Cinematic, Moody, Film, and Dark Editorial. The live preview uses the selected image, aspect, framing, zoom, and look.

The image section receives no voiceover, music, original audio, or subtitle timing; the Stage-2 graph supplies matching silence and keeps every transition boundary gap-free. One-Click, Quote/Flyer, Intro/Outro, chunked rendering, landscape/portrait, 1080p/4K, and all subtitle output modes use the same real image input path. Image settings are persisted but intentionally excluded from the Stage-1 cache fingerprint, so image-only changes reuse the validated Main Video.

### Cleaner subtitle segmentation + larger preview

Long-Form cues are preferably 1–2 measured lines of natural phrases; one/two-word captions are merged/rebalanced into neighbors (word-level timing untouched). The live preview and the new larger preview dialog paint through the SAME renderer geometry routine (font, size, wrapping, style, position, safe area, colors/highlights, animation staging with a word-progress slider).

### Clean Output directory + flexible subtitle outputs

The Output folder contains only useful user-facing files. The subtitle output mode controls the actual contract: `With Burned-in Subtitles + SRT + VTT` writes the primary burned-in video, a `_no_subtitles` master, SRT, and VTT; `With Burned-in Subtitles only` writes only the primary burned-in video; `Without Subtitles` writes only the primary clean video. Verification PNGs and the subtitle timeline JSON live under `temp/` (internal evidence/cache) instead of Output.

### Automatic local YouTube title + description (free, local, unlimited)

Every successful one-click final video automatically produces `FinalVideo_16x9_YouTube.txt` (`TITLE:` / `DESCRIPTION:` / `LANGUAGE:`) generated from the authoritative voiceover transcript: strong opening, useful summary in the author's own words, important themes as verbatim key phrases, one natural channel-follow CTA (philosophical/spiritual/modern), German content → German metadata, English → English. The generator is deterministic pure Python (always available offline, nothing invented, no keyword stuffing); an optional locally running Ollama may polish it under strict validation. No OpenAI/Claude/Gemini/paid API, no subscription, no per-video credits. Metadata problems never block rendering and are reported clearly; without an authoritative transcript no metadata is invented.

### One-Click workflow (Video Pool + everything)

`CREATE FINAL VIDEO – ONE CLICK` produces Video Pool + Voiceover(s) + Script(s) + Background Music + Subtitles + Watermark + Intro + optional Quote/Flyer + optional Image Insertion + Main Video + Outro = **FinalVideo** in one click; the rendered Main Video flows into Stage 2 automatically (no manual Stage-1→Stage-2 selection). Stage 1 and Stage 2 remain separately usable.

### Current defaults

Intro/Main/Outro Original Audio = Original · Subtitle Animation = Static Phrase · YouTube Landscape · Maximum Quality · Cross Dissolve = 1.0 s default · Music = 44 % Balanced default (voiceover remains dominant) · End Padding ≈ 1 s · Quote/Flyer disabled unless enabled (4.0 s, Fit) · Duration Fit = Cut Last Clip · Maximum Stretch = 10 % · Duration Before Merge = 0.70x · Duration After Merge = disabled / 1.00x. All existing features (transitions, ordering, loops, hold, caching, fonts, 4K, watermark, ducking, multi-voiceover) are unchanged; explicit saved transition and audio values remain authoritative.

# VideoMerger 1.3.0 for Windows

VideoMerger 1.3.0 is an additive local release built directly from the tested 1.2.4 application. Everything below the 1.3.0 section documents the preserved earlier releases. Existing Basic Merge, separate Stage 1/Stage 2, active manual ordering, four transitions, audio modes, watermarking, validation, hardware selection, non-overwriting exports and Windows setup remain available.

## New in 1.2.4

### Large Video Pool — Required-Only processing

The Input Folder is a source library, not a render queue. Discovery uses lightweight `ffprobe` metadata only (duration, resolution, fps, codec, audio presence, size — never a full decode of every file) and caches the result. The project-level **Video Order** selector offers **Natural**, **Alphabetical**, **Random**, and **Manual**. The selected order is resolved before Required-Only duration selection, so the GUI table, pool counts, preview, One-Click, chunked rendering, and final timeline all consume the same effective sequence. Natural uses numeric-aware filenames and the existing source-folder alternation; Alphabetical uses filename order; Random performs a fresh Fisher-Yates permutation and then applies folder alternation without saving it as Manual; Manual preserves the explicitly persisted drag/move sequence. The selection stops as soon as the active order covers the voiceover-derived target duration: only the required clips are rendered. With 300 available and ~14 needed, exactly ~14 clips enter the pipeline and the rest never appear in any decode, filter, transition or encode stage. The final clip is trimmed to fit; if the material is still short, the Full-Timeline Loop repeats the selected A-B-C sequence and Hold Last Frame holds only the final frame. Pre-processing time does not scale with the unused pool size, and changing subtitle style/Quote/Flyer/Intro/Outro never re-analyzes the pool. The GUI shows `Videos in Input Folder / Required / Selected / Not Used / Target Duration` and updates after Analyze, voiceover changes, order-mode changes, Randomize and manual reorder.

### Quote / Flyer artwork (optional, silent)

The optional section is composed as `Intro → (Cross Dissolve) → Quote/Flyer → (Cross Dissolve) → Main → (Cross Dissolve) → Outro`. Enable it with `[ ] Include Quote / Flyer`; it is disabled by default and lasts 0.5–5.0 seconds (default 4.0 seconds). The GUI has no text Quote field and no generated-text mode. It accepts PDF, PNG, JPG, JPEG, and WEBP artwork, with selected PDF page, Fit/Fill/Crop framing, and output-aware preview. PDF pages are rasterized internally with PyMuPDF and temporary rasters are removed after export.

**Quote/Flyer Audio is silent by design**: no voiceover, music, subtitles, or Main Video audio is routed into the section. It never enters the SRT/VTT/burn-in timeline. The live GUI preview updates for artwork, PDF page, Fit/Fill/Crop, aspect ratio, and output resolution.

### Real subtitle preview (Preview ≈ Final Render)

The GUI subtitle preview renders the exact demo cue through the same line-breaking, font-metrics, safe-area and position logic as the burned-in renderer — no fake GUI text, no FFmpeg render. Font, style, animation, position, wrapping, the max-two-line behavior and word highlighting update instantly.

### Four additional fonts

Inter, Manrope, Lora and Roboto (Regular + Bold) join the existing Noto Sans fallback: readable, professional long-form look with German and English support and strong bolds. All fonts are legally redistributable (OFL / Apache-2.0, licenses shipped in `tools/fonts/`); proprietary fonts remain detection-only with a legal fallback. The selector lists all available fonts and the renderer stays resolution-aware.

### 1.2.4 defaults

Intro/Main/Outro Original Audio all default to **Original** (Mute/Low/Original remain, independently settable); subtitle animation default is **Static Phrase** (Long-Form / YouTube Landscape, all 5 animations still selectable); Output Preset **YouTube Landscape** + Quality **Maximum** are unchanged. The one-click workflow now covers Video Pool + Voiceovers + Scripts + Music + Intro + optional Quote + Main + Outro + Subtitles + Watermark → FinalVideo, still handing the actual rendered `MainVideo.mp4` to Stage 2.

## New in 1.2.3

### Flexible video ordering

The project-level **Video Order** selector offers **Natural**, **Alphabetical**, **Random**, and **Manual**. Natural is numeric-aware (`1, 2, 3, 10`), Alphabetical is case-insensitive filename order, and both retain source-folder alternation when alternatives remain. Random performs an unbiased Fisher-Yates permutation of the current active pool and then applies the same folder rule; it is generated before duration selection and is never silently converted into a Manual override. A supplied seeded RNG is supported by the order helper for deterministic tests, while normal exports use a fresh random source. Manual drag-and-drop and the move buttons persist the exact explicit sequence and remain authoritative.

The existing **Randomize Order** button remains an explicit one-time action: it writes the resulting active sequence as Manual for compatibility with older projects. **Reset to Default Order** restores the natural numeric/alphabetical order and switches the selector back to Natural; it never restores a previous random sequence.

### Maximum Quality and YouTube Landscape defaults

The application default is **Maximum Quality**: real `libx264` encoding with CRF 16, preset `slow`, High Profile and yuv420p — not a cosmetic label. The default output preset is **YouTube Landscape**: 16:9, Auto/highest appropriate source resolution, Maximum Quality, Source/Auto FPS, H.264 High, AAC-LC 48 kHz, Fast Start. Source resolution is preserved (4K stays 4K) and source FPS is preserved unless explicitly changed. Both presets are visible in the main GUI (Output Preset & Quality group) and map to real encoder arguments; **Custom** keeps the explicit CRF 14–28 / fast–slow advanced settings.

### Optional Intro

An independent **Intro** can be assigned in Stage 2. The final composition is **Intro → Main → Outro**; each section keeps its own original audio with an independent Mute / Low / **Original** (default) setting. The Intro receives no main voiceover, no main generated background music and no subtitles. The selected transition and duration apply between every adjacent section.

### Multiple voiceover / script files

A dedicated **Voiceover Order** control supports **Natural / Alphabetical**, **Modification Date – oldest first**, **Modification Date – newest first**, and **Manual** (Add / Remove / Move Up / Down / Top / Bottom). The effective order is shown in the table, persists with the project, and Manual preserves the explicit list exactly. Scripts auto-associate by normalized basename (for example `intro.wav` ↔ `intro.txt`) and can be overridden per row.

- **One Global Script** (default): one selected text file is authoritative for the complete ordered voiceover timeline; it is stored once and is never duplicated per row.
- **Individual Scripts**: every voiceover needs its own basename-matched script; a missing script aborts with a clear `SUBTITLE GENERATION FAILED [script matching]` error — never a silent captionless output.

The **Pause Between Voiceovers** is a separate setting with presets `0.0`, `0.25`, `0.5`, `0.7` (default), `1.0`, `1.5`, `2.0` seconds plus Custom. It inserts actual silence between units, so the combined voiceover target and cumulative subtitle timestamps include the gaps. Subtitles break at the silence and never remain visible during it. **Main Video End Padding** remains the separate existing `1.0` second default. In global mode each cached source transcription feeds one global script-mapping operation; individual mode aligns each basename-matched pair and concatenates cumulative timestamps into one canonical SRT/VTT/burn-in timeline. Voiceover is never looped; background music spans the complete Main Video only.

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

Save `VideoMerger_Final_1.4.0.zip` in Downloads and run Windows PowerShell:

```powershell
$Zip = Join-Path $HOME "Downloads\VideoMerger_Final_1.4.0.zip"
$ProjectRoot = Join-Path $HOME "Downloads\VideoMerger_Final_1.4.0"
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
