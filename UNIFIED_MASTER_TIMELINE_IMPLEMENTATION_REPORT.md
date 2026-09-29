# Unified Master Timeline, Asset Cooldown, and Media Ratio — Implementation Report

## Scope

This change replaces the voiceover-driven split between fitted source video and later Smart/image insertion with one `MasterTimelineBuilder`. Non-voiceover/basic merge workflows remain on their established generic engine.

## Architecture

### Unified `MediaPool`

`app/video_merger/media_pool.py` defines canonical `MediaPoolAsset` records and a single deduplicated pool containing:

- ordinary source videos (`MediaInfo` retained for render materialization);
- indexed pool videos;
- indexed pool images and their semantic metadata.

Canonical resolved, case-normalized paths are the persistent asset identity.

### Single `MasterTimelineBuilder`

`app/video_merger/master_timeline.py` partitions the voiceover interval exactly once, applies cooldown filtering and image/video quota allocation, assigns one eligible asset per slot, and validates:

- first slot starts at `0.0`;
- every adjacent slot is contiguous;
- the last slot ends exactly at `T_voiceover`;
- every normal slot is between 4.0 and 12.0 seconds.

Pure Random mode performs seeded draws without semantic scoring. Smart Visuals mode uses local topic/metadata relevance and assigns the constrained image slots where their semantic advantage over video is strongest. Selection remains uniqueness-first and avoids immediate reuse after pool exhaustion.

`materialize_master_timeline()` creates the final `MediaInfo` chain directly. It compensates each item for its production incoming transition overlap and then applies the existing production endpoint lock. The legacy video-duration fit is bypassed whenever the unified mode is active.

### Persistent cross-video cooldown

`app/video_merger/asset_cooldown.py` stores successful-video usage in `data/asset_cooldown_history.json` using atomic temporary-file replacement. Planning and preview only read history. Usage is committed after a successful output (including a successful cache materialization), never before rendering or after failure.

The configured N-video window is applied before either random or semantic selection. If cooldown removes every eligible asset, rendering reports an explicit planning failure rather than bypassing the lockout through the former timeline.

### Ratio allocation

Long-Form and Shorts each persist:

- cooldown video count, default 3;
- minimum image percentage, default 30;
- maximum image percentage, default 40.

The ratio is enforced against the available semantic slot count; it never invents a speech cut merely to make a percentage exactly representable. When a short timeline has no exact integer allocation in the requested range, or one media kind is absent, the builder chooses the closest feasible distribution and records a diagnostic.

## Strict pacing

The former 15-second maximum and unsplittable-sentence exception are removed. The strict range is now 4.0–12.0 seconds.

Long aligned sentences are divided at significant word pauses where possible and otherwise at balanced inferred thought boundaries. A 20–30-second topic is divided into three or four visual slots under the same semantic text/topic. Thoughts shorter than four seconds continue to merge with adjacent material.

A complete voiceover shorter than four seconds remains the unavoidable endpoint edge case: exact audio lock takes precedence over padding past the voiceover.

## GUI and persistence

The Smart Visuals panel now exposes:

- `Enable Unified Master Timeline for this profile`;
- visible `Smart Visuals Mode` and `Pure Random Mode` choices;
- `Skip used asset for next N videos`;
- minimum and maximum horizontal image-ratio sliders.

The historical `smart_inserts` value remains loadable through a hidden compatibility row and maps to Smart behavior in the Master Timeline. New values round-trip independently for Long-Form and Shorts through `ExportSettings`, GUI save/load, `youtube_outputs.short_settings()`, and `config/example_settings.json`.

## Verification

New coverage in `tests/test_unified_master_timeline.py` verifies:

1. exact voiceover endpoint and zero overflow;
2. contiguous strict 4–12-second slots;
3. 20–30-second thought splitting into three or four visuals;
4. image-ratio allocation;
5. five consecutive renders with a three-video asset lockout;
6. preview not consuming cooldown state;
7. GUI controls and per-output persistence.

Local verification completed:

- `tests/test_unified_master_timeline.py`: **7 passed**;
- unified + protected Phase-31–36 backend/settings set: **183 passed, 11 skipped**;
- the 11 skips are real-FFmpeg cases unavailable on this Linux checkout because project-local FFmpeg is absent;
- Python compilation for all production modules and GUI modules: **passed**;
- `git diff --check`: **passed**.

## Final native Windows verification

The implementation commit `8ab16cfb1c1b93eb45be93379c113541ad502677` passed the complete native Windows workflow:

- workflow: `.github/workflows/phase35-windows-regression.yml`;
- run: `36515009861`;
- job: `109235233503` (`windows-regression`);
- result: **SUCCESS**;
- complete Windows regression suite: **passed**;
- regression-summary publication: **passed**;
- evidence upload: **passed**;
- duration: **14m 37s** (2026-09-29 02:57:51–03:12:28 UTC).

The only annotation was GitHub's infrastructure warning that Node.js 20 actions are being forced onto Node.js 24. There were no application test failures or errors.

## GUI/render source-of-truth correction

The follow-up correction removes the remaining dual-planner architecture.

### Canonical snapshot contract

`MasterTimeline` now owns the single canonical serialization and SHA-256 identity algorithm. Its versioned snapshot includes ordered slots, exact boundaries, asset paths/kinds/IDs, semantic evidence, diagnostics, pool/alignment/cooldown/settings fingerprints, and confirmation state. `from_dict()` strictly validates schema, ordering, asset identity, geometry, and the stored identity.

`MediaPool.fingerprint` deterministically covers each deduplicated canonical asset and relevant metadata. `MasterTimelineRequest` is the shared plain-data request used to construct the real timeline. Slot replacement uses immutable replacement semantics: only `MasterTimelineSlot.asset` changes, start/end/duration remain byte-identical, identity changes, and confirmation is cleared.

### GUI authority

The active GUI now requires analyzed `current_media`, obtains canonical voiceover/script alignment, indexes configured image/video folders, creates one `MediaPool.unified(...)`, and invokes `MasterTimelineRequest.build()` / `MasterTimelineBuilder`. The worker returns the actual `MasterTimeline`; neither the GUI nor its worker imports or calls `build_smart_visual_plan()`.

The list rows correspond one-for-one to Master slots and store the Master slot index as Qt user data. Source videos, indexed videos, and indexed images are shown in one chronological list. The summary reports pool composition, image/video slot counts, target endpoint, canonical identity, and confirmation state.

Legacy Move Start, Change Duration, and Remove Visual operations were removed. The active editor offers only Replace Slot Asset and Restore Automatic Asset. Legacy image-duration and insert-frequency controls remain loadable but are visibly labeled inactive and disabled because slot duration comes only from canonical speech timing.

Long-Form and Shorts persist separate snapshot and identity fields. Any relevant GUI input change clears confirmation. A headless `prepare_confirmed_master_timeline_settings()` entry point performs the same explicit Analyze→Confirm step for automation; rendering never invokes it.

### Render authority and no fallback

Active unified rendering now requires the confirmed persisted snapshot. It reconstructs the current pool for resolution/validation only, validates every fingerprint and endpoint, restores the exact ordered slots, and materializes those slots without rebuilding an assignment. GUI, persisted, and render identities use the same `MasterTimeline.identity` implementation; mismatch or stale state aborts with an actionable error.

The active render module no longer imports or calls `build_smart_visual_plan()` or `apply_smart_visual_plan()`. Missing voiceover/alignment, missing snapshots, unconfirmed snapshots, stale inputs, missing assets, cooldown incompatibility, and identity mismatch are all hard failures. Disabled unified mode continues through the historical fit/render workflow and does not require a snapshot.

Runtime trace/log output now includes mode, media-pool count, slot/image/video counts, exact target, GUI identity, render identity, and confirmation state.

### Correction verification

Focused local verification at branch tip before native execution:

- canonical unified/identity/GUI-source-of-truth and protected Phase-35/36 set: **43 passed**;
- production and test Python compilation: **passed**;
- `git diff --check`: **passed**;
- native-only Qt/FFmpeg execution remained delegated to Windows because this checkout lacks `libGL.so.1` and project-local FFmpeg.

The implementation was developed in `0fdb09d35b7a6cb2d50304ed8812f0795db19ac6`, with explicit Analyze→Confirm E2E migration in `22cd80eb9ac797bcf4da5c050cdbae5288a34ac9`, and protected compatibility updates in `a9c145e3bb4c85c9a0b091befbf274efdb57e2f6`.

Native Windows branch-tip verification for `a9c145e3bb4c85c9a0b091befbf274efdb57e2f6`:

- workflow run: `36572140250`;
- job: `109418497416` (`windows-regression`);
- URL: `https://github.com/mustchange1/VideoMerger/actions/runs/36572140250`;
- result: **SUCCESS**;
- complete Windows regression suite: **passed**;
- summary publication: **passed**;
- evidence upload: **passed**;
- duration: **14m 54s** (2026-09-29 13:01:42–13:16:36 UTC).

The only annotation was GitHub's Node.js 20-to-24 infrastructure deprecation warning. No application failures or errors remained.
