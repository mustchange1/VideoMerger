# BUILD REPORT – VideoMerger 1.5.0

Date: 2026-09-01
Basis: current Arena branch checkpoint, built additively on the tested 1.3.0 implementation
Target: `VideoMerger_Final_1.5.0.zip`

## 1.5.0 finalization note

This checkpoint adds no new feature beyond the implemented workflow enhancement: multi-folder source management and folder-aware selection, independent Before/After Merge controls, and the updated Flyer/subtitle defaults. The exact available validation results are reported in the final delivery summary; this sandbox has no FFmpeg/FFprobe binary and Qt cannot load because `libGL.so.1` is unavailable, so those real-render/GUI checks are not claimed as executed here.

## Implemented release changes (spec → implementation)

1. **Windows subtitle filtergraph fix (root cause)**
   FFmpeg now always runs with `cwd` = project root (`engine.export` → `_execute(working_directory)`). Every render-time file the filtergraph references is app-staged under that root with ASCII names: the burned-in ASS under `temp/` and the legal fonts under `tools/fonts`; Quote/Flyer artwork is passed as an ordinary absolute media input. The `subtitles=filename=`, `fontsdir=` and `fontfile=` values therefore become plain relative POSIX paths — on ANY Windows machine, whatever the unpack location (`C:\Users\Jürgen Müller\Downloads\VideoMerger_Final_1.3.0` works). No drive-letter colon, backslash, space or non-ASCII byte can appear in the value, making it immune to both filtergraph parser passes, the C-runtime/libass `fopen` and the Windows code page. Paths that cannot be made relative are emitted UNQUOTED with forward slashes and the verified two-level escape table: unlike the 1.2.4 quoted form, this represents apostrophes (`C:/Users/O'Brien/…` previously raised ValueError and aborted the render) and never produces a broken quoted span. Windows drive/UNC paths are normalized as pure strings (never resolved against a POSIX cwd). Real libass burn regression tests cover umlaut/space/apostrophe absolute paths, a non-ASCII working directory with a relative value, and the engine burn pass with the real fonts dir — at all subtitle fonts/animations/positions, 16:9 + 9:16, 1080p + 4K (geometry tests).
