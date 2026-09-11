# Phase-26 Feature-Parity Report (final delivery)

**Date:** 2026-09-11 · **Branch:** `arena/01a091fe-videomerger` · **Merge commit:** `1f54c1a` (pushed)

## 1. What happened

Phase 27 was originally implemented on top of `c09ba40`, which turned out to be
an **incomplete base**: it does not contain Phases 19–26 (commits `f13bced` →
`afc8019`). The true last-known-good state is **`afc801918150584c136c355196ef622963c457fc`**
(remote branch `arena/01a068dd-videomerger`).

Recovery was done as an **additive merge** of `afc8019` into the Phase-27 line
(merge commit `1f54c1a`): no history rewrite, no force-push, `main` untouched.
The result is a strict **superset**: every Phase-26 feature with its exact
historical defaults + all Phase-27 features + the two known-regression fixes.

## 2. Feature-parity audit

Legend: HIST = status at last-known-good `afc8019` · CUR = status on the merged
branch · REG = regression found? · FIX = what was done.

| # | Feature area (as requested) | HIST (`afc8019`) | CUR (merged) | REG? | Fix |
|---|---|---|---|---|---|
| A | Video input/pool: folders + per-folder timeline areas (`timeline_areas.py`, `source_folder_areas`), Required-Only pool (`video_pool.py`), loop/Hold, Full-Timeline Loop | complete | complete (+ no-consecutive-duplicate guard) | no | none needed; `enforce_continuous_sources` verified in all 3 `fit_media_to_duration` returns |
| B | Every Long-Form option: transition family/ease/duration, background blur/darkness/zoom, normalize audio, FPS/encoding/CRF/preset, quality & output presets, opening effects, visual intro/outro, watermark, HDR-safety flag | complete | complete | no | merge kept `long_form_settings()` intact |
| C | **Every Shorts setting**: aspect 9:16, resolution Auto, export modes, **own music** (volume, ducking, per-Short overrides), **own transition type/duration**, subtitle preset/font/**size**/position/animation, **Duration Before Merge**, clip selection w/o replacement, area filter, preview | complete (single-track music) | complete (+ multi-track Shorts sequence, independent Shorts font size & DBM) | **YES** — music profile independence and Shorts multi-track were absent on the Phase-27 base | new `short_music_tracks` profile; resolution order override → sequence → legacy `short_music_path` → silent; Long-Form sequence replaced at the Shorts job boundary so the two profiles can never disagree; GUI has two independent track lists |
| D | Subtitles: presets, fonts, sizes, positions, animations, language (German/English canonical), debug overlay, alignment warning, script authority, timing/SRT/VTT/canonical timeline, burn-in, preview | complete | complete (+ separated Long/Short groups, independent font sizes, fail-closed gate, debug default OFF) | no | styling stays decoupled from ASR/alignment; `preview_cue` production renderer shared by both previews |
| E | Audio: multi voiceover/script units, ordering/pause, original audio modes, music presets/volume/ducking, per-output volumes, intro/outro audio, image audio | complete | complete (+ whole-sequence music loop per profile) | no | `output_music_volume()` migration fallback verified |
| F | Stage 1/Stage 2: Main Video / Intro / Outro / Add Image, stage transitions & audio, naming | complete | complete | no | quote-artwork intentionally absent (see §5) |
| G | Order/loop: natural/mtime/manual modes, folder-based ordering only (no content analysis) | complete | complete | no | — |
| H | Output/quality presets (`maximum/high/balanced/fast/custom`, `youtube_landscape/youtube_vertical/custom`) | complete | complete | no | — |
| I | Persistence: save/restore of every setting, migrations, legacy fallbacks | complete | complete (+ `music_tracks`, `short_music_tracks`, `short_subtitle_font_size`, `duration_before_merge_shorts`) | no | legacy `music_path`/`short_music_path` → one-track sequences; shared volume/transition copied into both output profiles on load; `video_speed` and `final_pause` migrations kept |

### Known-regression detail

1. **Shorts music** — FIXED. Shorts now own a fully independent configuration:
   single-track (legacy `short_music_path`) **and** multi-track
   (`short_music_tracks`) both available; the entire sequence loops as one unit
   per output profile; changing Long-Form never touches Shorts and vice versa;
   migration is safe (empty + legacy path = historical one-track behavior;
   both empty = silent Short, never inherits Long-Form).
2. **Shorts transition** — VERIFIED INDEPENDENT. `shorts_transition_type` /
   `shorts_transition_duration` existed historically at `afc8019` and are kept
   verbatim, including the shared-value migration fallback and the exact
   defaults (Cross Dissolve / 2.0 s for both outputs; legacy shared default
   1.0 s is treated as "never configured"). All four transition families
   (Smooth Blur, Cross Dissolve, Film Dissolve, Additive Dissolve) remain
   selectable for both profiles; previews and renders resolve the correct
   profile's values (see §6 proof).

