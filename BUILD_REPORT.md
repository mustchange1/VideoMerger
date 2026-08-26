# BUILD REPORT – VideoMerger 1.2.4

Date: 2026-08-26  
Basis: exact tested `VideoMerger_Final_1.2.3.zip` source tree, changed additively  
Target: `VideoMerger_Final_1.2.4.zip`

## Implemented release changes

- **Large Video Pool (Required-Only processing)**: the Input Folder is a source library, not a render queue. Lightweight `ffprobe` metadata discovery (filename, path, duration, resolution, fps, codec, audio presence, size — never a full decode of all files) with a persistent metadata cache. Selection stops as soon as the **current active order** (Natural / Manual / Randomized — the order is authoritative) covers the voiceover-derived target duration; only the required prefix is rendered. With 300 available and ~14 needed, exactly 14–16 clips enter the pipeline and the remaining ~286 never appear in any decode, filter, transition or encode stage. The final clip is trimmed to fit the target; if the material is still short, the Full-Timeline Loop repeats the selected A-B-C sequence (not just the final clip, not the whole pool) and Hold Last Frame holds only the final frame as needed. Pre-processing time does not scale with the unused pool size (measured: 20 vs. 500 pool, same selected count ≈ same pre-processing). GUI status row shows `Videos in Input Folder / Required / Selected / Not Used / Target Duration` and updates after Analyze, voiceover changes, Randomize and manual reorder.
- **No pool pre-rendering**: pool clips are processed directly by the final atomic `-filter_complex` command; no intermediate clip files are produced for the pool.
- **Cache discipline**: changing subtitle style/font/animation/quote text/duration/Intro/Outro does NOT re-analyze the pool; order-only changes do not reprocess unused clips.
- **Quote Card (new optional section)**: Stage 2 composes Intro → (transition) → Quote → (transition) → Main → (transition) → Outro. `[ ] Add Quote` checkbox; large multiline text field (German/English/umlauts/punctuation); duration default 2.0 s with options 1.0/1.5/2.0/2.5/3.0 s. Cinematic/editorial design: dark neutral default background with subtle vignette, quote as single focal point slightly above mathematical center, automatic balanced line breaks at word/phrase boundaries (never a broken word, no lone-word lines), resolution-aware font size rendered at native resolution (1920×1080 / 2560×1440 / 3840×2160 / 1080×1920 / 2160×3840). Uses the existing transition system (no separate transition type). **Quote Card Audio = Silent**: no voiceover, no generated music, no subtitles — the quote never enters the SRT/VTT/burn-in timeline and does not affect Main Video subtitle timing (verified acoustically: ≤ −60 dB in the pure quote window of a real render). Live GUI quote preview reuses the same layout/line-break/font-metrics logic as the renderer. The architecture is extensible to custom backgrounds.
- **Real subtitle preview (Preview ≈ Final Render)**: the GUI subtitle preview is now a canvas that renders the exact demo cue through the **same** line-breaking, font-metrics, safe-area and position logic as the burned-in renderer (no fake GUI text, no FFmpeg render). Font, style, animation, position, wrapping, max-two-line behavior, word highlighting and realistic scaling/safe margins update instantly on any control change.
- **Four additional fonts**: Inter, Manrope, Lora and Roboto (Regular + Bold each) — readable, professional long-form look with German + English coverage and strong bolds — added to the existing Noto Sans fallback. All five animation types and the complete ten-preset style system remain unchanged. Bundled fonts carry valid redistribution licenses (OFL / Apache-2.0, shipped in `tools/fonts/`); proprietary fonts (e.g. Eveleth) remain detection-only with legal redistributable fallback. The font selector lists all available fonts; the renderer stays resolution-aware.
- **1.2.4 defaults**: Intro/Main/Outro Original Audio all default to **Original** (Mute/Low/Original remain, independently settable); subtitle animation default **Static Phrase** for Long-Form / YouTube Landscape (all 5 animations remain selectable); Output Preset **YouTube Landscape** + Quality **Maximum** unchanged.
- **One-click complete workflow extended**: Video Pool + Voiceovers + Scripts + Music + Intro + optional Quote + Main + Outro + Subtitles + Watermark → FinalVideo. The actual rendered `MainVideo.mp4` is still physically handed to Stage 2.
- Everything from 1.2.3/1.2.2 remains: Basic Merge, separate Stage 1/Stage 2, natural/manual/random ordering, four transitions and defaults, audio modes, ten subtitle presets with five animations (max two lines), watermark, Hold Last Frame / Full-Timeline Loop, SRT/VTT, validation, FFmpeg/FFprobe handling, encoders with CPU fallback and non-overwriting output behavior.

