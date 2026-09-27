# Phase 33: Smart Visuals — Content-Aware Selection & Timeline Placement Engine — Delivery

Rework of Smart Visuals on top of the verified Phase-32 production baseline
(`32ffb96`). Smart Visuals is now a **pure content-aware asset selection +
timeline placement engine**: it selects existing images and videos from the
configured pools using their metadata, places them on the one existing
timeline, and **never generates media**. `main` is untouched; this work is a
normal descendant of `32ffb96` on `arena/01a091fe-videomerger`. No rebase, no
force-push, no history rewrite.

Highest-priority rule for this phase: **do not regress anything that works.**
Subtitles, timing, voiceover sync, Shorts, transitions, rendering, audio,
typewriter, image effects and the Phase-32 contract were all preserved; every
preserved behavior is pinned by tests (see §5).

## 1. What it does

### A — Three selection modes (replaces generation + fallback policies)

| Mode | Behavior |
|---|---|
| `smart_match` (default) | Best semantic match per region; seeded random draw from EXISTING pool media when nothing reaches the relevance threshold |
| `smart_inserts` | Mostly random: only a strong match is placed at ~25 % (configurable) of the insert opportunities; everything else is a random pool draw |
| `random_only` | No semantic matching at all — seeded random placement from the pool (baseline / debug) |

Threshold policy lives in ONE constants place in `smart_visuals.py`
(`SMART_MATCH_THRESHOLD = 0.50`, `SMART_INSERT_STRONG_THRESHOLD = 0.60`) and
is not exposed as magic values in the primary UI.

### B — Generation removed from the Smart Visuals workflow
The Smart Visual plan no longer resolves any generation provider, and there is
**no hidden generation fallback**: insufficient relevance falls back to a
seeded random **existing** asset (tagged `FALLBACK_RANDOM_VIDEO` /
`FALLBACK_RANDOM_IMAGE` per asset kind). Unrelated infrastructure
(`image_generation.py` and its tests) is retained untouched for other
workflows; the Smart Visual code path contains zero references to it.

### C — Semantic, explainable matching on existing metadata
* Context extraction order: script/transcript → voiceover-aligned words →
  section/topic → timeline/video metadata; a local context window per visual
  region. No network, no GPU, no video-content analysis — the timeline text
  is matched against asset metadata only.
* Tolerant sidecar metadata harvest: known fields (`title`, `description`,
  `keywords`, `topic`, `subject`, `entities`, `mood`, `environment`, `scene`,
  …) are read into structured fields; **unknown keys enrich the free-text
  metadata** instead of being rejected. Multiple sidecar formats are accepted.
* Normalization: case / punctuation / plurals / word forms / stopwords /
  synonym groups; media index built once and cached (`build_media_index`).
* Every selection is explainable: `source_mode` (SMART / RANDOM), `score`,
  `reason` (`matched[:tokens]`, `smart_fallback_random_no_relevant_match`,
  `smart_insert_random_baseline`, `random_only_mode`,
  `matched_pool_exhausted_reuse`, `skipped_no_media`) and a per-slot fallback
  tag — all exposed through the reusable `SmartVisualPlan.to_records()`
  structure and the GUI.

### D — Uniqueness per output video
Each asset (image or video) is used at most once while unused pool assets
remain; all pool assets are consumed before any reuse; reuse happens only
when the pool is exhausted and never places the same asset twice back-to-back
(where avoidable). Randomize respects the same uniqueness.

### E — Single Image Duration (default 5.0 s)
One configurable value, applied uniformly to every inserted visual
(`smart_visual_image_duration` / `shorts_smart_visual_image_duration`,
default 5.0). Placement stays strictly additive — inserts extend the target
duration and never move voiceover, subtitles, audio, transitions or the
existing clip timeline.

### F — Analyze Timeline + Randomize Timeline
* **Analyze** runs the real planner (no render) and lists per-segment records:
  start/end/duration, type (VIDEO | IMAGE), filename, source (Smart / Random),
  score and reason.
* **Randomize** bumps the persisted `*_randomize_nonce`, re-analyzes with a
  new seeded assignment (Smart Match explores alternate good matches via
  top-K) and keeps mode, duration, folders and the timeline intact.

### G — Section "7 · Image Timeline & Visual Effects" is clickable again
Root cause fixed: the sub-controls only unlocked when an insertion mode was
enabled, and the per-image `effect` / global `global_effect` dropdowns were
never wired to the sync routine. Both effect blocks (per-image TV effect +
global TV overlay) are **kept**, always configurable, and their effect choice
stays independent of image selection.

### H — Typewriter: Hold After Typing default 3.0 s (new/unset only)
`typewriter_hold_seconds` / `short_typewriter_hold_seconds` default to 3.0
(`DEFAULT_HOLD_SECONDS = 3.0`) for new or unset configurations; explicitly
saved values are preserved verbatim.

## 2. Guarantees (verified by tests)

* **No generation in Smart Visuals:** the selection path never resolves a
  provider; legacy `allow_generated` / `generate_image` /
  `generation_strategy` settings are inert (unit + real-render proofs).