## 3. Restored features (missing on the old base, present from `afc8019`)

- `timeline_areas.py` — timeline areas A/B/C incl. Shorts area filter
- `script_sections.py` + `short_groups.py` — global-script-to-Short derivation and Short grouping (Phase 20/25)
- `opening_effects.py` — Long-Form opening visual effects (Phase 22)
- Visual intro/outro sections with explicit semantics (Phase 22)
- Shorts music ending text, short script sidecars (Phase 21)
- Phrase-level animations (`phrase_focus`), per-collection animation options and deprecation migration (Phase 22)
- Subtitle language pipeline (German/English canonical, Phase 26)
- Independent per-output music volume & transition pairs with migration (Phase 23)
- All Phase 19–26 test suites (restored as part of the merge)

## 4. New features (Phase 27, kept in the superset)

- Multi-track music sequences for **both** profiles; the ENTIRE sequence loops as a unit (A→B→C→A→B→C), never per-track
- Separated Long-Form / Shorts subtitle groups with two real previews sharing production geometry (1920×1080 / 1080×1920)
- Independent subtitle font sizes per profile (no effect on ASR/alignment)
- Per-profile Duration Before Merge (playback-rate multiplier, default 0.70)
- Debug overlay clearly separate, production default OFF, only emitted when ON
- Fail-closed alignment warning gate ("Continue After Alignment Warning" = explicit safety override)
- No consecutive duplicate source clips in normal planning (`enforce_continuous_sources`; randomness/manual order/loop untouched)
- Folder-based ordering only (no video-content analysis reintroduced)
- Cache identity extended for sequences, font sizes and per-profile DBM

## 5. Intentionally absent features (verified, not regressions)

- **Quote artwork / Flyer (`quote_artwork.py`, `test_phase3_quote_artwork.py`, PyMuPDF dependency)** — deliberately removed upstream in commit `4832452` ("removed quote_artwork"). The merge keeps the removal; `timeline._same_source` tolerates the removed attributes via `getattr` fallbacks. Confirmed absent at `afc8019`; not restored.

Nothing else from `afc8019` is missing: the merge diff was audited hunk by hunk
(4 conflict files resolved manually, all auto-merged files re-checked for
Phase-27 markers).

## 6. Tests & regression results

| Suite | Baseline `afc8019` | Merged branch |
|---|---|---|
| Unit/integration (`not e2e, not benchmark`) | 797 passed, **2 failed** (pre-existing) | **876 passed, 0 failed**, 84 deselected |
| e2e (real renders via sandbox ffmpeg shim) | same 18 failures (verified on baseline worktree) | 60 passed, 18 failed — **all 18 fail identically at `afc8019`** (sandbox shim limits: audio-window behavior, frame-color/blend-graph expectations, missing drawtext) → **zero merge regressions** |

Both pre-existing baseline failures were fixed by this work:
settings-store test pollution (conftest isolation fixture) and the music-preset
two-way resync bug. One stale Phase-22 test expectation (Shorts animation
default `word_highlight` vs the removed-for-Shorts animation) was corrected to
match the Phase-26 code's own documented behavior (`phrase_focus`).

New/updated tests:
- `tests/test_feature_parity_phase26.py` — 12 acceptance tests: defaults preservation, Long/Short music independence, sequence resolution order, Shorts sequence loop input, legacy migration, transition independence (type + duration, both directions), all transition options, persistence round-trip, GUI restore of both profiles
- `tests/test_phase27_gui.py` — updated to the dual-profile track-list API + new independence test
- `tests/test_gui_controls.py` — animation-default expectation corrected

**Real-render proof (merged tree):** combined export (Long-Form + Shorts) rendered
through the real pipeline — the Long-Form ffmpeg command references only
`long_form.mp3`, the Shorts command only `shorts_only.mp3` (argument log
audited); outputs valid (Long-Form 320×240 landscape, Shorts 720×1280 vertical,
both with AAC audio). Whole-sequence looping was previously proven in a real
render (`ABCABCA` spectral trace, `/tmp/e2e27b`).

## 7. Delivery status

- **Final commit:** `1f54c1a` (merge), on top of Phase-27 commits `5dca15f`/`c032725`
- **Branch:** `arena/01a091fe-videomerger` — pushed to origin
- **Push status:** ✅ pushed (`c032725..1f54c1a`)
- **Rules honored:** `main` untouched (still `1db2e91`), no force-push, no history rewrite
- Defaults preserved exactly; existing projects migrate safely (verified by store round-trip and legacy-file tests); doing nothing remains compatible (single-track legacy configurations keep byte-identical log lines and behavior)
