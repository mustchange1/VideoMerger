# VideoMerger 1.5.0 for Windows

## New in 1.5.0

### Multiple source folders and folder-aware selection

Add any number of configured video folders with **Add Folder**, **Remove Folder**, and **Clear All**. Folders persist in project settings; each clip keeps its resolved source-folder identity. Automatic selection uses deterministic randomized folder alternation, never repeats a folder while another configured folder still has usable clips, and falls back only when no alternative remains. An explicit manual order disables alternation; Required-Only, Hold Last Frame, Full-Timeline Loop, and Smart Last-Clip Stretch keep their existing semantics.

### Independent merge-duration controls

**Duration Before Merge** defaults to `0.70x` and applies to each normal selected visual clip (`timeline_duration = source_duration / 0.70`) before timeline construction. **Duration After Merge** defaults to disabled / `1.00x` and runs as a separate post-merge operation on the complete Stage-1 master. Smart Last-Clip Stretch remains after timeline construction and before rendering; Stage-2 Intro, Add Image, and Outro are not altered by Before Merge.

### Add Image and subtitle output defaults

**Add Image** is an optional, silent Stage-2 section: PNG/JPG/JPEG/WEBP, Before Main Video by default, 4.0 seconds, Fit, 100% zoom, Natural look, and the existing Cross Dissolve transition with a 1.0 second boundary request. The legacy Image Insertion names and position aliases remain accepted. Its complete file identity and settings are included in the independent Stage-2 composition fingerprint.

Subtitle output is explicit and defaults to **With Subtitles** whenever a subtitle source is requested. It burns the aligned ASS timeline and writes SRT/VTT but creates no clean sibling. **With and Without Subtitles** additionally retains the clean variant. **Without Subtitles** skips alignment, burn-in, SRT and VTT generation while preserving voiceover/audio timing. Landscape long-form subtitles default to **Center**; vertical short-form subtitles default to **Bottom Center**. Saved/manual position overrides remain authoritative.

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

The short visual gap after the voiceover is now a free manual setting (0.0–5.0 s). This control is today the **Long-Form Outro (visual after voiceover)** in group `4d · Timeline – Visual Intro / Outro / Opening Effect`; its user-facing default is `2.5 s`, and it remains the *single* visual tail after the spoken audio, so it can never be applied twice. See *Visual intro/outro sections* below.

### Add Image (optional, silent Stage 2)

The dedicated section appears directly below Add Intro. Choose one PNG, JPG, JPEG, or WEBP and place it immediately **Before Main Video** or **After Main Video**. The image uses an editable duration with a 4.0 second default, the shared transition selector with Cross Dissolve as its default, a separately clamped 1.0 second boundary duration, Fit/Fill/Crop sizing, optional zoom, and five deterministic looks: Natural, Cinematic, Moody, Film, and Dark Editorial. The live preview updates for the selected file, aspect, sizing, zoom, and look.

The effective Stage-2 sequence is always `Intro → optional Add Image Before Main → Main Video → optional Add Image After Main → Outro`; with no Intro/Outro the selected image becomes the first/last section. The image section receives no voiceover, music, original audio, or subtitle timing; the graph supplies matching silence and keeps each relevant transition boundary gap-free. One-Click, normal Stage-2 export, preview/basic hand-off, landscape/portrait, 1080p/4K, subtitles, and chunked rendering use the same timeline. Settings are persisted and the complete file identity/content plus all image settings participate in the independent Stage-2 composition fingerprint; Stage-1 remains reusable when only Add Image changes. Legacy Image Insertion CLI/config names remain compatible.

### Cleaner subtitle segmentation + larger preview

Long-Form cues are preferably 1–2 measured lines of natural phrases; one/two-word captions are merged/rebalanced into neighbors (word-level timing untouched). The live preview and the new larger preview dialog paint through the SAME renderer geometry routine (font, size, wrapping, style, position, safe area, colors/highlights, animation staging with a word-progress slider).

### Clean Output directory + flexible subtitle outputs

The Output folder contains only useful user-facing files. The subtitle output mode controls the actual contract: `With Subtitles` writes the primary burned-in video plus SRT/VTT without a clean sibling; `With and Without Subtitles` writes both video variants plus SRT/VTT; `Without Subtitles` writes only the primary clean video. Verification PNGs and the subtitle timeline JSON live under `temp/` (internal evidence/cache) instead of Output.