2. **Smart Last-Clip Stretch (Duration Fit Mode)** — `cut` (default, byte-identical proven 1.2.4 behavior) or `stretch`: the shortest ordered prefix is taken one clip shorter and ONLY its final occurrence is slowed (`setpts` + clip-audio `atempo`) so the complete source content fills the voiceover target exactly. Configurable maximum stretch 5/10 (default)/15/20/Custom %. Transitions, order and visual continuity preserved; beyond the limit it falls back to the normal last-clip trimming — never to Hold Last Frame. Mirrored in pure O(n) duration math by `video_pool` so GUI status and render agree.
3. **Global Video Speed** — 0.50x–2.00x, default 1.00x. Voiceover stays the timing authority: target duration, subtitle timeline, voiceover and music behavior unchanged (e2e proves byte-identical SRT at 1.00x vs 1.50x); clip playback rate and required-clip selection adapt. Clip-own audio follows the rate for internal A/V sync.
4. **Main Video End Padding (manual)** — free 0.0–5.0 s spin box; the existing ~1 s default preserved exactly (pinned by tests in models + GUI).
5. **Large Video Pool — safe optimization only** — no redesign. `compute_pool_status` now derives required/selected from ONE prefix pass (previously two full transition computations); `required_selection_length` reuses the already computed prefix list; stretch/speed parameters flow through the same single pass. Pinned by tests: exactly one prefix computation per status, unchanged selection numbers for 120–200-file pools, input order never re-sorted, repeated status updates never spawn an FFprobe process, media metadata cache remains stat-keyed.
6. **Subtitle quality (Long-Form YouTube)** — cues preferably 1–2 measured lines of natural phrases; one/two-word groups are merged into the better-fitting neighbor or rebalanced (one word moved) within the measured two-line geometry and word budget; word-level timing never changes (cue starts are exactly the first word's acoustic start, no overlap, no leading/lagging, nothing reaches into the quiet pause). Static Phrase remains the default animation; safe areas and all ten presets unchanged.
7. **Subtitle Preview** — the live canvas and the reworked larger dialog paint through ONE shared routine (`paint_subtitle_layout`) using the exact renderer geometry (same `preview_cue` layout: font, size, wrapping, safe area, position, colors/highlights) and stage the animation with a word-progress slider.
8. **Quote/Flyer artwork (optional, silent)** — Stage 2 accepts only an uploaded PDF, PNG, JPG, JPEG or WEBP. The GUI exposes Include Quote / Flyer, artwork chooser, one-based PDF page, Fit/Fill/Crop and duration (default 4.0 s); there is no text Quote or generated-text mode. Raster images are looped as a real image input, PDFs are rasterized with PyMuPDF into render-only temporary files and cleaned up. Fit/Fill/Crop preserve aspect ratio at 16:9/9:16 and 1080p/4K. The visual sequence is Intro → transition → Quote/Flyer → transition → Main → transition → Outro, with an explicit silent audio source so voiceover, music, subtitles and Main audio cannot enter the artwork section. One-Click passes the artwork directly to Stage 2 and Quote-only changes are excluded from the Stage-1 cache fingerprint.

9. **One-Click complete workflow** — one click produces Video Pool + Voiceover(s) + Script(s) + Background Music + Subtitles + Watermark + Intro + optional Quote + Main Video + Outro = FinalVideo; the rendered Main Video automatically flows into Stage 2 (no manual Stage-1→Stage-2 selection). Stage 1/Stage 2 remain separately usable.
10. **Outputs** — explicit Main Video render produces `MainVideo_16x9.mp4` + `MainVideo_16x9_no_subtitles.mp4` (+ SRT/VTT); one-click's primary output is the final video `FinalVideo_16x9.mp4` plus `FinalVideo_16x9_no_subtitles.mp4`. The subtitled version always remains the primary. Implementation: clean master render first, then a dedicated libass burn pass (audio stream-copied, same encoder arguments and color tags) — both files share one timeline and one bundle index.
11. **Clean Output directory** — Output contains only user-facing files (final/main MP4s, `_no_subtitles` variants, SRT, VTT, `FinalVideo_16x9_YouTube.txt`). Verification PNGs and the subtitle timeline JSON live under `temp/` (internal evidence/cache). Asserted exactly by an e2e directory-listing test.
12. **Automatic YouTube title + description** — `FinalVideo_16x9_YouTube.txt` (TITLE/DESCRIPTION/LANGUAGE) generated from the authoritative voiceover transcript whenever a successful final video exists: strong opening (the transcript's own first thought), a useful summary (salient verbatim sentences), important themes (verbatim key phrases), one natural channel-follow CTA for philosophical/spiritual/modern insights; German → German, English → English (auto-detected or explicit). No invented facts, no keyword stuffing — extraction-only.
13. **Local / free / unlimited metadata generation** — the generator is deterministic pure Python (always available offline, unlimited use). Optional polish from a locally running Ollama (`127.0.0.1:11434` only) under strict validation; any problem falls back to the deterministic draft. No OpenAI/Claude/Gemini/paid API, no subscription, no per-video credits, no API keys anywhere (test-enforced). Metadata failures never block video rendering and are reported clearly; without an authoritative transcript nothing is written (never invented).
14. **1.5.0 defaults** — Intro/Main/Outro Original Audio = Original; Subtitle Animation = Static Phrase; YouTube Landscape + Maximum Quality; End Padding = 1.0 s; Quote/Flyer disabled unless enabled, duration 4.0 s, Fit; Duration Fit = Cut Last Clip; Maximum Stretch = 10 %; Duration Before Merge = 0.70x; Duration After Merge = disabled / 1.00x; landscape subtitle position = Center; portrait short-form = Bottom Center. Pinned by the updated defaults and focused workflow tests.
15. **No regression** — Basic Merge, Stage 1/Stage 2, all four transitions, manual/natural/random ordering, large pools, Full-Timeline Loop, Hold Last Frame, Intro/Main/Quote/Outro, multiple voiceovers/scripts, music loop/trim/ducking, Mute/Low/Original, watermark, SRT/VTT, burned subtitles, preview, all 7 fonts, 4K/16:9/9:16, caching, Maximum Quality, one-click: all 232 baseline tests still pass unmodified except three intentional expectation updates (free quote duration error message, one-click 3-phase progress labels, end-padding spin box instead of the fixed combo — each updated to the new documented behavior with equivalent or stronger assertions).

## Executed environment

- Linux development sandbox, Python 3.11.2, PySide6 6.11.2 (offscreen; headless GL/xkb/dbus stubs)
- FFmpeg/FFprobe n8.0-23-gd1f31a829d (BtbN-class GPL build with drawtext + libass, obtained from the `ffmpeg8-binaries`/`ffprobe8-binaries` wheels because the sandbox blocks release-assets.githubusercontent.com)
- Linux results are NOT represented as native Windows execution; the Windows-specific logic is covered by cross-platform path-strategy tests plus real libass burns over Windows-style hostile paths.

## Executed source-tree tests (final run before packaging)

```text
327 passed, 4 skipped (opt-in gates) — 0 failed
```

The 4 skips are the pre-existing opt-in gates (`VIDEOMERGER_RUN_2MIN_BENCHMARK=1`; `VIDEOMERGER_TEST_REAL_ALIGNMENT=1` ×3). New 1.3.0 suites: `test_130_windows_subtitle_paths.py` (9), `test_130_stretch_speed_padding.py` (12), `test_phase3_quote_artwork.py` (artwork/PDF/fit/silence/cache coverage), `test_130_outputs_and_metadata.py` (12), `test_130_subtitle_quality.py` (16), `test_130_defaults.py` (8), `test_130_pool_optimization.py` (5).

Evidence: `test_evidence/1.3.0/` (full suite output, evidence summary, manifest).

## Packaging / delivery verification (this build)

Verification order: build ZIP from the clean tree (excluding `.git`, `dev/`, caches, local settings and temp artifacts) → SHA-256 → clean extraction to a fresh directory → full test suite run against the EXACT extracted tree → identical SHA-256 re-check → artifact attached. Results are recorded in `test_evidence/1.3.0/` and `ARTIFACT_IDENTITY.txt`.

## 1.5.0 YouTube delivery phase

Implemented and wired through model, SettingsStore, GUI, CLI, Stage 1/Stage 2 and One-Click:

- YouTube Long-Form, YouTube Shorts, and combined delivery modes;
- one independent vertical Short per ordered voiceover, including 10 voiceovers + one global script → 10 Shorts;
- separate `LongForm/` and `Shorts/` output bundles and per-Short cache identities;
- distinct Long-Form and mobile-safe Shorts subtitle profiles;
- `With Subtitles`, `Without Subtitles`, and `With and Without Subtitles`, with the new default avoiding an extra clean copy;
- focused dependency-light planning tests in `tests/test_phase17_youtube_outputs.py`.

Validation in this Linux workspace: Python compilation/import smoke tests and the focused phase-17 functions were run manually with a minimal temporary pytest stub. The `pytest` executable, PySide6, FFmpeg and FFprobe were unavailable, so no claim is made for the unavailable GUI, pytest or real FFmpeg rendering suites.
