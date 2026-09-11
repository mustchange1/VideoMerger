# VideoMerger — Phase 27 Delivery Report

**Branch:** `arena/01a091fe-videomerger`
**Final SHA:** `5dca15fd593f1892a2c19d8d62ec3ced748a8789`
**Base:** `c09ba40cc0631463a8506df7ca601b8395e40c5c` (`Optimize Shorts clip pools and subtitle alignment`)
**Push status:** pushed to `origin/arena/01a091fe-videomerger` (new branch).

All work is additive. The highest-priority regression contract — *"with every
setting left at its default, Long-Form and Shorts renders are identical to the
pre-Phase-27 output"* — is enforced by tests that compare against the exact
historical code paths (legacy single-track music graph, 100 % font size,
debug overlay OFF, single Duration-Before-Merge value).

---

## 1. Multiple background music tracks

- New module `app/video_merger/music_tracks.py`: canonical ordered track list
  (`{"path","trim_start","trim_duration"}`), clamped per-track trims,
  `effective_music_tracks()` migrates a legacy single `music_path` to a
  one-item sequence, `music_track_paths()` resolves files.
- `models.py`: `music_tracks: list[dict]` (persisted user sequence) and
  `music_track_plan: list[dict]` (render-time plan filled by
  `MainProjectEngine`, carrying probed durations).
- `main_project.py`: probes every track, rejects a `trim_start` at/after EOF
  with a clear error, computes each track's effective duration, builds
  `music_track_plan`, and passes it to the fingerprint and settings.
- `command_builder.py`: `_music_sequence_plan()` decides the graph. A single
  track **without trim** keeps the byte-identical legacy `-stream_loop -1`
  single-input graph. Two-or-more tracks (or any trim) switch to an explicit
  sequence graph: one real input per track → `atrim` → `concat` → the whole
  sequence is `asplit`/`concat`-repeated as ONE unit until the window is
  covered (**A → B → C → A → B → C …**). A single track is never looped on its
  own while other tracks exist. Global volume/preset/ducking apply after the
  sequence is assembled, unchanged.
- Verified end-to-end with real FFmpeg: a three-track render audibly plays
  `A(220 Hz) → B(587 Hz) → C(988 Hz) → A → B → C …` through the program.

### Cache rules (music)
- `render_cache.build_stage1_payload` gains an optional `music_track_plan`.
  A legacy single-track default adds **no** `music_tracks` key, so every
  existing Stage-1 cache stays valid (byte-identical digest verified). Any
  multi-track or trimmed sequence adds the ordered sequence identity and
  invalidates the render stage only. Music never appears in ASR/alignment
  cache keys (`alignment.py` keys are audio+model+language only).

## 2. Subtitles — Long-Form vs Shorts separation, independent font sizes

- `subtitles.py`: `_font_size(..., font_size_percent)` scales the preset's
  resolution-aware base size; `clamp_font_size_percent()` bounds 50–200 %.
  `build_cues()` and `write_ass()` thread the percent through the SAME
  measured wrapping/geometry used by the burn-in renderer. 100 % is
  byte-identical to the historical output.
- `models.py`: `subtitle_font_size` (Long-Form) and
  `short_subtitle_font_size` (Shorts), both default `100`.
- `youtube_outputs.py`: `short_settings()` maps the Shorts size and the
  Shorts Duration-Before-Merge onto the job's canonical generic fields;
  `long_form_settings()` pins the Long-Form values. One profile never leaks
  into the other.
- GUI: the Subtitle panel now shows two clearly separated groups
  (**YouTube Long-Form Subtitles** / **YouTube Shorts Subtitles**), each with
  Style / Animation / Font / **Font Size** / Position and its own live preview
  canvas. Font-size and position changes of one profile never move the other
  preview (asserted in tests).

## 3. Real per-profile subtitle previews

- `subtitle_preview.py`: `preview_cue(..., font_size_percent)` and the
  `SubtitlePreviewCanvas` drive the SAME `subtitles._font_size` /
  `subtitles._position` routines as the renderer (Preview ≈ Final Render).
  Added optional `set_background_image()` for the **Include Image** toggle.
- GUI: Long-Form preview is fixed 1920×1080, Shorts preview fixed 1080×1920;
  Top/Center/Bottom move the caption live; font size changes wrapping live;
  Include Image paints behind the captions. No FFmpeg is started.

## 4. Per-profile Duration Before Merge

- `models.py`: `duration_before_merge_shorts` (default `0.70`).
- Semantics are preserved EXACTLY: it remains a playback-rate multiplier
  (`setpts=PTS/x`), never a time value. GUI exposes two independent
  `0.25x–2.00x` combos. Legacy projects keep the single saved value for both
  profiles until the Shorts value is changed (`short_settings()` falls back to
  the long value when the Shorts field is unset).

## 5. Subtitle debug overlay — root cause and fix

- **Root cause:** the ASS writer emitted a `Style: Debug` line unconditionally
  (harmless but present), and the ON/OFF toggle was persisted like any other
  setting, so a project left with it ON silently burned the diagnostic layer
  into production output.