### Automatic local YouTube title + description (free, local, unlimited)

Every successful one-click final video automatically produces `FinalVideo_16x9_YouTube.txt` (`TITLE:` / `DESCRIPTION:` / `LANGUAGE:`) generated from the authoritative voiceover transcript: strong opening, useful summary in the author's own words, important themes as verbatim key phrases, one natural channel-follow CTA (philosophical/spiritual/modern), German content → German metadata, English → English. The generator is deterministic pure Python (always available offline, nothing invented, no keyword stuffing); an optional locally running Ollama may polish it under strict validation. No OpenAI/Claude/Gemini/paid API, no subscription, no per-video credits. Metadata problems never block rendering and are reported clearly; without an authoritative transcript no metadata is invented.

### One-Click workflow (Video Pool + everything)

`CREATE FINAL VIDEO – ONE CLICK` produces Video Pool + Voiceover(s) + Script(s) + Background Music + Subtitles + Watermark + Intro + optional Add Image + Main Video + Outro = **FinalVideo** in one click; the rendered Main Video flows into Stage 2 automatically (no manual Stage-1→Stage-2 selection). Stage 1 and Stage 2 remain separately usable.

### Current defaults

Intro/Main/Outro Original Audio = Original · Subtitle Animation = Static White Reveal · YouTube Landscape · Maximum Quality · Cross Dissolve = 1.0 s default · Music = 44 % Balanced default (voiceover remains dominant), selected separately for Long-Form and Shorts · End Padding ≈ 1 s (Long-Form; every Short ends with its own fixed 0.7 s video-only ending) · Duration Fit = Cut Last Clip · Maximum Stretch = 10 % · Duration Before Merge = 0.70x · Duration After Merge = disabled / 1.00x. All existing features (transitions, ordering, loops, hold, caching, fonts, 4K, watermark, ducking, multi-voiceover) are unchanged; explicit saved transition and audio values remain authoritative.

# VideoMerger 1.3.0 for Windows

VideoMerger 1.3.0 is an additive local release built directly from the tested 1.2.4 application. Everything below the 1.3.0 section documents the preserved earlier releases. Existing Basic Merge, separate Stage 1/Stage 2, active manual ordering, four transitions, audio modes, watermarking, validation, hardware selection, non-overwriting exports and Windows setup remain available.

## New in 1.2.4

### Large Video Pool — Required-Only processing

The Input Folder is a source library, not a render queue. Discovery uses lightweight `ffprobe` metadata only (duration, resolution, fps, codec, audio presence, size — never a full decode of every file) and caches the result. The project-level **Video Order** selector offers **Natural**, **Alphabetical**, **Random**, and **Manual**. The selected order is resolved before Required-Only duration selection, so the GUI table, pool counts, preview, One-Click, chunked rendering, and final timeline all consume the same effective sequence. Natural uses numeric-aware filenames and the existing source-folder alternation; Alphabetical uses filename order; Random performs a fresh Fisher-Yates permutation and then applies folder alternation without saving it as Manual; Manual preserves the explicitly persisted drag/move sequence. The selection stops as soon as the active order covers the voiceover-derived target duration: only the required clips are rendered. With 300 available and ~14 needed, exactly ~14 clips enter the pipeline and the rest never appear in any decode, filter, transition or encode stage. The final clip is trimmed to fit; if the material is still short, the Full-Timeline Loop repeats the selected A-B-C sequence and Hold Last Frame holds only the final frame. Pre-processing time does not scale with the unused pool size, and changing subtitle style/Add Image/Intro/Outro never re-analyzes the pool. The GUI shows `Videos in Input Folder / Required / Selected / Not Used / Target Duration` and updates after Analyze, voiceover changes, order-mode changes, Randomize and manual reorder.

### Real subtitle preview (Preview ≈ Final Render)

The GUI subtitle preview renders the exact demo cue through the same line-breaking, font-metrics, safe-area and position logic as the burned-in renderer — no fake GUI text, no FFmpeg render. Font, style, animation, position, wrapping, the max-two-line behavior and word highlighting update instantly.

### Four additional fonts

