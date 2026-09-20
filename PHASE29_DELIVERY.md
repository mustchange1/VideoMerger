# Phase 29 Delivery Report — Typewriter Hook Intro

**Branch:** `arena/01a091fe-videomerger`
**Baseline:** `20bf979` (Phase 28, authoritative — untouched, verified green before and after)
**Date:** 2026-09-20

---

## 1. Implementation Summary

Phase 29 adds a fully automated, production-rendered **Typewriter Hook Intro**:
an optional segment before the actual video in which a hook text is typed
character-by-character with typewriter sounds, holds briefly, and then hands
over to the normal video through the project's own transition system.

Pipeline (requirement 17), implemented as a dedicated isolated stage:

```
User Text → Typewriter Timeline → Intro Visual Render
          → Typewriter SFX Timeline → Intro Audio Mix
          → Existing Video/Transition Pipeline
```

* The intro is **not** subtitle content. The subtitle engine, ASS/SRT/VTT
  sidecars, ASR, word alignment and their caches are never touched.
* The intro is rendered as a standalone cached MP4 (Qt frames → FFmpeg
  rawvideo pipe, libx264 CRF 17) at the job's resolved resolution/fps, with a
  deterministic synthesized SFX WAV track, then merged in front of the
  finished program via `xfade` (same expression system as clip transitions)
  and `acrossfade` audio.
* Injection point: a post-render hook inside `MainProjectEngine.create_main`.
  This single point covers every consumer: YouTube Long-Form jobs, YouTube
  Shorts jobs, plain Main Video and the Complete Workflow's Stage-1 input.
* Subtitles are burned into the program **before** the intro is prepended, so
  the whole program (captions included) starts after the hook — identical
  semantics to the existing user-supplied `intro_path`.
* Everything is deterministic: timing, sound events, variant selection and
  frames are pure functions of the settings (no RNG, no wall-clock).

### Long-Form and Shorts independence

Two strictly separate profiles:

* Long-Form reads `typewriter_*` fields.
* Shorts reads `short_typewriter_*` fields, resolved 1:1 onto the canonical
  fields of each Short job inside `short_settings()` (same pattern as all
  other `short_*` settings). The Long-Form job never sees them and vice
  versa — proven by unit tests and by a combined-render E2E byte-parity test.

### Fail-safe behavior

* Disabled (default) → the hook code path is never entered; renders stay
  byte-identical to the historical pipeline (E2E byte-parity test).
* Enabled + empty/whitespace text → behaves exactly like disabled with a
  log message; never produces broken output (E2E byte-parity test).

## 2. Files Changed (all additive: +785 lines, 1 replaced import line)

| File | Change |
|---|---|
| `app/video_merger/typewriter_intro.py` | **NEW** dedicated intro stage: canonical option sets + normalizers, deterministic timeline builder, SFX synthesis (stdlib only), cache identity, shared Qt layout/frame renderer, FFmpeg asset encoder, merge-command builder |
| `app/video_merger/models.py` | 52 new `ExportSettings` fields (26 per profile) with spec defaults |
| `app/video_merger/main_project.py` | `_apply_typewriter_intro()` merge hook; Stage-1 fingerprint extended only when the intro is active; post-render application to burned + clean variants |
| `app/video_merger/youtube_outputs.py` | `short_settings()` maps `short_typewriter_*` → canonical fields per Short job (zero values preserved, never replaced by defaults) |
| `app/video_merger/render_cache.py` | Optional `typewriter_intro` Stage-1 payload key (present only when active) |
| `app/video_merger/gui/main_window.py` | New group "6 · Typewriter Hook Intro" with two strictly separated profile sections (26 controls each), large production-geometry preview dialog, full save/load wiring |
| `config/example_settings.json` | All 52 new keys with their built-in defaults |
| `tests/test_phase29_typewriter_intro.py` | **NEW** 45 unit tests |
| `tests/test_phase29_typewriter_e2e.py` | **NEW** 8 real-render E2E tests |

No other files were touched. `main` is untouched. No VoiceOverApp content
exists in this repository or in this change.

## 3. Settings Added (all with backward-compatible defaults)

Per profile (Long-Form unprefixed, Shorts `short_` prefixed):

