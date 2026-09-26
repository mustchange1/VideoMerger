# Phase 32: Smart Visual Fallbacks, Image Rendering & Typewriter Completion Sound — Delivery

Strictly additive refinement on top of the Phase-31 production baseline
(`0f2a28e`). Every Phase-32 setting ships at a default that reproduces the
exact Phase-31 behavior; the single intentional default change is the new
Typewriter completion sound (ON). `main` is untouched; this work is a normal
descendant of `0f2a28e` on `arena/01a091fe-videomerger`. No rebase, no
force-push, no history rewrite.

## 1. What it does (Feature Groups A–H)

### A — Smart Visual fallback policy (+ per-slot diagnostics)
New per-profile setting `smart_visual_fallback` /
`shorts_smart_visual_fallback` with five policies:

| Policy | Behavior below the match threshold |
|---|---|
| `generate_image` (default) | Exact Phase-31 flow: generate → best existing → skip |
| `random_video` | Seeded random draw from the pool's videos (no matching) |
| `random_image` | Seeded random draw from the pool's images |
| `best_available` | Highest-scoring pool media of either kind, never generation |
| `skip` | Insert nothing; the slot stays a pure video-only section |

Every planned slot carries a machine-readable diagnostic tag used by the
plan preview: `MATCH`, `GENERATED`, `FALLBACK_GENERATED`,
`FALLBACK_RANDOM_VIDEO`, `FALLBACK_RANDOM_IMAGE`, `FALLBACK_BEST_AVAILABLE`,
`SKIPPED` — visible in the GUI preview list and in
`SmartVisualPlan.to_records()`.

### B — Dedicated image transition + duration
`timeline_image_transition_type` + `timeline_image_transition_duration`
(canonical; per-profile mirrors `long_form_image_transition_*` /
`shorts_image_transition_*`) control the xfade at every boundary that
touches a timeline image (V→I, I→V, I→I). `project` (default) keeps the
historical project-wide transition untouched; `none` forces a hard cut
(duration 0). The existing transition engine and its 45 % safety clamp do
all the work — no second engine, video→video boundaries are never affected.

### C — Five subtle image visual-effect presets
`timeline_image_visual_effect` (+ per-profile mirrors): `none` (default),
`soft_shimmer`, `gentle_flicker`, `film_flicker` (projector),
`crt_broadcast` (reuses the Phase-30 CRT scanline chain — one engine),
`soft_glow_pulse`. Each preset is a deterministic, geometry-aware filter
chain applied exclusively to image sections.

### D — Intensity Low / Medium / High
`timeline_image_visual_effect_intensity` scales each effect's designed
amplitude. Default is `low` (conservative). Effects never touch subtitles,
audio, or the video defaults; composition stays deterministic (motion +
transition + effect all derive from fixed settings and frame time).

### E — Enriched Smart Visual plan preview
`SmartVisualPlan.to_records()` and the GUI preview now show per slot:
media label, source kind, match score, match/fallback reason, fallback tag,
clamped duration, and the active image transition / effect / intensity.

### F — "Allow Generated Images" toggle
`smart_visual_allow_generated` / `shorts_smart_visual_allow_generated`
(default ON = Phase-31 behavior). When OFF, no generation provider is even
resolved — zero generation requests, no silent generation. Strategies that
want generation (e.g. *always*) fall back to the configured policy instead.

### G — Random fallback safeguards
Random fallback draws only from the profile's own Smart Visual folders
(LF/Shorts pool isolation), respects the repetition window (and still avoids
the immediately previous pick when the window exceeds the pool), honors
disabled/invalid files via the existing index validation, and is seeded from
the plan seed parts — identical settings ⇒ identical plan.

### H — Typewriter completion sound
One short, deterministic Enter/Return click plays when the final character
completes — immediately before the intro→video transition. Presets:
`enter_return` (default), `mechanical_keypress`, `typewriter_return`,
`off`; volume 0–100 (default 40 %). ON by default (the one intended
behavior change), fully independent from the per-character typing SFX, plays
exactly once, is clamped inside the intro track, and never disturbs typing
timing, subtitle timing, or intro duration. LF and Shorts have separate
settings (`typewriter_completion_sound_*` / `short_typewriter_*`).

## 2. Guarantees (verified by tests)

* **Defaults = Phase 31:** the Stage-1 fingerprint, Smart Visual plan
  identity, image plan identity and a real Smart-Visual render are
  byte-identical when every Phase-32 field stays at its default
  (`test_A_phase32_defaults_keep_phase31_render_byte_identical`,
  `test_default_phase32_settings_never_change_the_stage1_fingerprint`).
* **Identity churn only when active:** the render cache gains new keys only
  when an image effect is configured on timeline images / when Phase-32
  settings deviate from defaults.
* **Generation toggle is absolute:** OFF ⇒ provider never resolved, no
  generated files on disk, even with `generation_strategy=always`.
* **LF/Shorts separation:** every resolver path and GUI section is
  per-profile; Shorts never read Long-Form image/fallback/typewriter
  settings and vice versa.