Inter, Manrope, Lora and Roboto (Regular + Bold) join the existing Noto Sans fallback: readable, professional long-form look with German and English support and strong bolds. All fonts are legally redistributable (OFL / Apache-2.0, licenses shipped in `tools/fonts/`); proprietary fonts remain detection-only with a legal fallback. The selector lists all available fonts and the renderer stays resolution-aware.

### 1.2.4 defaults

Intro/Main/Outro Original Audio all default to **Original** (Mute/Low/Original remain, independently settable); subtitle animation default is **Static White Reveal** (Long-Form / YouTube Landscape, all 5 animations still selectable); Output Preset **YouTube Landscape** + Quality **Maximum** are unchanged. The one-click workflow now covers Video Pool + Voiceovers + Scripts + Music + Intro + optional Add Image + Main + Outro + Subtitles + Watermark → FinalVideo, still handing the actual rendered `MainVideo.mp4` to Stage 2.

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

Animations (Long-Form):

- Type Reveal
- Color Change
- Word Highlight
- Phrase Focus
- Static White Reveal

Shorts offer the same list without **Word Highlight** and default to **Phrase Focus** — a calm, conservative phrase-level entrance that stays readable on a 9:16 mobile frame. **Outline Highlight is removed**: it drew a heavy per-word outline colour and produced filled rectangular areas outside the glyphs. Saved projects migrate automatically and deterministically (Outline Highlight → Color Change, a Shorts Word Highlight → Phrase Focus, unknown values → the collection default), and no selectable animation emits outline, shadow, clip or vector-drawing overrides any more — every effect stays glyph-aligned.

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

Save `VideoMerger_Final_1.5.0.zip` in Downloads and run Windows PowerShell:

