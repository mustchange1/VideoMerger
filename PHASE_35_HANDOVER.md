# Phase 35 Handover — Speech-Aware Smart Visual Timeline

## Status

Phase 35 is implemented and its mandatory real-render acceptance fixture passes with FFmpeg/FFprobe 6.0-static. Machine-readable and text evidence are stored at repository root in `PHASE_35_E2E_RESULTS.json` and `PHASE_35_E2E_RESULTS.txt`.

## Isolation

Phase 35 is strictly behind an active Smart Visual profile. Smart Visuals OFF does not compute speech units, resolve a Phase-35 timeline, split sources, apply overrides, validate/confirm a Smart timeline, or emit a Phase-35 trace. Normal merging remains on its existing renderer.

## Architecture

- `smart_metadata.py` and the existing `smart_visuals.py` scoring decide **what** visual is semantically appropriate.
- Canonical aligned speech units decide **when** the visual belongs.
- `smart_timeline.py` and production `resolve_export()` geometry decide **where** it is inserted in the xfade chain.
- Placements are applied chronologically and sequentially, so each later search sees every prior insertion and source split.
- `MediaInfo.source_start` preserves the source-clock origin of split fragments.
- Preview/trace and rendering consume the same resolved item geometry.

## Automatic duration policy

Automatic speech-anchored visuals obey a dedicated 4.0–10.0-second contract:

- use the next retained sentence/topic boundary as the natural end;
- defer dense boundaries that would create a visual shorter than four seconds;
- cap long topics at ten seconds;
- hold a short/final edge for four seconds;
- leave manual overrides on their separate safe range.

The policy is covered explicitly by `tests/test_phase35_duration_rule.py`.

## Manual review state

Long-Form and Shorts persist independent Auto/Hybrid/Manual mode, confirmation, and override dictionaries. Supported edits are start, duration, replacement asset, remove, and restore automatic. Overrides modify the resolved pre-render Smart Visual model, not rendered media. Hybrid/manual rendering is gated on confirmation.

## Subtitles and alignment

The supplied script stays text-authoritative and the voiceover/alignment stays timing-authoritative. When subtitles are also enabled, the subtitle stage reuses the canonical alignment prepared for active Smart Visuals; ASR is not repeated and subtitle text/timing is not modified by Phase 35.

## Render-chain edge handling

Real FFmpeg testing found that very short speech-adjacent source fragments could end between decoded source frames and starve a chained xfade. The Phase-35-only correction requires at least six output frames/0.2 seconds per source fragment, frame-pads and re-trims marked fragments after rate conversion, and uses FFmpeg's native offset-gated fade for cross-dissolve chains. The behavior is keyed by `smart_visual_audio_anchored`, included in cache identity, and absent from ordinary renders.

## Real E2E evidence

At 30 fps, all three planned automatic visuals were found in decoded final MP4 frames:

- forest: requested 0.500000 s; planned 0.500000–4.500000 s; rendered 0.533333–4.500000 s.
- ocean: requested 4.500000 s; planned 4.520000–9.520000 s; rendered 4.533333–9.500000 s.
- city: requested 9.500000 s; planned 9.540000–13.540000 s; rendered 9.566667–13.533333 s.

Maximum measured planned-vs-rendered drift is one frame (0.033333 seconds). All timeline changes are attached to aligned sentence/first-word boundaries and none begins inside another spoken word.

## Tests and known environment results

- Focused Phase-35/protected suite: 43 passed.
- Automatic 4–10-second policy: 6 passed.
- Linux non-E2E broad run: 1119 passed; 7 failures and 11 setup errors are GUI imports blocked by missing host `libGL.so.1`/pytest-qt; 7 skipped and 141 E2E deselected.
- Direct transition render probe: **17 passed**. Five stale assertions were corrected to the authoritative default `duration_before_merge=0.70` clock. Production behavior was retained: two 0.8-second clips with a 0.3-second overlap render for 1.985714 seconds, and the 0.4-second cross-dissolve occupies 0.742857–1.142857 seconds after the default speed conversion.

## Files

- `app/video_merger/smart_timeline.py`
- `app/video_merger/smart_visuals.py`
- `app/video_merger/main_project.py`
- `app/video_merger/models.py`
- `app/video_merger/command_builder.py`
- `app/video_merger/render_cache.py`
- `app/video_merger/youtube_outputs.py`
- `app/video_merger/gui/main_window.py`
- `tests/test_phase35_duration_rule.py`
- `tests/test_phase35_smart_timeline.py`
- `tests/test_phase35_isolation.py`
- `tests/test_phase35_e2e.py`
- `PHASE_35_E2E_RESULTS.json`
- `PHASE_35_E2E_RESULTS.txt`
