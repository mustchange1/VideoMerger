# VM Automatic – Windows Real-World Test Plan

Purpose: trustworthy end-to-end verification of VM Automatic on a real Windows PC.
Branch under test: `arena/01a09941-videomerger` (HEAD at audit time: `1cd9ef4`).
Base VideoMerger on this branch: `1db2e91` ("VideoMerger 1.3.0").

IMPORTANT SCOPE NOTE (see audit, CASE 3):
This branch is based on the 1.3.0 squash, NOT on the Phase-27 baseline
(`bb5470d`, branch `arena/01a091fe-videomerger`). Phase 23–27 features
(separate Shorts music tracks, Shorts-specific transition settings,
per-Short script groups, multi-track music, timeline areas, render cache,
YouTube export modes, speech-language pipeline) are NOT present on this
branch. Steps marked [PHASE27-ONLY] can therefore only be executed after
VM Automatic is reconciled onto the Phase-27 baseline.

Preconditions: Windows 10/11 x64, internet (first run downloads faster-whisper
model), NVIDIA GPU optional (CPU rendering works), Python 3.11–3.13.

## 0. Setup (exact commands)

```bat
:: clone (or fetch if already cloned)
git clone https://github.com/mustchange1/VideoMerger.git
cd VideoMerger
git fetch origin
git checkout -B arena/01a09941-videomerger origin/arena/01a09941-videomerger
git rev-parse HEAD        :: record exact commit tested

:: one-click setup: venv + requirements + ffmpeg check + selftest + optional shortcut
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_vm_automatic.ps1
```

Collect: `git rev-parse HEAD`, setup console output (selftest PASS lines),
`python --version` from `.venv`, `tools\ffmpeg\bin\ffmpeg -version`.

## 1. Start VM Automatic

```bat
"VM Automatic starten.cmd"
```

Expect: window titled "VM Automatic – Video Merger Automatic", status
STOPPED (watcher OFF by default – verify this: no "WATCHING" line before
pressing ▶ Start Watching).
Second launch while first is open → "läuft bereits" dialog, no second
instance (single-instance lock, repaired + regression-tested).

## 2. Configure Watch Folder

Set the Watch Folder to a fresh, empty directory, e.g. `C:\vm_test\inbox`
(create it first). Set Clip Pool Folder to a folder `C:\vm_test\pool`
containing 4–6 mp4 clips (total duration > voiceover duration, e.g.
6 × 10 s). Set Output Folder to `C:\vm_test\output`. Save.

Evidence: `config\vm_automatic\settings.json` reflects the three folders.

## 3. Start watching

Press ▶ Start Watching.
Evidence: log line "WATCHING – Watch-Ordner …", watcher backend
"watchdog" (Event-gesteuert) shown in the status area.

## 4. Provide one valid audio/script pair

`C:\vm_test\inbox\Topic_001.mp3` (any real voiceover, e.g. 15–30 s)
`C:\vm_test\inbox\Topic_001.txt` (matching script text, German)

## 5. Verify stability detection (step 7 of the required list)

Watch the job table: Topic_001 appears as NEW → WAITING_FOR_STABILITY
while the files are copied; log lines "Waiting for stability – …"; after
the stability window (default 8 s, configurable) "Datei stabil: …".
Slow-copy check: copy a second pair (Topic_002) slowly (e.g. hand-crafted
chunked copy); the job must stay in WAITING_FOR_STABILITY until the copy
finishes AND the stability window passes. It must NOT start rendering mid-copy.

## 6. Verify job creation + pairing

Job id `topic_001` (normalized stem), audio+script assigned, state trail
NEW → WAITING_FOR_STABILITY → READY in the job history (GUI details or
`config\vm_automatic\state.json`).

## 7. Verify randomization

Log line "Randomizing N eligible clips (Seed …): …" – order is a full
permutation of the pool. Render completes. Job shows `clip_order` + `seed`.
Second job (Topic_002, different pair) → different seed, different order.

## 8. Verify VideoMerger rendering + output validation

Log: "Job … → RUNNING (Versuch 1/1…)", then "Output-Validierung OK
(existiert, nicht leer, lesbar, stabil)", then "Job completed successfully."
`C:\vm_test\output\Topic_001\MainVideo_16x9.mp4` exists, non-zero,
plays, duration ≈ voiceover + configured pause.

## 9. Verify Long-Form output (step 12)

Master aspect 16:9 (default) → Long-Form MP4 as above.
SRT/VTT + no-subtitles variant next to it (subtitle output mode of the
master config is authoritative).

## 10. Verify subtitles (step 14)

Voiceover+Script assigned → subtitles auto-enabled (script-authoritative,
ASR word timing via faster-whisper). Burned-in subtitles visible in the
MP4; SRT/VTT files present and cue timings align with the voiceover
(check in a player; compare a few cue times against the SRT).

## 11. Shorts output (step 13) [BASE-FEATURE]

Set master aspect to 9:16 (or VM Automatic outputs = Shorts) and run a job.
9:16 output produced. NOTE: separate Shorts music / Shorts-specific
transition settings / per-Short script groups are [PHASE27-ONLY] and NOT
available on this branch – do not treat their absence as a VM Automatic
defect; treat their presence as a baseline question (CASE 3).

## 12. Verify logs (step 16)

`config\vm_automatic\vm_automatic.log` (rotating) contains the full trail;
GUI log view mirrors it; German messages; timestamps.

## 13. Verify job state persistence (step 17)

Close VM Automatic (clean stop). Reopen. SUCCEEDED jobs stay SUCCEEDED,
no re-processing, queue/jobs table restored from `state.json`.
Crash variant (optional, strong evidence): stop with
`taskkill /F` while a job is RUNNING, reopen → job shows
INTERRUPTED → automatic retry → SUCCEEDED (never marked SUCCEEDED after crash).

## 14. Verify no duplicate job (step 18)

With both Topic_001/002 files still in the inbox, press "Scan Now" and
wait one reconciliation cycle (5 min or lower the interval in settings).
Attemps stay 1, outputs unchanged, no new job ids for the same stems.
Drop a `Topic_001_copy.mp3` (different stem) → new job `topic_001_copy`
is created (correct: different stem = different job); extra
same-stem files (e.g. a second `Topic_001.mp3` after archive) produce a
warning, not silent duplication.

## 15. Verify no accidental source deletion (step 19)

After all jobs succeeded: `C:\vm_test\inbox` still contains
Topic_001.mp3/.txt, Topic_002.mp3/.txt unchanged (same size/mtime).
Archive OFF by default → nothing moved. (If archive is enabled later,
files move to the archive folder ONLY – never deleted.)

## 16. Master-config authority check (safety)

Before/after: hash `config\settings.json`. VM Automatic must never modify
it. Run one job, re-hash → identical. (VM Automatic writes only
`config\vm_automatic\*`.)

## Verdict rules

- PASS: every step 1–15 (+16) shows the listed evidence.
- Any step without evidence = NOT VERIFIED.
- A failed step = FAIL with log excerpt + exact state.
- Windows test results are only valid for the exact `git rev-parse HEAD`
  recorded in step 0.
