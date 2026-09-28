# Phase 36 Implementation Report

## Scope

Phase 36 changes only the optional Smart Visual workflow. Smart Visuals OFF retains the established source fitting, subtitle, audio, transition, cache, and rendering path. No new media-generation feature or renderer was introduced.

## Absolute voiceover lock

The Phase-35 overflow was caused by fitting the normal source sequence to the complete target and then appending the net duration of inserted Smart Visuals to that target. Phase 36 removes that extension behavior for active Smart Visuals.

The resolved Smart Visual chain is now passed through `lock_timeline_duration()` after sequential placement. This function repeatedly uses the production transition resolver, removes every item wholly beyond the voiceover endpoint, trims the final intersecting item, and holds the final item only when the chain is short. The Stage-1 `timeline_target_duration` remains the concatenated voiceover duration. Legacy Phase-30 image insertion is not run as a second independent pass while the unified Smart Visual pool is active.

Invariant for the Smart Visual main timeline:

```text
resolved timeline end == concatenated voiceover duration
resolved item end <= concatenated voiceover duration
```

The real FFmpeg fixture renders 420 frames at 30 fps for a 14.0-second voiceover: exactly 14.0 seconds and zero frames beyond audio.

## Semantic sections

`app/video_merger/smart_timeline.py` now provides the immutable `SemanticSection` model and `build_semantic_sections()` pre-pass.

- Canonical forced-alignment `SpeechUnit` records remain the only timing authority.
- Adjacent sentences are grouped by lexical topic continuity and pause structure.
- Sections shorter than the preferred 5.0-second floor merge with an adjacent section.
- Long sections are divided into balanced sub-units at complete sentence boundaries.
- A 22-second coherent section is resolved as two approximately 11-second units.
- A single sentence over 15 seconds remains intact and is explicitly tagged `unsplittable_sentence`; this is the only automatic over-15-second exception.
- An entire speech program shorter than four seconds keeps normal source footage instead of emitting an illegal Smart Visual flash or extending beyond audio.

The preferred automatic visual range is 5.0–15.0 seconds. The absolute minimum is 4.0 seconds. Manual duration edits are also clamped to 4.0–15.0 seconds.

Semantic sections are serialized in the version-2 pre-render timeline trace and participate in timeline identity, so preview, validation, cache identity, and rendering consume the same section boundaries.

## Unified media pool and order modes

The existing backend index already represents images and videos as one `MediaIndexEntry` collection. Phase 36 exposes that model directly as the **Unified Media Pool** in the GUI.

- `Smart Order` uses semantic metadata scoring.
- `Randomized Order` uses the existing seeded uniqueness-aware pool selection.
- `Reload / Reshuffle Smart Order` increments the persisted nonce.
- For Smart Order, the nonce deterministically rotates through top-ranked relevant alternatives, guaranteeing a different sequence whenever another qualifying permutation exists without dropping below the semantic relevance gate.

## GUI cleanup and live refresh

The visible `7 · Image Timeline & Visual Effects` block was removed. Legacy fields and hidden compatibility widgets remain loadable so old project files round-trip safely, but active controls now live in `7 · Smart Visuals — Unified Media Pool`:

- image/video media folders;
- Smart or Randomized order;
- image transition and duration;
- image motion/zoom;
- image scaling (`fit`, `fill`, `crop`);
- image visual effect and intensity;
- audio-locked B-roll replacement behavior;
- index, timeline analysis, reshuffle, and manual review controls.

Geometry-affecting Smart Visual and project-transition changes invalidate timeline confirmation and schedule a debounced recalculation. The GUI thread first snapshots all widget state; a dedicated `SmartTimelineWorker` then performs the pure indexing/planning work on a `QThread`, coalescing further edits to the latest pending request. Only the resulting immutable plan is returned for GUI display, so the worker never reads a Qt widget. Rendering still performs the authoritative alignment-backed recalculation and validates the final production transition geometry before FFmpeg starts.

## Test coverage

`tests/test_phase36_timeline_fixing.py` covers:

1. strict timeline/voiceover endpoint equality;
2. no resolved item beyond the voiceover endpoint;
3. the 4.0-second hard minimum and 15.0-second maximum;
4. balanced 22-second section splitting at sentence boundaries;
5. short-section merging;
6. the documented unsplittable-sentence exception;
7. GUI Block-7 removal and unified controls;
8. Smart Order reshuffle diversity with relevance preserved;
9. widget-free `QThread` background recalculation wiring.

The real FFmpeg E2E fixture additionally verifies:

- voiceover duration: 14.000000 seconds;
- rendered timeline: 14.000000 seconds / 420 frames at 30 fps;
- zero frames after voiceover: true;
- one merged 13.0-second semantic visual;
- planned visual interval: 0.500000–13.500000 seconds;
- measured visual interval: 0.533333–13.500000 seconds;
- maximum drift: one frame / 0.033333 seconds;
- word-boundary synchronization: true;
- transition chain: verified;
- canonical subtitle alignment reuse: true.

Evidence is stored in `PHASE_36_E2E_RESULTS.json` and `PHASE_36_E2E_RESULTS.txt`.

## Validation status

- Phase-36 + protected semantic/timeline unit set: **57 passed**.
- Phase-31 through Phase-36 focused non-GUI/real-render set: **212 passed**; three additional Typewriter tests are unavailable locally because this Linux host lacks `libGL.so.1`, not because of a product failure.
- Broad local non-E2E run: **1125 passed, 8 skipped, 141 deselected**; remaining failures/setup errors are the known Linux PySide6/Qt host dependency limitation.
- Real Phase-36 FFmpeg E2E: **1 passed**.
- Transition render probe: `tests/test_transition_renders.py`: **5 passed**. The larger transition invocation reached its known long-running cross-dissolve/music case and was terminated at the 900-second local limit; native Windows regression remains authoritative for that path.

Native Windows results are recorded after the final pushed commit is exercised by `.github/workflows/phase35-windows-regression.yml` (the workflow name is retained for compatibility, but it runs the complete repository suite).
