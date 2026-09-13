# VM Automatic – Baseline Verification

Date: 2026-09-13 (UTC)
Verified in: fresh checkout of `mustchange1/VideoMerger`

## Base commit

Task instruction referenced baseline hash `bb5470d841ec7d5b159e0303c6610ac6dc0a6283`.
That object does not exist in this repository's history: the repository contains
exactly ONE commit, the squashed merge of PR #1:

    1db2e915fbfc9bdf96f5e49aec5bb5d480966f8a
    "Merge pull request #1 from mustchange1/arena/01a03e9f-videomerger"
    "VideoMerger 1.3.0 — additive release on the tested 1.2.4 baseline"

This single commit IS the complete 1.3.0 baseline described in the task
(Long-Form/Shorts, separate music/transitions/subtitle presets, multi-track
music, subtitle language/presets/font sizes/previews/position preview,
Duration Before Merge, debug overlay, alignment warning, folder/random/manual
clip selection, Full-Timeline Loop, Hold Last Frame, clip continuity
protection, voiceover, background music, original audio, ducking, watermark,
intro, outro, image insertion, output presets, quality settings,
render/cache pipeline, project persistence, diagnostics, Windows scripts).
Verification was therefore performed on `1db2e915` (= the only available
baseline, a strict superset requirement holds against it).

Working branch for this session: `arena/01a09941-videomerger` (session-pinned
by the Arena environment; branched from the baseline commit). `main` is
untouched. No history was rewritten, rebased or force-pushed.

## Test environment

- Python 3.11.2, venv with `requirements-dev.txt` (pytest, ruff, PySide6,
  faster-whisper, fonttools) plus `watchdog` (new VM Automatic dependency).
- FFmpeg: sandbox-local static build (FFmpeg 7.0.2) under the gitignored
  `tools/ffmpeg/bin/`; on real user machines `setup_windows.ps1` provides the
  genuine FFmpeg/FFprobe (unchanged convention).
- GUI tests run with `QT_QPA_PLATFORM=offscreen`.

## Baseline test result (pre-VM-Automatic, commit 1db2e915)

```
2 failed, 319 passed, 15 skipped in 83.57s
```

Full summary: `/tmp/baseline_final.txt` (kept outside the repo on purpose).

### Skipped (all opt-in / environment-gated, by design of the suite)

- `test_121_benchmark_2min.py` – needs `VIDEOMERGER_RUN_2MIN_BENCHMARK=1`
- `test_121_subtitle_workflow.py`, `test_alignment_subtitles.py` (2) – need
  `VIDEOMERGER_TEST_REAL_ALIGNMENT=1` + downloaded local model
- `test_124_quote.py` (2), `test_130_quote_styles.py` (5),
  `test_130_windows_subtitle_paths.py` (2) – need a drawtext-capable FFmpeg
  build at `/tmp/ffdev-dt/bin` (special build not present in sandbox)

### Failed – PRE-EXISTING in the baseline commit (not caused by VM Automatic)

1. `test_windows_setup_assets.py::test_setup_selftest_includes_real_subtitle_workflow_fixture`
   Expects binary evidence files
   `test_evidence/1.2.2/subtitle_workflow/assets/{KnownVoiceover.wav,script.txt,background.mp4}`.
   Those binaries are gitignored (`test_evidence/**/*.wav`, `*.mp4`) and are
   absent from this checkout. Repository-state issue, present since the
   baseline commit.
2. `test_windows_setup_assets.py::test_windows_powershell_scripts_use_utf8_bom_crlf_and_intact_german`
   Expects CRLF line endings in the `.ps1` files; the committed blobs of this
   repository contain LF-only endings (`git show HEAD:setup_windows.ps1 | grep -c $'\r'` → 0).
   Line endings were lost when the repository history was squashed into the
   single baseline commit. Repository-state issue, present since the baseline
   commit.

## Acceptance criterion for VM Automatic

Any NEW failure introduced by VM Automatic is a blocker. Parity target:
`319 passed, 2 failed, 15 skipped` or better.