| Key | Default | Notes |
|---|---|---|
| `typewriter_intro_enabled` | `false` | Feature OFF by default |
| `typewriter_hook_text` | `""` | Multi-line, Unicode, manual `\n` breaks |
| `typewriter_speed` | `"auto"` | slow / normal / fast / auto (natural, bounded, deterministic; 20 s typing cap) |
| `typewriter_sound_frequency` | `"every_character"` | every_character / every_word / word_boundary / off |
| `typewriter_sound_preset` | `"typewriter_1"` | typewriter_1 / typewriter_2 / mechanical / soft_keyboard / off |
| `typewriter_sound_volume` | `30` | 0–100 %, medium-low default |
| `typewriter_cursor_enabled` | `true` | Blinking block cursor (0.8 s period) |
| `typewriter_position` | `"Center"` | Exactly five canonical spots: Top / Upper-Middle / Center / Lower-Middle / Bottom (aliases like "Middle" migrate to Center) |
| `typewriter_h_align` | `"Center"` | Left / Center / Right, independent of vertical position |
| `typewriter_font` | `"modern_sans_bold"` | Reuses `FONT_OPTIONS`/`resolve_font` |
| `typewriter_font_size` | `100` | Percent of the resolution-aware base size |
| `typewriter_bold` | `true` | |
| `typewriter_color` | `"#FFFFFF"` | |
| `typewriter_outline_enabled` | `true` | Safe readable default |
| `typewriter_shadow_enabled` | `false` | |
| `typewriter_box_enabled` / `_box_opacity` / `_box_padding` | `false` / `55` / `40` | Optional background box |
| `typewriter_background_image_enabled` / `_path` | `false` / `""` | OFF = dark neutral background; cover-fit, common formats, missing file falls back to neutral |
| `typewriter_background_darken` / `_blur` / `_zoom` | `0` / `false` / `false` | Optional intro-only effects, all OFF |
| `typewriter_hold_seconds` | `0.5` | Clamped 0–10 s |
| `typewriter_transition` | `"project"` | `"project"` = the profile's existing transition; else one of the four transition keys |
| `typewriter_music_mode` | `"start_with_video"` | Default preserves historical music placement; `continue_during_intro` mixes the music's first intro-seconds under the SFX |

## 4. Migration / Backward Compatibility

* No settings migration needed: every new field has a dataclass default and
  `SettingsStore.load()` tolerates missing keys (verified by test).
* Old projects load unchanged and render identically (intro disabled by
  default). Verified: legacy settings file without any Phase-29 keys loads
  with all defaults.
* `config/example_settings.json` extended with all 52 keys at default values.

## 5. Cache Behavior

* **Intro asset cache:** `cache/typewriter/<sha2>/<identity>.mp4` + `.wav`,
  where identity = SHA-256 over every profile field + width/height/fps +
  schema version. Identical settings reuse the asset byte-for-byte (E2E test
  proves the file set does not grow across identical renders).
* **Stage-1 render cache:** when the intro is ACTIVE, the payload gains one
  key `typewriter_intro: <identity>` (cached artifacts already contain the
  intro). When disabled/empty, **no key is added**, so every historical
  project keeps its exact Stage-1 fingerprint and all existing caches remain
  valid (unit-verified against the unmodified payload).
* **ASR / alignment / subtitle caches:** never contain intro data — the intro
  stage runs after all of them and never modifies their inputs.

## 6. The Shorts "missing text" investigation — root cause and fix

During E2E verification the final encoded Shorts video appeared to contain
only the intro background without the typed text. A minimal deterministic
reproduction through the **actual production path** (asset → merge command →
xfade → encoded output at 720×1280 with `TYPEWRITER TEST`) proved:

* the standalone intro asset contains progressive text,
* the merged final video contains progressive text
  (bright-pixel ratio 0.00229 → 0.00474),
* the hold frame is stable, the transition blends, and frames after the
  transition are pure program with **zero** residual overlay.

**Root cause: the test, not the production path.** The Shorts orchestrator
enforces the vertical YouTube preset (720×1280) regardless of the requested
180×320. The E2E test decoded raw frames assuming 180×320 and therefore read
only the first 172,800 bytes of a 2,764,800-byte frame — the top ~75 rows of
background, well above the Upper-Middle text. **Fix:** the test now probes
the real encoded resolution via ffprobe before frame analysis. No production
code was weakened or changed to accommodate the test.

## 7. Test Results

### Unit / GUI suite (`-m "not e2e and not benchmark"`)

```
959 passed, 0 failed, 92 deselected   (baseline 914 + 45 new Phase-29 tests)
```

New unit coverage includes: all normalizers incl. position aliases; timeline
determinism (double-build equality); every visible character exactly once in
order; Unicode/emoji/Umlaut preservation; manual line breaks; one-char /
empty / 5000-char texts (typing cap); all 4 speeds; all 4 sound frequencies;
preset-off; deterministic byte-identical SFX WAVs; volume scaling; cache
identity sensitivity to every render input; LF/Shorts profile independence;
explicit-zero preservation in the Shorts mapping; Stage-1 payload unchanged
when inactive; settings store round-trip + legacy migration; example-settings
completeness; GUI round-trip + both preview profiles; layout for all 5
positions × 3 alignments on 16:9 **and** 9:16; frame-render determinism and
progressive reveal; background image cover-fit/effects and missing-file
fallback; merge-command transition clamping and music-mode chains.

### E2E suite (`-m "e2e"`)

```
68 passed, 6 skipped, 18 failed
```

