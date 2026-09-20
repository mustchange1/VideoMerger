# Phase 30 – Image Timeline & Visual Effects (Delivery Report)

Baseline: commit `61e99c6` (Phase 29, with Phase 28 `20bf979` beneath it).
Branch: `arena/01a091fe-videomerger`. `main` untouched. Strictly additive:
with the feature disabled (the default) every historical render, cache
identity and behavior is byte-identical.

The final commit of this delivery sits directly on top of `61e99c6`
(parent = `61e99c6`); its exact hash is the tip of
`origin/arena/01a091fe-videomerger` after this push (state it in the chat
delivery report and verify with `git log --oneline -1`).

## 1. Overview

Phase 30 adds an optional, fully isolated **Image Timeline & Visual
Effects** system. Images become **genuine timeline elements between normal
video clips** (`A → B → Image → C → Image → D`), each with its own
duration, motion and an optional image-only TV effect, plus one optional
**global TV overlay** applied exactly once above the complete program.
Long-Form and Shorts own strictly separate profiles (folders, rules,
effects) that never leak into each other.

## 2. Architecture

New module `app/video_merger/image_timeline.py` (single owner of the
feature):

* **Folder scanning** – PNG/JPG/JPEG/WEBP/BMP (minimum formats), Unicode
  paths preserved, unsupported files/folders ignored gracefully,
  deterministic natural ordering.
* **`ImagePool`** – seeded `random.Random`; one full shuffled pass per
  image before reshuffle, never the same image twice in a row (reshuffle
  guard), fully deterministic per seed.
* **Planner** – `disabled` (default) / `every_n` (insert after every N-th
  video occurrence, never after the last) / `percentage` (target share of
  timeline elements, evenly spread with seeded jitter, no clusters).
  `min_video_gap` (default 2, minimum 1) prevents `Video→Image→Image`
  unless unavoidable; images never come first or last.
* **MediaInfo factory** – inserts synthetic `is_image_insertion=True`
  entries into the fitted sequence **after** `fit_media_to_duration`, so
  fitting, Hold-Last-Frame, Full-Timeline-Loop and every Phase-27/28/29
  continuity safeguard never see the images; the authoritative Stage-1
  target is extended by exactly the net added image time (chain
  computation), so the complete mixed program plays to its final frame.
* **Deterministic seed** – derived from export mode, video-order mode,
  video-order seed and folder list; every shuffle/jitter/duration draw is a
  pure function of it.

Pipeline order (spec §14): image → AR-preserving cover fit (same
`force_original_aspect_ratio=increase` + center-crop family the production
already uses, no second scaling system) → motion (deterministic `zoompan`
pass over one still, driven purely by the output frame counter) →
image-only TV effect → existing transition engine at every boundary →
global overlay as ONE post-render pass over the finished program →
optional Typewriter intro prepend (unchanged).

### Command builder (`command_builder.py`)

* Phase-30 timeline images take a dedicated path: cover-fit + `zoompan`
  motion + `tv_effect_chain()`; Stage-2 Add Image keeps its historical
  framing verbatim (moved byte-identical into `_stage2_image_framing`),
  verified by `test_stage2_add_image_graph_unchanged_by_phase30`.
* Motion kinds: `none | zoom_in | zoom_out | ken_burns | pan` on a 12%
  overscanned frame – never reveals empty space, never distorts.
* Image-only TV effects: `off | crt_scanlines | vhs | broadcast` via pure
  `t/T, X, Y` expressions (scanline geq, deterministic noise + slight
  desaturation, time-based interference band). Intensity 0–100 (default
  20), flicker speed `slow|normal|fast` (default normal).
* Video occurrences never receive image motion or TV effects (unit +
  E2E assertions on the generated graph and on real frames).
* Inputs: motion images feed ONE still frame into `zoompan` (no `-loop`);
  all other image inputs keep the historical `-loop 1` path.

### Main project (`main_project.py`)

* Insertion hook in `create_main` after the voiceover fit; the net image
  time extends `timeline_target_duration` (authoritative Stage-1 endpoint),
  keeping voiceover/music/subtitle timing untouched while the program
  plays out completely.
