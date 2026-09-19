# Phase 28 Delivery Report – VideoMerger

**Repo/Branch:** `mustchange1/VideoMerger` · `arena/01a091fe-videomerger`
**Starting commit (baseline):** `bb5470d841ec7d5b159e0303c6610ac6dc0a6283`
**Final commit:** the HEAD of this branch (`git log -1`)
**Contract:** strict additive superset of `bb5470d` — nothing that worked was
removed, redesigned or silently changed; an untouched project renders exactly
like before.

---

## 1. Feature summary & implementation notes

### 1.1 Dedicated YouTube Shorts video folders (B)
* New field `ExportSettings.shorts_video_folders: list[str]` (default `[]` ⇒
  legacy behavior). Persisted through `SettingsStore` (`asdict`), migrated
  safely for old projects (missing key ⇒ `[]`).
* `MainProjectEngine._shorts_folder_pool()` resolves the pool: missing folders
  are warned + skipped; zero existing folders or zero videos ⇒ clear
  `VideoMergerError`. Ordering uses the shared `order_media_for_video_order`
  contract (Natural/Alphabetical/Random/Manual), so the same continuity
  safeguards apply.
* `create_youtube_exports()` (Shorts/Combined): dedicated pool first, fall back
  to the historical `shorts_area_pool`. Preflight planning (`take_for_duration`,
  `choose_fps`) and the defensive unreserved fallback both use the restricted
  pool — a Short can never fall back to Long-Form-only material.
* Long-Form renders never read Shorts folders (verified in E2E S1).
* GUI: "YouTube Shorts Video Sources" list with Add/Remove/Clear/Up/Down in the
  Project group, strictly separate from the Long-Form folder list.

### 1.2 Flexible music playback & sequence modes (C)
* Track dicts gain `playback_mode` (`once`|`loop`|`repeat`, default `once`) and
  `repeat_count` (default 1, clamped 1–1000). Normalization keeps legacy
  entries valid (`normalize_music_track`).
* New per-profile fields `music_sequence_mode` / `short_music_sequence_mode`
  (`loop_sequence` default = historical whole-sequence loop; `play_once` ends
  the music after the last track).
* `build_music_segment_plan()` expands a sequence into ordered segments for a
  target duration: `once` continues, `repeat` appends N copies, `loop` consumes
  the remainder (documented: later tracks unreachable), final segment clamped
  to the audio boundary, 5000-segment safety cap.
* `command_builder`: legacy graphs stay byte-identical — single track without
  trim under `loop_sequence` keeps `-stream_loop -1`; multi-track all-once
  `loop_sequence` keeps the Phase-27 whole-sequence graph
  (`sequence_is_legacy`). Anything else builds the explicit segment graph
  (`msegN` atrim chains → concat → volume → window → apad).
* Bug fixed during implementation: single track + `play_once` was classified
  legacy (music would have looped forever); it now takes the segment graph and
  ends after one play.
* GUI: per-profile "Track: Play Once / Loop / Repeat N" buttons + repeat spin +
  explanatory label + sequence-mode combo. List rows carry the mode visibly
  (`· loops until the end`, `· ×N`). Items store full entry dicts; plain-path
  items (used by older tests/imports) remain readable.

### 1.3 Music independence (feature 3)
Long-Form and Shorts each keep their own track list, order, per-track modes,
sequence mode, volume and trim. `short_settings()` maps the Shorts sequence
mode onto the job; the Long-Form job never reads a Shorts-only value (unit +
E2E verified).

### 1.4 Persistence & migration (features 4, 12)
* All new fields have backward-compatible defaults; `SettingsStore.load()`
  ignores unknown keys and supplies defaults for missing ones.
* Legacy one-track ⇒ one-item sequence with unchanged behavior (the legacy
  stream-loop graph). Multi-track Phase-27 projects migrate untouched.
* Minimum new fields: `shorts_video_folders`, `shorts_duration_after_merge`,
  `music_sequence_mode`, `short_music_sequence_mode` + per-track
  `playback_mode`/`repeat_count`. No UI-only state persisted.