- **Fix:** `write_ass()` emits the Debug style **only** when
  `debug_overlay=True`. With the production default OFF there is no debug
  style, layer or text in the output (regression-tested). `main_project.py`
  logs an explicit warning when a render starts with the overlay ON. The
  GUI default is OFF and the tooltip states it is diagnostic-only.

## 6. Continue After Alignment Warning — fail-closed safety

- With the override **OFF** (default) any alignment warning now stops subtitle
  generation with an explicit, actionable error (fail-closed). With it **ON**
  the user has confirmed continuation and the render proceeds unchanged. The
  flag is a manual safety confirmation, never a repair. Improved GUI tooltip;
  covered by regression tests.

## 7. Consecutive duplicate clip bug — root cause and fix

- **Root cause 1 (chunked rendering):** `plan_segments` computed the chunk
  boundary from the *raw* clip durations and ignored transition overlap, so a
  boundary could fall inside a transition and the next segment re-rendered
  (replayed) content already covered, producing an A→A-style seam.
- **Fix:** `_boundary_before_clip` is now `sum(dur[:K]) − sum(trans[:K-1])` for
  both the transition and no-transition branches. Numerical simulation and a
  new 60-config seeded test prove the segments form an exact partition of
  `[0, total)` with no replay and no gap.
- **Defense in depth:** `timeline.enforce_continuous_sources()` guarantees no
  two consecutive occurrences share a source: it COALECES an unnecessary
  same-source split, REPLACES a remaining repeat with a different pool clip
  (exact duration preserved, never matching either neighbor, never disturbing
  an explicit stretch), and only when no alternative exists keeps the repeat
  with a warning. Manual order, random order, Hold, Full-Timeline Loop and
  loop boundaries (`… C → A`) are unchanged. Covered by
  `tests/test_clip_continuity.py` (14 tests).

## 8. Persistence & migration

- `SettingsStore` persists every new field through the dataclass; legacy
  project JSON without the new fields loads the documented safe defaults
  (music → one-item sequence, font sizes → 100 %, Shorts multiplier → 0.70).
  Round-trip and legacy-migration tests cover music, both font sizes and both
  Duration-Before-Merge values.

## 9. Tests added / changed

| File | Scope | Count |
|---|---|---|
| `tests/test_phase27_music.py` | multi-track graph, order, loop, trim, persistence, migration, cache identity | 15 |
| `tests/test_phase27_subtitles.py` | font-size scaling/wrapping/timing, debug OFF/ON, per-profile multipliers, ASR isolation, fail-closed gate | 20 |
| `tests/test_phase27_gui.py` | separate groups, independent previews, geometry, position, Include Image, duration controls, music list UI | 12 |
| `tests/test_clip_continuity.py` | coalesce/replace/fallback/loop-boundary + full `fit_media_to_duration` pipeline | 14 |
| `tests/conftest.py` | autouse isolated `config/settings.json` per test (fixes cross-test pollution) | — |
| `tests/test_phase4_multi_voiceover.py` | corrected stale expectation to the fail-closed "retain every script word" contract | 1 |
| `tests/test_chunked_rendering.py` | updated planner expectations + exact-partition regression | 13 pass |

### Results
- **Full non-E2E/non-benchmark suite: 384 passed, 2 skipped, 83 deselected.**
  (Baseline at `c09ba40` was 317 passed / 2 failed / 3 skipped; the two
  failures were pre-existing and are fixed here — no regressions.)
- **E2E suite (`-m e2e`) with real FFmpeg: 59 passed, 18 failed, 6 skipped —
  the 18 failures are byte-identical to baseline `c09ba40`** (environmental:
  drawtext build absent, static-build pixel/audio-timing tolerances), i.e.
  Phase 27 introduces zero E2E regressions. Targeted real-render scenarios for
  the new features (multi-track loop, order, legacy single track, 5-clip
  no-duplicate timeline) all pass.

## 10. Limitations / known pre-existing behavior (not introduced here)

- The 18 E2E failures present at baseline remain present at baseline level;
  they are environmental, not Phase-27 regressions (verified by re-running the
  exact same E2E selection on a clean `c09ba40` worktree).
- Real-model ASR alignment E2E (`VIDEOMERGER_TEST_REAL_ALIGNMENT`) and the
  drawtext-dependent watermark/subtitle paths require assets/builds not
  available in this sandbox and stay skipped, as at baseline.
- Per-track music trim is fully supported in the model/engine/graph; the GUI
  exposes track add/remove/reorder (trim editing is model/API-level, matching
  the "preserve if supported" scope).

## Files changed

```
README.md  README_DE.md  PHASE27_DELIVERY.md
app/video_merger/models.py
app/video_merger/music_tracks.py            (new)
app/video_merger/main_project.py
app/video_merger/command_builder.py
app/video_merger/render_cache.py
app/video_merger/subtitles.py
app/video_merger/subtitle_preview.py
app/video_merger/youtube_outputs.py
app/video_merger/timeline.py
app/video_merger/chunked_render.py
app/video_merger/gui/main_window.py
tests/conftest.py
tests/test_phase27_music.py                 (new)
tests/test_phase27_subtitles.py             (new)
tests/test_phase27_gui.py                   (new)
tests/test_clip_continuity.py               (new)
tests/test_chunked_rendering.py
tests/test_phase4_multi_voiceover.py
```