* **Timing untouched:** completion sound renders change audio only (frame-
  identical videos, identical durations); image effects never change timing.

## 3. Settings (persisted, documented in `config/example_settings.json`)

| Key (Long-Form / canonical) | Shorts mirror | Default |
|---|---|---|
| `smart_visual_allow_generated` | `shorts_smart_visual_allow_generated` | `true` |
| `smart_visual_fallback` | `shorts_smart_visual_fallback` | `generate_image` |
| `long_form_image_transition_type` | `shorts_image_transition_type` | `project` |
| `long_form_image_transition_duration` | `shorts_image_transition_duration` | `null` (project) |
| `long_form_image_visual_effect` | `shorts_image_visual_effect` | `none` |
| `long_form_image_visual_effect_intensity` | `shorts_image_visual_effect_intensity` | `low` |
| `typewriter_completion_sound_enabled` | `short_typewriter_completion_sound_enabled` | `true` |
| `typewriter_completion_sound_preset` | `short_typewriter_completion_sound_preset` | `enter_return` |
| `typewriter_completion_sound_volume` | `short_typewriter_completion_sound_volume` | `40` |

Legacy projects (files without these keys) load with exactly these defaults.

## 4. GUI

* Smart Visual section (per LF/Shorts profile): *Allow Generated Images*
  checkbox, fallback policy combo, image transition combo + duration spin,
  image effect combo + intensity combo. Gating: strategy/style controls lock
  when generation is OFF; intensity locks while the effect is `none`.
* Plan preview shows the enriched per-slot records incl. fallback tag.
* Typewriter section (per LF/Shorts profile): completion-sound checkbox,
  preset combo, volume spin — compact, in the existing style, NOT inside the
  Smart Visual group.

## 5. Verification (exact runs, 2026-09-27)

**Phase-32 test inventory — 78 tests, ALL PASSING:**

| Suite | File | Tests | Result |
|---|---|---|---|
| Fallback policies + generation toggle (A, F, G) | `tests/test_phase32_fallback.py` | 14 | 14 passed |
| Image transitions + visual effects (B, C, D) | `tests/test_phase32_image_rendering.py` | 18 | 18 passed |
| Typewriter completion sound (H) | `tests/test_phase32_typewriter_completion.py` | 12 | 12 passed |
| Persistence + cache identity | `tests/test_phase32_persistence_cache.py` | 7 | 7 passed |
| GUI controls, gating, preview (A–H) | `tests/test_phase32_gui.py` | 15 | 15 passed |
| E2E real FFmpeg renders (A–H) | `tests/test_phase32_e2e.py` | 12 | 12 passed |

* **Unit + GUI regression (final run):**
  `python3 -m pytest tests/ -q -m "not e2e and not benchmark"` →
  **1109 passed, 0 failed, 127 deselected** (Phase-31 baseline was 1043;
  +66 new Phase-32 unit/GUI tests).
* **Phase-32 E2E (real FFmpeg renders):** **12/12 passed** — byte-identical
  default render (A), random-video fallback (B), random-image fallback (B2),
  skip parity with the video-only render (C), generation-OFF proof (D),
  **generation-ENABLED proof (I): a real render with generation allowed
  produces real generated PNG files via the local provider and shows them on
  screen**, image-transition duration math at real boundaries (E), hard cut
  (E2), shimmer frame proof with untouched timing (F), Shorts profile
  separation (G), completion sound plays exactly once without disturbing
  timing — Long-Form and Shorts (H, H2).
* **Full E2E regression (final run):**
  `python3 -m pytest tests/ -q -m e2e` → **104 passed, 17 failed,
  6 skipped**. Every Phase-23…32 e2e suite passes (zero Phase-suite
  failures). The 17 failures are legacy (pre-Phase-23-generation) e2e
  tests, and the failing-test set is **byte-identical to the verified
  Phase-31 baseline (`0f2a28e`) run in this sandbox** — they are
  pre-existing sandbox/ffprobe-shim environment limitations (transition
  frame-blend timing, subtitle ASR language gating), NOT Phase-32
  regressions. They are intentionally preserved in the suite, not removed
  or weakened.
* **One adapted contract test:** the Phase-29 long-form hold-silence e2e
  test now explicitly sets `typewriter_completion_sound_enabled=False`
  because it pins the ORIGINAL silent-hold behavior; the new default-ON
  click legitimately plays inside the hold and is covered by
  `tests/test_phase32_e2e.py` (H/H2) instead. No assertion was weakened.

## 6. Files changed (app/)

`models.py`, `smart_visuals.py`, `image_timeline.py`, `target.py`,
`command_builder.py`, `typewriter_intro.py`, `youtube_outputs.py`,
`main_project.py`, `render_cache.py`, `gui/main_window.py`,
`config/example_settings.json` — plus six new test files and one adapted
Phase-29 contract test. Nothing else was modified; no existing behavior was
removed or redesigned.
