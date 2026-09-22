# Phase 31: Smart Visual Hybrid — Delivery

Strictly additive, **opt-in** feature on top of Phase 30 (`1e0375d`). When
disabled (the default) every historical render, selection path (Manual /
Random / Folder), the Phase-30 image timeline and all cache identities stay
byte-identical. `main` is untouched; this commit is a direct descendant of
`1e0375d` on `arena/01a091fe-videomerger`.

## 1. What it does

The renderer builds a **visual plan BEFORE rendering** and then only
executes it through the existing Phase-30 timeline engine (no second
renderer, no second ASR pipeline):

1. **Timing (stage A):** semantic slots are derived from the canonical
   script sentences + the voiceover-driven program duration (proportional
   mapping; no second ASR).
2. **Slots (stage B):** short same-topic sentences are grouped into one
   slot (adaptive) or sized by the configured cadence (every 1–4 sentences).
3. **Matching (stage C):** slots are matched against a cached, incrementally
   built media index of the configured Smart Visual folders. Folder name =
   category; optional `smart_metadata.json` / `smart_metadata.csv` sidecars
   add titles/keywords. Existing media may be images or videos
   (configurable priority: Video First / Image First / Best Match /
   Balanced). Match score is a normalized 0–1 value (semantic cosine +
   keyword/tag + category bonus − repetition penalty).
4. **Generation (stage D):** when no existing media matches (or when the
   configured strategy demands it) a local image is generated through the
   `ImageGenerationProvider` abstraction. Generated images are cached
   content-addressed (`cache/smart_visual_generated/<sha256>.png`) and
   reused across projects; a repeated sentence therefore reuses the exact
   same file.
5. **Insertion (stage E):** selected visuals become genuine Phase-30-style
   timeline elements between the fitted clips — images reuse the Phase-30
   cover-fit/motion/TV-effect/transition chain; existing videos become
   silent, trimmed clip elements. Slots that collide on the same timeline
   boundary are skipped; the program target is extended by the net added
   visual time exactly like Phase 30.

## 2. Semantic layer (honest)

Local-first by design (spec §8 "embedding or equivalent", §17 no mandatory
internet/paid API): deterministic lexical concept vectors — EN/DE stopword
removal, light suffix stripping, a ~30-concept synonym map (EN+DE surface
forms), cosine similarity. No ML dependency, no network.

## 3. Local generation (honest, spec §13–17/49)

* `ImageGenerationProvider` contract: `is_available`, `generate`,
  `get_cache_key` (+ shared `generation_cache_key` over
  prompt+provider+model+style+w+h+params+seed).
* `DiffusersProvider` — real diffusion backend, availability-gated. In this
  environment `diffusers`/`torch` are not installed; it reports *unavailable*
  with clear diagnostics and never crashes. A real model was therefore
  **not** tested here; that is documented, not faked.
* `SyntheticConceptProvider` — **deterministic demo/test backend** that
  renders abstract gradient concept art via FFmpeg lavfi, seeded by the
  prompt hash (purple/magenta palette chosen so it can never be confused
  with video content). It is clearly labelled as synthetic in every
  diagnostic and in the GUI; it exists so the whole pipeline (prompts,
  caching, planning, insertion, rendering) is fully testable without a GPU.
* Generation happens BEFORE rendering (never inside the FFmpeg render pass),
  is cached, and any failure falls back to existing media (spec §32).

## 4. Settings (all opt-in, backward compatible)

New `ExportSettings` fields (23, default = feature OFF / empty folders):
`long_form_smart_visual_folders`, `shorts_smart_visual_folders`, canonical
per-job `smart_visual_enabled`, `smart_visual_source_priority`,
`smart_visual_threshold_mode` (+`_custom`), `smart_visual_generation_strategy`
(+`_percent`), `smart_visual_repetition_window`, `smart_visual_style`
(+`_custom`), `smart_visual_cadence`, `smart_visual_folders`, and the strictly
separate `shorts_smart_visual_*` profile. `long_form_settings()` /
`short_settings()` resolve each profile onto the canonical per-job fields —
the two profiles can never leak into each other. Old project files load
unchanged (dataclass defaults; `SettingsStore` allow-list). Stage-1
fingerprint gains `smart_visual_plan` **only** when the plan really placed a
visual, so disabled projects keep their exact historical cache identity.

## 5. GUI