### 1.5 Cache discipline (feature 5, I)
* Stage-1 payload gains `music_sequence_mode` only while music is active, and
  `music_tracks[].playback_mode/repeat_count` only when non-default. Default
  projects keep their exact historical fingerprint (no new keys) — existing
  Stage-1 caches stay valid.
* Music identity never appears in ASR/alignment cache keys (unchanged
  architecture; unit-tested).

### 1.6 Large Shorts subtitle preview (feature 6, E)
* `_open_large_subtitle_preview(profile)` is the single shared implementation
  for both profiles (one source of truth: `preview_cue` + the real renderer
  geometry). The Shorts dialog reads ONLY the Shorts controls in the portrait
  1080×1920 frame (Long-Form aspect radio cannot leak in); it adds
  `font_size_percent` (previously missing in the large dialog), the word-stage
  slider and the "Include Image" background (same fill semantics as the live
  preview). New button: "Open Larger Shorts Subtitle Preview".

### 1.7 Shorts subtitle sync optimization (feature 7, F)
* Root cause found: per-word ASS events quantize to centiseconds; tightly
  packed word boundaries (fast Shorts pacing, word_highlight) could collapse to
  equal/overlapping timestamps.
* Targeted fix: `_ass_time_centis()` enforces strictly monotonic boundaries in
  the per-word branch (`start >= previous end`, `end >= start + 1 cs`).
* Provably a no-op at normal pace: word spacing (≥ `MINIMUM_WORD_SPACING`
  0.02 s) is always larger than the 0.01 s grid — verified byte-identical by
  the 112 subtitle/animation/timeline tests at baseline plus a new compressed
  regression test. ASR/alignment untouched.

### 1.8 Center vs Middle deduplication (feature 8, D)
* Canonical user-facing set `SUBTITLE_POSITIONS = (Bottom, Center, Medium-Low,
  Top)` in both profile combos and the auto-resolution switch.
* `normalize_subtitle_position()` migrates duplicates: `Middle`→`Center`,
  `Bottom Center`→`Bottom` (case/hyphen-insensitive); unknown legacy spellings
  fall back exactly like the renderer, so the visible label matches the real
  spot.
* `SettingsStore.load()` canonicalizes saved values; the model default is now
  `"Bottom"` (renders pixel-identically to `"Bottom Center"` via the retained
  `_position` aliases — old API values keep working, CLI choices unchanged).
* E2E proves distinct burned geometry: Top → ASS alignment 8, Center → 5,
  Bottom → 2.

### 1.9 Shorts Duration After Merge up to 3.50x (feature 9, G)
* New field `shorts_duration_after_merge: float | None` (`None` ⇒ follow the
  shared value + enable flag — exact legacy pass-through). A configured value
  wins and enables itself for Shorts jobs only.
* Semantics unchanged: playback-rate multiplier on the merged master
  (`setpts=PTS/x` + atempo chain), never a literal time. Values >2.0 are split
  into valid atempo stages (3.5 = 2.0 × 1.75). Long-Form combo keeps its
  2.00x maximum.
* GUI combo: "Same as Long-Form" + 0.25–3.50 in 0.05 steps.

### 1.10 Clip continuity (feature 10)
Unchanged safeguards (`ShortsVideoPool` without-replacement consumption,
no-adjacent-folder ordering rule) now also govern the dedicated Shorts pool;
E2E asserts no adjacent duplicate source clips in rendered inputs.

### 1.11 GUI clarity (feature 11)
Every control is grouped and labeled per profile ("Background Music
(Long-Form)" vs "(Shorts)", "Duration After Merge (Shorts)", dedicated Shorts
sources group, per-profile subtitle groups with independent live previews and
independent large-preview buttons).

---

## 2. Changed files