The 18 failures are **byte-for-byte the same 18 failures as the pristine
baseline `20bf979`** (verified by exporting `git archive 20bf979` and running
the identical E2E selection there; the failure lists diff to zero). They are
known sandbox FFmpeg-shim quirks of this Linux test container, unrelated to
Phase 29 — Phase 29 introduces **zero** new E2E failures. The 8 new Phase-29
E2E tests all pass.

### Phase-29 E2E tests (all with real FFmpeg renders)

1. Disabled intro keeps the historical render **byte-identical**.
2. Enabled + empty/whitespace text behaves disabled (byte-identical).
3. Long-Form real render: progressive typed text (measured bright-pixel
   growth), silent hold, synced SFX, voiceover only after the intro,
   transition blend, clean program after the transition, exact duration
   `program + intro − transition`, no replayed ending.
4. Shorts real render (720×1280 vertical): progressive text, hold, blend,
   clean program after transition, synced SFX, exact duration.
5. Combined export: Shorts-only intro leaves the Long-Form output
   **byte-identical**; the Short grows by intro − transition.
6. Subtitles: SRT/VTT sidecars byte-identical with and without the intro;
   burned output longer by exactly intro − transition.
7. Music modes: default = no music under the intro (historical placement);
   `continue_during_intro` = music measurable under the intro.
8. Intro asset cache reuse: identical second render adds no new cache files.

## 8. Real-Render Proof (final encoded videos)

Long-Form render, hook "Why do 97% fail?\nWatch till the end." (normal speed):

| Timestamp | Evidence |
|---|---|
| t=0.73 s (25 % typing) | bright-text ratio 0.00325 (partial text visible) |
| t=2.79 s (95 % typing) | bright-text ratio 0.01224 (text grew ~3.8×) |
| t=3.03 s (hold) | 0.01102 (complete text, cursor blink phase) |
| t=3.26 s (transition) | blend into program |
| t=3.43 s (after) | pure program, no overlay |
| Audio | click RMS 0.089 during typing; **0.00000** during hold; voiceover 0.060 only after the intro; first click at 0.000 s = first character, last click 2.857 s ≈ typing end 2.933 s |

Shorts render (720×1280), hook "POV:\nDu scrollst trotzdem weiter":

| Timestamp | Evidence |
|---|---|
| t=0.42 s (30 % typing) | bright-text ratio 0.00337 |
| t=1.33 s (95 % typing) | 0.00761 (progressive) |
| t=1.50 s (hold) | 0.00761 (stable) |
| t=1.69 s (transition) | blend |
| t=1.88 s (after) | program fills the frame, zero residual overlay |
| Audio | typing RMS 0.108; hold RMS 0.00000 |

## 9. Known Limitations

1. **`continue_during_intro` overlap:** during the (short) acrossfade, the
   intro's music tail and the program's music beginning overlap briefly.
   Audibly seamless in practice; the default mode avoids it entirely.
2. **Complete Workflow ordering:** in the Stage-2 composition the typewriter
   hook is part of the Main Video, so a user-supplied `intro_path` plays
   first, then the hook, then the content. On the dominant direct
   YouTube Long-Form/Shorts paths the hook is the very first thing shown.
3. **SRT/VTT sidecar timestamps** describe the spoken program (unchanged);
   like the existing user-intro workflow, they are offset by the intro
   duration relative to the final file's timeline. Burned-in captions shift
   with the video they belong to and stay perfectly in sync.
4. **Extreme hook lengths:** Auto speed caps typing at 20 s so very long
   texts type extremely fast rather than dragging; practical hooks are short.
5. **Intro asset encoding** is fixed libx264 CRF 17 (high quality); the final
   merge re-encodes with the project's own encoder settings.
6. The 18 pre-existing sandbox E2E failures (identical at baseline `20bf979`)
   remain environmental FFmpeg-shim quirks of this Linux container; they are
   not Phase-29 regressions and pass on real Windows/FFmpeg installations per
   the Phase-28 Windows test procedure.

## 10. Preservation Statement

* Built strictly additively on authoritative baseline `20bf979`:
  **+785 lines, 1 deleted line** (an import line replaced by its expanded
  form). Verified via full `git diff` inspection — no silent feature drops.
* Full historical suite green: **914/914** of the pre-existing tests still
  pass (now 959 with the new Phase-29 unit tests).
* E2E failure set byte-identical to pristine `20bf979` (no new regressions).
* Disabled intro ⇒ byte-identical historical renders (E2E proof).
* Subtitle appearance/timing/sync, ASR/alignment, music pipeline, clip
  continuity, transitions, Long-Form and Shorts behavior: unchanged
  (dedicated E2E tests + full-suite evidence).
* `main` untouched; no force-push, no rebase, no history rewrite.
* No VoiceOverApp files, folders, caches or TTS code touched (none exist in
  this repository).