```powershell
$Zip = Join-Path $HOME "Downloads\VideoMerger_Final_1.5.0.zip"
$ProjectRoot = Join-Path $HOME "Downloads\VideoMerger_Final_1.5.0"
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

## YouTube delivery modes

The export mode is an actual pipeline setting, not a cosmetic aspect toggle:

- **YouTube Long-Form** renders one complete 16:9 landscape timeline. All ordered voiceovers and the global or matched scripts stay on that shared timeline.
- **YouTube Shorts** renders one independent 9:16 Short per available voiceover. A shared without-replacement media pool assigns each Short the next required prefix; clips are reused only after the complete pool is exhausted. Script count does not determine output count. One global script with ten voiceovers therefore still produces ten Shorts.
- **YouTube Long-Form + YouTube Shorts** renders both sets.

Outputs are separated into `Output/LongForm/` and `Output/Shorts/` (`YouTube_LongForm.mp4`, `001.mp4`, `002.mp4`, …). Intro, Outro, Add Image, music, original audio, transitions, ordering and One-Click use the existing render pipeline for every job. Each Short has its own cache identity.

**Separate background music.** Long-Form and Shorts use two independent music selections: **Background Music (Long-Form)** and **Background Music (Shorts)**. The two tracks are strictly separate — the Long-Form track is never mixed into a Short, and a Short whose own track is empty simply has no background music. Volume, preset, ducking, looping and trimming behave exactly as before for whichever track is active, and in the combined mode each output type uses only its own track.

**Video-only Short intro and outro.** Every Short is `[1.5 s visual intro][its own voiceover][1.5 s visual outro]`. The spoken audio stays the authoritative duration and is never extended; intro and outro contain no speech, no voiceover audio and no subtitles — the caption timeline is shifted by the intro inside the timeline model and ends with the voiceover, so no cue from this or any other Short can appear in either section. The material comes from the regular timeline logic (clip selection, transitions, Hold/Loop, chunking and the without-replacement Shorts pool, which reserves intro, spoken part and outro up front). The former fixed `0.7 s` ending is *replaced* by the configurable Short outro, never stacked behind it; `0.7 s` survives only as the guaranteed floor for settings objects that carry no explicit Short outro. The Long-Form keeps its freely configurable **Long-Form Outro** (the former Main Video End Padding).

**One script text file per Short.** Beside every rendered Short, VideoMerger automatically writes `<same name>.txt` (`001.mp4` → `001.txt`) containing exactly the script text that Short uses: its own section of a global script, or its matched/individual script. Nothing is transcribed again — the file reuses the already derived content, so it always matches the spoken/captioned words of that Short and never contains text from another Short. An explicit audio-only Short (a voiceover that speaks no part of the global script) has no text and therefore no sidecar.

Subtitle output is **With Subtitles** by default. It burns the selected profile and writes SRT/VTT without creating an extra clean video. **Without Subtitles** skips subtitle rendering; **With and Without Subtitles** writes both variants. Long-Form and Shorts use separate subtitle profile settings: Long-Form defaults to 16:9 Static White Reveal, while Shorts use a larger Inter mobile profile, a safe Bottom Center position, and word-synchronized animation.

With one global script and several voiceovers, the Long-Form still uses the complete script over the complete timeline while each Short receives only the section its own voiceover speaks — derived acoustically from one shared global mapping, never by aligning the complete script against every Short.

The CLI equivalents are `--export-mode long_form|shorts|long_form_and_shorts`, `--music` (Long-Form), `--short-music` (Shorts), `--subtitle-output-mode with_subtitles|without_subtitles|with_and_without_subtitles`, and `--short-subtitle-style`, `--short-subtitle-animation`, `--short-subtitle-font`, `--short-subtitle-position`.

## Visual intro/outro sections, opening effect and Legacy Input Root priority

Every voiceover-driven Main Video now has an explicit, unambiguous structure:
`[visual intro][voiceover + normal video][visual outro]`. Both visual sections
play moving material from the regular timeline (never black or unintentionally
frozen frames) and contain no voiceover audio; background music may already play
during the intro and stops with the spoken content, exactly like the former end
padding did.

- **Long-Form Intro (visual before voiceover)** — default `2.5 s`, `0` disables it, any positive value is accepted (the GUI spin is capped at `60 s`, the model itself has no artificial limit). The voiceover starts exactly after the intro and the subtitles never start before it.
- **Long-Form Outro (visual after voiceover)** — default `2.5 s`. This *is* the former Main Video End Padding: one control, one canonical timeline tail, so the two can never double. An old project file keeps its saved padding.
- **Short Intro / Short Outro** — default `1.5 s` each, per Short, with the same rules; the Short outro replaces the legacy `0.7 s` ending.
- **Subtitles only while spoken** — the whole caption timeline is shifted by the intro in the timeline model (not by a post-render delay), starts exactly with the voiceover and ends exactly with the spoken audio. Word-level timing, global-script sections, SRT/VTT, burn-in and the strict cue validation are unchanged.
- **Opening Effect (Main Video)** — an optional subtle entrance in group `4d`: `None` (default), `Gentle Zoom In`, `Gentle Zoom Out`. Peak magnification is 5 %, centred, covering the opening portion only (the visual intro, or `3 s` when there is none, never longer than the program), applied to the assembled timeline *before* the subtitle burn-in so captions stay crisp, continuous across chunked rendering, and identity afterwards. It adds and drops no frame, so the voiceover-driven duration, sync and captions are untouched. Shorts never use it. There is no animation editor by design.
- **Legacy Input Root priority (Random order only)** — while **Random** is active, the first three clips of the effective sequence are always drawn from the Legacy Input Root (the configured input folder): distinct where possible, shuffled among themselves, and reserved *before* the rest of the sequence is built. Clip 4 onwards keeps the unchanged full random pool (folder-aware alternation, duplicate/exhaustion rules, seeded determinism). Fewer than three eligible clips reserve what exists and then fill normally; an empty or missing root changes nothing at all — not even the random sequence, so existing projects stay bit-identical. Natural, Alphabetical and Manual are not affected. One log line names the reserved clips.

GUI: group **`4d · Timeline – Visual Intro / Outro / Opening Effect`** holds the four duration spins and the opening-effect selection; the subtitle animation combos are built per collection (Long-Form / Shorts) with their own defaults, and switching to 9:16 selects the Shorts default automatically. All new values persist in the project file, survive old project files (missing fields fall back to the documented defaults, deprecated animations migrate, unknown fields are ignored) and are part of the render-cache identity (fingerprint schema `3`), so changing any of them re-renders instead of reusing a stale cache entry.

CLI: `--long-intro`, `--long-outro` (alias of `--pause`/`--end-padding`), `--short-intro`, `--short-outro` and `--opening-effect none|zoom_in|zoom_out`.