## Executed environment

- Linux development sandbox, Python 3.13, PySide6 6.11.2 (offscreen)
- FFmpeg/FFprobe 7.0.2-static (johnvansickle) via `VIDEOMERGER_FFMPEG_DIR` for the suite; BtbN N-126264 (with drawtext) for the quote-card e2e renders
- PowerShell parser checks on Linux (not Windows PowerShell 5.1)
- Linux/offscreen results are **not** represented as native Windows execution.

## Executed source-tree tests

```text
232 passed, 4 skipped in 88.74s
```

The 4 skipped tests are opt-in (`VIDEOMERGER_RUN_2MIN_BENCHMARK=1` benchmark; `VIDEOMERGER_TEST_REAL_ALIGNMENT=1` real-acoustic e2e, 3 tests). New 1.2.4 suites executed: `tests/test_124_defaults_fonts_preview.py` **17 passed** (0.32 s), `tests/test_124_video_pool.py` **15 passed** (1.51 s, real ffprobe discovery on 10/100/300/500 pools), `tests/test_124_quote.py` **28 passed** (38.38 s, real FFmpeg renders incl. 16:9 + 9:16 e2e with acoustic silence probes and subtitle-timeline checks). Three legacy default-pins (1.2.2/1.2.3/GUI) were updated to the intentional 1.2.4 defaults (Static Phrase animation, 7 fonts, Intro audio Original) — every remaining baseline test passes unchanged.

Evidence:

- `test_evidence/1.2.4/full_source_tests.txt`
- `test_evidence/1.2.4/defaults_fonts_preview_tests.txt`
- `test_evidence/1.2.4/video_pool_tests.txt`
- `test_evidence/1.2.4/quote_card_tests.txt`
- `test_evidence/1.2.4/EVIDENCE_REPORT.html`
- `test_evidence/1.2.4/evidence_summary.json`
- `test_evidence/1.2.4/EVIDENCE_MANIFEST_SHA256.txt`

## Performance

The 1.2.1–1.2.3 structural performance fixes (timeline-gated blur and ramp blend, one atomic `-filter_complex`, one final lossy encode, cached ASR/alignment/probes) remain fully in place. New 1.2.4 guarantee: pre-processing/selection time does **not** scale with the unused pool size — measured by `test_preprocessing_time_does_not_scale_with_unused_pool` (20 vs. 500 pool, identical selected count). The full two-minute cold-ASR benchmark is opt-in for 1.2.4 (1.2.2 reference: 115.559 s analysis-to-final).

## Required status matrix

`PASS` means the stated behavior was actually executed on this host. `NOT EXECUTED` identifies platform/hardware checks not run here.

### Large Video Pool

| Item | Status |
|---|---|
| 10 / 100 / 300 / 500-file pools handled efficiently | PASS – pool-size status tests |
| Lightweight ffprobe metadata discovery (no full decode) | PASS – discovery test with real ffprobe on 100 files |
| Metadata cached across changes | PASS – cache test |
| Required-only selection stops at covered VO duration | PASS – 300 pool → 14–16 selected |
| Unused clips absent from final FFmpeg command (no decode/filter/transition/encode) | PASS – command-exclusion assertion |
| Active order (Natural / Manual / Randomized) authoritative | PASS – per-order selection tests |
| Randomize + Reset + manual reorder with immediate recalculation | PASS |
| Final clip trimmed to fit target | PASS |
| Still short → Full-Timeline Loop of selected A-B-C / Hold Last Frame | PASS – fallback tests |
| No voiceover → full active order rendered | PASS |
| Pre-processing time independent of unused pool size | PASS – 20 vs. 500 measurement |
| No pool pre-rendering (direct `-filter_complex` processing) | PASS – no intermediate clip files |
| GUI status row (Folder/Required/Selected/Not Used/Target) | PASS – status update tests |