New group **"8 · Smart Visuals"** with one section per output profile:
enable switch (OFF by default), media folder list with Add/Remove/Move/
Clear, Preferred Existing Media, Cadence, Minimum Match Score
(Low 0.35 / Medium 0.50 / High 0.65 / Custom), Generation Strategy
(Only When No Match / every 2nd–4th / random % / custom % / always),
Repetition Protection, Generation Style (+ custom prompt),
**Build/Refresh Media Index** (incremental; reports new/reused/errors/
categories), **Preview Smart Visual Plan** (time / topic / selected media /
match score / fallback reason) and a Local Generation diagnostics line
(availability is reported without initializing any model when disabled).
Controls lock/unlock consistently with the enable switch.

## 6. Files

* `app/video_merger/smart_visuals.py` (new) — slots, timing, lexical
  semantics, media index (+ incremental cache), scoring, strategy, prompt
  builder, plan building, plan application (silent video + Phase-30 image
  insertion), identity.
* `app/video_merger/image_generation.py` (new) — provider abstraction,
  `DiffusersProvider`, `SyntheticConceptProvider`, cache keys.
* `app/video_merger/models.py` — Phase-31 settings block + `MediaInfo.
  smart_visual_insertion` flag.
* `app/video_merger/youtube_outputs.py` — LF/Shorts profile resolution.
* `app/video_merger/main_project.py` — plan build/apply before the Phase-30
  pass, target extension, fail-safe try/except, fingerprint key.
* `app/video_merger/render_cache.py` — conditional `smart_visual_plan` key.
* `app/video_merger/image_timeline.py` — the Phase-30 planner now counts
  genuine source videos only and addresses positions by video ordinal, so
  interleaved smart visuals never shift generic image placement (no-op when
  Smart Visuals are off).
* `app/video_merger/command_builder.py` — final-tail `tpad` fix:
  `setpts=PTS-STARTPTS` must stay AFTER `tpad` (on the bundled FFmpeg 7.0 the
  old order silently disabled stop-clone padding, leaving the video stream a
  few frames short whenever the program tail needed padding). This also
  fixes one previously failing baseline E2E test; no test regressed.
* `app/video_merger/gui/main_window.py` — group 8 widgets, load/save,
  index build, plan preview.
* `config/example_settings.json` — new keys documented (disabled defaults).
* `tests/test_phase31_smart_visuals.py` (35), `tests/test_phase31_gui.py`
  (10), `tests/test_phase31_e2e.py` (11).

## 7. Tests & verification

* Unit + GUI: **1043 passed / 0 failed** (998 Phase-30 baseline + 35 new
  unit + 10 new GUI).
* Phase-31 E2E (real FFmpeg renders): **11/11** —
  A disabled = byte-identical historical render · B matched image, zero
  generation · C matched silent video (video-first) · D no match ⇒
  generated · E repeated sentence ⇒ one cached generation reused · F Shorts
  portrait 720×1280 generation + profile separation · G Long-Form landscape
  generation · H generation unavailable ⇒ safe fallback, render stays
  renderable · I disabled + Typewriter unchanged · J subtitles/music/
  transitions intact · K generated image with ken_burns + VHS via the
  Phase-30 chain · L video → generated → video transitions.
* Phase-30 E2E: still **12/12**.
* Full E2E matrix: 94 passed / 15 failed / 6 skipped vs. pristine `1e0375d`
  worktree: 82 passed / 16 failed / 6 skipped — **zero new failures**; the
  15 remaining are the pre-existing sandbox/shim class, one of which the
  `tpad` fix repaired. Failure-set diff: `current − baseline = ∅`.
* Real renders inspected: Long-Form 1280×720 (video clip with burned
  subtitle, matched white video section, full-frame generated gradient with
  ken_burns/vignette, green clip tail) and Shorts 720×1280 (full-frame
  portrait generated visual with portrait subtitle). No black/stretched
  frames; generated visuals contain no text/logos/watermarks; subtitles
  align over smart visual sections.

## 8. Limitations (documented, not hidden)

* The semantic matcher is lexical, not a neural embedding; synonyms outside
  the small concept map are not resolved.
* No real diffusion model runs in this sandbox (documented per spec §49);
  the synthetic backend is a demo/test art generator, explicitly labelled.
* Slot timing is proportional to sentence lengths; word-exact placement
  would require the (deliberately unused) second ASR pass.
* Insertion uses clip boundaries; when more slots than boundaries exist,
  surplus slots are skipped (reported in plan diagnostics).