* **Uniqueness:** no duplicate asset while the pool is sufficient; exhausted
  pools reuse without immediate back-to-back repeats; Randomize keeps it.
* **Timing untouched:** Smart-Visual renders keep subtitle files, voiceover
  alignment and transition clocks identical to the no-Smart-Visual baseline;
  image inserts only extend the target.
* **Determinism:** seeded RNG everywhere (fixed seeds in every test); the
  same project re-renders identically; Randomize explores a different but
  still valid assignment.
* **LF/Shorts separation:** every setting, resolver and GUI section is
  per-profile; Shorts keep their own mode, duration and portrait geometry.
* **Backward compatibility:** old projects load with safe defaults
  (Mode=Smart Match, Image Duration=5.0 s, Insert Frequency=25 %,
  Hold=3.0 s); explicit saved values are never overwritten.
* **Phase-32 contract intact:** byte-identical default render, dedicated
  image transitions, image visual effects, completion sound, generation-OFF
  guarantees — all still passing.

## 3. Settings (persisted, documented in `config/example_settings.json`)

| Key (Long-Form) | Shorts mirror | Default |
|---|---|---|
| `smart_visual_mode` | `shorts_smart_visual_mode` | `smart_match` |
| `smart_visual_image_duration` | `shorts_smart_visual_image_duration` | `5.0` |
| `smart_visual_insert_percent` | `shorts_smart_visual_insert_percent` | `25` |
| `smart_visual_randomize_nonce` | `shorts_smart_visual_randomize_nonce` | `0` |
| `typewriter_hold_seconds` | `short_typewriter_hold_seconds` | `3.0` (unset only) |

Legacy Phase-31/32 keys (`smart_visual_fallback`, `smart_visual_allow_generated`,
threshold fields, …) still load safely and are ignored by the selection
engine. Existing image rendering settings (`timeline_image_transition_*`,
`timeline_image_visual_effect*`, motion) are unchanged and still applied.

## 4. GUI

The Smart Visuals section (per LF/Shorts profile) is intentionally compact:
Enabled, pool folders (+ tools), **Mode**, **Image Duration**, **Smart Insert
Frequency** (Mostly Random only), **Analyze Timeline**, **Randomize
Timeline** and the plan list. The former generation widgets (provider,
strategy, style, allow-generated) are gone from this section. The "7 · Image
Timeline & Visual Effects" block keeps motion, per-image TV effect, global TV
overlay and preview — all clickable at all times.

## 5. Verification (exact runs, 2026-09-27)

**Phase-33 test inventory — 43 new tests, ALL PASSING:**

| Suite | File | Tests | Result |
|---|---|---|---|
| Selection engine (modes, uniqueness, randomize, tolerant metadata, image duration, no-generation trap) | `tests/test_phase33_smart_visuals.py` | 22 | 22 passed |
| GUI (clickable section-7 controls, Analyze/Randomize, hold default, round-trip, legacy load) | `tests/test_phase33_gui.py` | 10 | 10 passed |
| E2E real FFmpeg renders (match placement, fallback, uniqueness, duration math, randomize, Shorts, byte-identical disabled) | `tests/test_phase33_e2e.py` | 11 | 11 passed |

* **Unit + GUI regression (final run):**
  `python3 -m pytest tests/ -q -m "not e2e"` →
  **1139 passed, 0 failed, 138 deselected** (Phase-32 baseline was 1109;
  +30 net new unit/GUI tests; the 9 generation-era tests that encoded the
  retired generation contract were replaced 1:1 by selection-contract tests,
  never deleted).
* **Full E2E regression (final run):**
  `python3 -m pytest tests/ -q -m e2e` → **115 passed, 17 failed,
  6 skipped**. Every Phase-23…33 smart-visual e2e test passes. The 17
  failures are the exact pre-existing set documented in the Phase-32
  delivery (sandbox/ffprobe-shim limitations in legacy transition/subtitle
  e2e tests), byte-identical to the Phase-32 baseline — NOT Phase-33
  regressions, intentionally preserved, not removed or weakened.
* **Reworked generation-era E2E contracts (1:1, documented in the module
  docstrings):** phase31 `D/E/F/K` and phase32 `B/B2/C/G/I` now assert the
  selection engine — existing-media fallback, deterministic selection,
  kind-aware random tags, empty-pool skip == video-only render, and legacy
  generation settings being inert — with real FFmpeg renders.

## 6. Files changed (app/)

`smart_visuals.py`, `models.py`, `youtube_outputs.py`, `typewriter_intro.py`,
`main_project.py`, `gui/main_window.py`, `config/example_settings.json` —
plus three new Phase-33 test files and reworked Phase-31/32 contract tests.
`image_generation.py` was NOT modified. Nothing else was changed; no existing
behavior outside the retired generation workflow was removed or redesigned.

## 7. Statements

Supported by the test results in §5:

> **No existing Smart Visual image-generation capability remains active in
> the Smart Visuals workflow.**

> **Existing audio, subtitle, transition, rendering, and synchronization
> behavior was preserved.**