* **Global TV overlay**: `_apply_global_tv_effect` – one post-render pass
  per output variant, video re-encoded with the project encoder/quality,
  **audio stream-copied byte-for-byte**, applied before the Typewriter
  hook. `off` (default) never reaches the path.
* `stage1_fingerprint` receives two optional digests: `timeline_images`
  (only when ≥1 image was inserted) and `global_tv_effect` (only when not
  `off`) – historical payloads/digests stay byte-identical when disabled.

### Profiles & settings (`models.py`, `youtube_outputs.py`)

* Canonical per-job fields: `timeline_image_folders/_mode/_every_n/
  _share_percent/_min_video_gap/_duration_mode/_duration/_duration_min/
  _duration_max/_motion/_effect/_effect_intensity/_flicker_speed` plus
  `global_tv_effect/_intensity/_flicker_speed`.
* Long-Form storage: `long_form_image_folders` (+ canonical fields);
  Shorts storage: `shorts_image_folders` + `shorts_image_*` +
  `shorts_global_tv_*`. `long_form_settings()` / `short_settings()` map
  each profile onto the canonical fields; the other profile is never read.
  Safe defaults everywhere: folders empty, mode disabled, duration 2.5 s,
  motion zoom_in, effects off / 20 %, min gap 2, every_n 4, share 20 %.
* New per-item `MediaInfo` fields (`image_timeline_insertion`,
  `image_motion`, `image_effect`, `image_effect_intensity`,
  `image_flicker_speed`) default to inert values.

### GUI (`gui/main_window.py`)

Group **"7 · Image Timeline & Visual Effects"** with two strictly separate
sections (Long-Form / Shorts), each with:

* Image Sources list + **Add Image Folder / Remove / Move Up / Move Down /
  Clear** (same philosophy as the video folder lists).
* Insertion mode (Disabled / Every N Videos / Random Percentage), Every-N
  spin, share %, minimum video gap.
* Duration mode (Fixed / deterministic Random Range), presets
  1.0/2.0/2.5/3.0/4.0/5.0 s + custom spin, range min/max.
* Motion combo, image TV effect combo + intensity + flicker speed.
* Global TV overlay combo + intensity + flicker speed.
* **Preview** button: renders one REAL frame with production geometry
  (16:9 for Long-Form, 9:16 for Shorts) – cover fit + motion at 60 % +
  selected effect – shown in-section (with duration/motion/effect/AR
  caption) and double-clickable for a large full-size preview dialog.

### Cache identity (spec §24)

* `_media_payload` extends only for items flagged
  `image_timeline_insertion`; every historical payload shape is unchanged.
* Stage-1 keys `timeline_images` / `global_tv_effect` exist only when the
  feature is active; ASR/alignment/subtitle cache keys never contain them.
* E2E verifies disabled-with-folders and enabled-without-folders renders
  stay **byte-identical** to the historical output.

## 3. Transitions & continuity

* Images reuse the EXISTING transition engine; the project transition is
  applied at video↔image boundaries (per-item `image_transition_type`),
  including Smooth Blur; `safe_transition_durations` caps blends at ≤45 %
  of the shorter neighbour (existing safeguard, numerically tested with
  image entries).
* `_same_source` never matches images, so continuity safeguards treat them
  as distinct sources; no A→A replays, no duplicate segments, exact
  partition coverage (unit tests on `resolve_export` with images).

## 4. Subtitles / music / voiceover / typewriter

* Subtitles: exact timing unaffected by images (E2E compares SRT bytes of
  video-only vs image renders); burned captions render over image sections.
* Music: continues across image boundaries without restart/duplicate
  (E2E frequency probe inside an image section).
* Voiceover/ASR/alignment: purely visual feature; aligner input unchanged.
* Typewriter: hook stays a separate prepended stage; the global overlay is
  applied before it, on the program only.

## 5. Tests