### Subtitle preview, fonts, defaults

| Item | Status |
|---|---|
| Preview reuses renderer line-breaking/metrics/safe-area/position logic | PASS – identity assertions (Preview ≈ Final Render) |
| Instant updates on font/style/animation/position/size/color | PASS |
| Word highlighting + max two lines in preview | PASS |
| Four additional fonts (Inter/Manrope/Lora/Roboto, Reg+Bold) present, selectable, licensed | PASS – font inventory + selection |
| Font selector lists all available fonts | PASS |
| All 5 animations + full ten-preset style system kept | PASS |
| Static Phrase default (16:9 / YouTube Landscape) in settings and GUI | PASS |
| Intro/Main/Outro Original Audio default = Original (Mute/Low/Original remain) | PASS |
| YouTube Landscape + Maximum defaults kept | PASS |

### Quote Card

| Item | Status |
|---|---|
| Quote disabled → chain unchanged (regression) | PASS |
| Intro → Quote → Main (+Outro) real render | PASS – e2e (5.05 s, correct section durations/transitions) |
| 16:9 and 9:16 real renders | PASS – e2e |
| 1080p / 1440p / 4K native-resolution layouts | PASS |
| Duration options 1.0/1.5/2.0/2.5/3.0, default 2.0 | PASS |
| German/English/umlauts/punctuation (line break + escaping) | PASS – umlaut e2e asserts burned-in text |
| Automatic line breaks (word/phrase boundaries, no broken words, no lone words) | PASS |
| Live GUI quote preview (text/font/lines/bg/duration/format) | PASS |
| Uses existing transition system | PASS |
| Quote silent: ≤ −60 dB in pure quote window (measured −91.0 dB) | PASS – volumedetect on real render |
| No subtitles / VO / music on quote; Main subtitle timing unaffected | PASS – drawtext-window + main-loud probe |
| Extensible background architecture (default dark neutral + vignette) | PASS – layout unit tests |

### One click and regression

| Item | Status |
|---|---|
| One-click complete workflow (Pool + VO + Scripts + Music + Intro + Quote + Main + Outro + Subs + Watermark → FinalVideo) | PASS – regression e2e (real MainVideo handoff to Stage 2) |
| All 1.2.3/1.2.2 features (transitions, ordering, multi-VO, SRT/VTT, styles/animations, watermark, Hold Last Frame, Full-Timeline Loop, validation, FFmpeg/FFprobe, PowerShell assets) | PASS – full suite 232/0 |
| Subtitle/style/quote changes do not re-analyze pool | PASS – cache tests |
| Native Windows GUI/setup/export | NOT EXECUTED (Linux sandbox) |
| Windows PowerShell 5.1 | NOT EXECUTED (UTF-8 BOM/CRLF + German integrity machine-checked) |
| Actual NVENC/QSV/AMF hardware encoding | NOT EXECUTED (CPU fallback by design) |
| Full two-minute cold-ASR benchmark | NOT EXECUTED for 1.2.4 (opt-in; 1.2.2 reference 115.559 s) |

## Packaging and exact artifact

The direct-root ZIP excludes `.venv`, downloaded models, downloaded FFmpeg binaries, caches, logs, user config and generated runtime outputs. It includes source, tests, Windows scripts, documentation, legal fonts/licenses and 1.2.4 release evidence. The final ZIP checksum and the exact clean-extraction suite result are recorded externally in `VideoMerger_Final_1.2.4_DELIVERY_VERIFICATION.txt` and the delivery response because an archive cannot self-reference its own final hash.