```
app/video_merger/models.py            new fields + canonical default position
app/video_merger/music_tracks.py      modes, plan builder, legacy classifier
app/video_merger/command_builder.py   Phase-28 segment graph branch
app/video_merger/main_project.py      _shorts_folder_pool, pool wiring, plan modes
app/video_merger/youtube_outputs.py   Shorts after-merge + sequence-mode resolution
app/video_merger/render_cache.py      mode-aware Stage-1 payload (legacy-safe)
app/video_merger/subtitles.py         SUBTITLE_POSITIONS, normalization, monotonic ASS
app/video_merger/settings_store.py    legacy position migration on load
app/video_merger/gui/main_window.py   Shorts folders UI, music mode UI, after-merge
                                      combo, large Shorts preview refactor
config/example_settings.json          new fields documented
tests/test_phase28_features.py        NEW: 38 tests
tests/test_phase27_music.py           normalize shape updated (superset)
tests/test_phase18_optimization.py    canonical "Bottom" label
tests/test_phase27_gui.py             canonical position labels
tests/test_gui_controls.py            canonical position labels
```

## 3. Test results

* Full unit/GUI suite (markers `not e2e and not benchmark`):
  **914 passed / 0 failed / 84 deselected** (baseline `bb5470d`: 876 passed;
  +38 new Phase-28 tests; the 84 deselected are the pre-existing e2e/benchmark
  markers; the benchmark self-skips by design in this sandbox).
* New tests cover: data-model defaults, position normalization + store
  migration, mode normalization/clamping, `sequence_is_legacy` classification,
  segment-plan semantics (play-once sequence, per-track loop, repeat-N,
  legacy untouched), command-builder graph selection, Stage-1 payload cache
  discipline (legacy fingerprint preserved, defaults add no identity), Shorts
  after-merge resolution incl. 2.0–3.5 and the "1.0 disables itself" rule,
  dedicated Shorts pool (inactive/active/missing/empty), monotonic per-word
  ASS boundaries under compression, and GUI flows (folder list, mode editing,
  restore, large preview isolation).

## 4. Real-render E2E (ffmpeg 7.0.2, real encodes)

Sandbox: real generated clips (4 Long-Form 640×360, 3 vertical Shorts),
argument-logging ffmpeg wrapper, real aligner with injected deterministic
recognizer. **Result: 45/45 checks passed.**

| Scenario | Verified |
|---|---|
| S1 Combined, dedicated Shorts folders, mixed music modes | Long-Form uses ONLY Long folders, Shorts ONLY dedicated folders (both directions), no adjacent duplicate clips, Long music keeps legacy `stream_loop` graph, Shorts music builds the Phase-28 segment graph: track A exactly once, track B looped to the boundary |
| S2 Shorts After Merge 2.0 / 2.5 / 3.0 / 3.5 | real outputs rendered; after-merge pass executed; every atempo stage within [0.5, 2.0]; stage product equals the multiplier exactly |
| S3 Shorts positions Top / Center / Bottom | with+without-subtitle outputs rendered; burned `.ass` captured at the renderer boundary: distinct geometry, alignments 8 / 5 / 2 |
| S4 repeat×2 + play_once sequence | exactly 3 segments (A, A, B), never wraps |
| S5 unchanged re-run | zero new Stage-1 renders (cache hit), outputs intact |

## 5. Migration & cache behavior

* Old projects load unchanged: missing fields ⇒ defaults; `Middle`/`Bottom
  Center` migrate on load; legacy one-track music renders with the exact
  historical graph.
* Music/mode changes invalidate only the Stage-1 render identity; ASR and
  alignment caches are never touched by music.
* Default projects keep byte-identical Stage-1 fingerprints (no payload key
  added), so existing render caches remain valid across the upgrade.

## 6. Known limitations

* E2E proof uses the sandbox's static ffmpeg 7.0.2; visual QA of burned
  captions beyond ASS geometry inspection is not possible headless.
* `track: loop` consumes the remaining duration by design — tracks after a
  looping track are unreachable (documented in UI tooltip and plan builder).
* The benchmark test self-skips in this sandbox (missing espeak-ng/voice
  fixture) exactly as at baseline.

## 7. Push status

Committed on `arena/01a091fe-videomerger` and pushed to origin. `main`
untouched; no force-push, no history rewrite.
