# Phase 35 Implementation Report

## Scope and isolation

Baseline: VideoMerger 1.5.0 at `ffc7f37`. Phase 35 is confined to optional Smart Visuals. With Smart Visuals disabled, canonical speech planning, source splitting, timeline validation, trace output, and Phase-35 rendering flags are not evaluated. Zero/default model fields preserve the normal render graph and cache identity.

## Implemented workflow

1. Active Smart Visuals reuse canonical voiceover/script alignment (the subtitle stage reuses the same result).
2. Script punctuation and aligned words produce explicit speech units.
3. Existing semantic metadata still selects **what** to show; speech selects **when**; production render-chain geometry selects **where**.
4. Automatic placements are resolved sequentially against the current transition-aware timeline.
5. Source clips may be split on the output-frame grid; nonzero source offsets emit explicit video/audio trims.
6. Auto/Hybrid/Manual state and overrides are independent for Long-Form and Shorts. Hybrid/manual rendering requires confirmation.
7. Validation and `PHASE_35_DEBUG_TRACE.json/.txt` are produced before rendering.

## Automatic duration contract

Automatic speech-anchored visuals are always 4.0–10.0 seconds. The next retained sentence/topic boundary determines natural duration; boundaries less than four seconds after the preceding retained boundary are deferred; long topics cap at ten seconds; short/final speech tails hold for four seconds. Manual overrides remain on their separate safe range.

## Render-chain correction

Real FFmpeg 6 verification exposed an edge where a short source-clock fragment ended between decoded frames. In a chained xfade this could collapse output to a later input. Phase-35 chains now:

- require six output frames (at least 0.2 seconds) per source fragment;
- pad and re-trim marked source fragments after playback-rate conversion;
- use FFmpeg's native, offset-gated `fade` xfade for cross-dissolves in marked speech chains.

These changes are gated by `smart_visual_audio_anchored`; ordinary and Smart-Visual-disabled graphs remain unchanged. Other transition styles remain on their existing custom expressions.

## Real rendered E2E result

FFmpeg/FFprobe: 6.0-static. Output: 30 fps. Three automatic visuals were planned and all three were detected in decoded final MP4 frames.

| Visual | Requested boundary | Planned interval | Measured interval | Start drift | End drift |
|---|---:|---:|---:|---:|---:|
| forest | 0.500000 s | 0.500000–4.500000 s | 0.533333–4.500000 s | +0.033333 s / +1 frame | +0.000000 s / 0 frames |
| ocean | 4.500000 s | 4.520000–9.520000 s | 4.533333–9.500000 s | +0.013333 s / 0 rounded frames | -0.020000 s / -1 frame |
| city | 9.500000 s | 9.540000–13.540000 s | 9.566667–13.533333 s | +0.026667 s / +1 frame | -0.006667 s / 0 rounded frames |

Maximum measured drift from planned geometry: **1 frame / 0.033333 seconds**. All xfade events were anchored to aligned sentence/first-word boundaries; no event began inside another spoken word. Exact machine-readable evidence is in `PHASE_35_E2E_RESULTS.json` and `.txt`.

## Test results

- Phase-35 duration, timeline, isolation, real E2E, migrated Phase-34 render, and transition-expression focus: **43 passed**.
- Automatic-duration policy: **6 passed**.
- Linux non-E2E equivalent of the Windows unit command: **1119 passed, 7 failed, 7 skipped, 11 setup errors, 141 deselected**. All failures/errors require unavailable Linux `libGL.so.1`/PySide6/pytest-qt GUI support; collection with plugin autoload fails for the same host dependency.
- Real transition regression probe: **12 passed, 5 failed**. These pre-existing direct-engine tests use the current default `duration_before_merge=0.7` while asserting raw-speed timing, producing four 2.0-vs-1.3-second mismatches and one stale transition sample window. Phase-35 focused transition rendering passes.
- The repository Windows workflow runs `setup_windows.ps1` and the complete `test_windows.ps1` suite; its run result is recorded in the final completion report.