* Unit + GUI suite: **998 passed / 0 failed** (959 historical + 39 new
  Phase-30 tests: scanning, pool, planner, duration modes, migration,
  LF/Shorts isolation, MediaInfo factory, command-builder chains,
  continuity math, cache identity, example-settings validation, GUI
  controls/defaults/round-trip).
* Phase-30 E2E (`tests/test_phase30_e2e.py`, real FFmpeg renders):
  **12 passed** – disabled byte-identity; images between videos (count,
  duration, visual); zoom motion visible; image effect confined to images;
  broadcast effect on images + as global overlay; global overlay changes
  program once with audio copied; video↔image transitions + determinism
  (byte-identical re-render); subtitle timing identical over images; music
  continues across images; Shorts byte-identity, own pool/geometry/duration
  and pan motion.
* Full E2E matrix: **80 passed / 18 failed / 6 skipped** – the 18 failures
  are byte-identical to a pristine run at baseline `61e99c6` (proven by
  executing the whole E2E suite in a `61e99c6` worktree in this
  environment: same 18 failures, 68 passed there). They are sandbox/ffprobe
  shim quirks (drawtext-free ffmpeg, missing Windows-path tools), not
  regressions.

## 6. Real-render evidence

Inspectable artifacts were produced in `temp/phase30_inspection` (kept out
of Git):

* **Long-Form** `out_lf/MainVideo_16x9.mp4` – 1280×720 @ 30 fps, 8.10 s:
  6 real clips + voiceover + burned subtitles + one timeline image
  (every_n=2, 2.0 s, Slow Zoom In, Analog VHS 30 %) + global CRT Scanlines
  25 %; SRT and clean master produced. Verified frames: video section with
  burned subtitle; image section shows the SMPTE-bars still cover-fitted
  with VHS grain.
* **Shorts** `out_combined/Shorts/001.mp4` – 720×1280 @ 30 fps, 7.80 s:
  own pool (percentage 25 %, Ken Burns, Broadcast 25 %) + global VHS 20 %;
  vertical geometry verified; image sections fill the full 9:16 frame.
* E2E frame probes additionally prove motion (zoom/pan), effect
  visibility, image-only scoping and byte-identical determinism.

## 7. Diff / silent-drop audit

`git diff` at commit time: +insertions across 7 tracked files plus 3 new
test files and this document. Every deleted line is accounted for:

* command_builder: the historical Stage-2 framing block was **moved
  verbatim** into `_stage2_image_framing()` (behavior byte-identical,
  proven by dedicated test + green historical Stage-2 E2E); the loop-input
  line was wrapped in a feature-gated conditional with unchanged else path.
* main_project: `target` replaced by `effective_target` (== `target` when
  no images are inserted).
* gui: Qt import line extended (QPixmap added).
* example_settings.json re-serialized with all 142 historical keys present
  (validated field-by-field against the real model).

No historical line was dropped without an equivalent preserved path.

## 8. Known limitations

* Image TV effects are deliberately subtle at the default 20 % (production
  quality); at very high intensities readability of burned subtitles can
  degrade – inherent to the feature and documented in the UI tooltips.
* Motion renders through `zoompan` (the standard deterministic still-image
  motion filter); very high overscan (12 %) means a slight upscale of the
  still, matching typical Ken-Burns quality.
* The 18 pre-existing sandbox E2E failures (identical at pristine
  `61e99c6`) remain out of scope – proven environment-specific.
* Preview renders one representative frame; continuous preview playback is
  intentionally out of scope (the production render is the preview).

## 9. Files changed

* `app/video_merger/image_timeline.py` (new)
* `app/video_merger/models.py`
* `app/video_merger/command_builder.py`
* `app/video_merger/main_project.py`
* `app/video_merger/youtube_outputs.py`
* `app/video_merger/render_cache.py`
* `app/video_merger/gui/main_window.py`
* `config/example_settings.json`
* `tests/test_phase30_image_timeline.py` (new)
* `tests/test_phase30_gui.py` (new)
* `tests/test_phase30_e2e.py` (new)
* `PHASE30_DELIVERY.md` (new)
