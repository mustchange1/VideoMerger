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

The ratio is enforced against slot count. The builder increases the balanced slot count, without violating the four-second floor, when a small integer slot count cannot represent the requested percentage interval. If one media kind is genuinely absent, it chooses the closest feasible distribution and records a diagnostic.

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

Native Windows complete-suite verification is recorded after the implementation commit is pushed and `.github/workflows/phase35-windows-regression.yml` completes.
