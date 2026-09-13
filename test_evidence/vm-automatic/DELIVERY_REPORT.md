# VM Automatic – Delivery Report

Date: 2026-09-13 (UTC)
Branch: `arena/01a09941-videomerger` (session-pinned by the Arena environment;
the task-requested name `arena/vm-automatic` could not be used because this
session is tracked by `arena/01a09941-videomerger`).

## Base / final commits

* Base commit: `1db2e915fbfc9bdf96f5e49aec5bb5d480966f8a`
  ("Merge pull request #1 … VideoMerger 1.3.0").
  NOTE: the task referenced baseline hash `bb5470d841ec7d5b159e0303c6610ac6dc0a6283`,
  which does not exist in this repository (verified locally, after fetch, and via
  for-each-ref). The repository contains exactly one commit – the squashed 1.3.0
  baseline – which is what was verified (see `BASELINE.md`).
* Final commit: `d8e4c1d` ("Add VM Automatic (Video Merger Automatic)
  companion automation") on `arena/01a09941-videomerger`, pushed to
  `mustchange1/VideoMerger`.
* No history rewrite, no rebase, no force-push. `main` untouched.

## Files changed / added

Changed (1):
* `requirements.txt` – added `watchdog>=4,<7` (event-driven file watching).

Added (19 new files, none of the existing VideoMerger files modified):
* `app/vm_automatic/__init__.py` – package + version
* `app/vm_automatic/__main__.py` – entry point (`python -m app.vm_automatic`)
* `app/vm_automatic/applock.py` – single-instance lock (cross-platform)
* `app/vm_automatic/config.py` – VM Automatic settings (own file, never the master config)
* `app/vm_automatic/controller.py` – headless automation core (supervisor + worker threads)
* `app/vm_automatic/identity.py` – job identity rules (deterministic audio/script pairing)
* `app/vm_automatic/logging_setup.py` – VM Automatic logger (rotating file + listeners)
* `app/vm_automatic/pairing.py` – watch-folder scan → job candidates (conflict detection)
* `app/vm_automatic/queue.py` – persistent sequential queue (1 job at a time, retries)
* `app/vm_automatic/randomization.py` – job-local, seed-replayable clip-order randomization
* `app/vm_automatic/recovery.py` – crash recovery (RUNNING → INTERRUPTED → retry)
* `app/vm_automatic/runner.py` – job execution via the EXISTING VideoMerger pipeline
* `app/vm_automatic/state.py` – persistent job states + atomic JSON store
* `app/vm_automatic/stability.py` – file stability detection (size/mtime + lock probe)
* `app/vm_automatic/watcher.py` – watchdog (primary) / 1 s polling fallback watcher
* `app/vm_automatic/gui.py` – light GUI (folders, status, queue, jobs, logs, all controls)
* `app/vm_automatic/windows_startup.py` – opt-in Windows autostart (HKCU Run)
* `VM Automatic starten.cmd` – one-click Windows start (UTF-8 BOM, CRLF)
* `run_vm_automatic.ps1` – Windows run script (BOM+CRLF; venv/ffmpeg checks, env setup)
* `setup_vm_automatic.ps1` – Windows setup script (BOM+CRLF; venv, deps, selftest, optional shortcut)
* `README_VM_AUTOMATIC_DE.md` – German user documentation (starts with "SO STARTEN SIE VM AUTOMATIC")
* 9 test files: `tests/test_vm_automatic_{identity,stability,queue,random,watcher,outputs,flow, windows_assets}.py` + `tests/test_vm_automatic_e2e.py`

Test evidence: `test_evidence/vm-automatic/BASELINE.md` (baseline verification) and this report.

## Test results (full suite, this environment)

Non-E2E (`-m "not e2e and not benchmark"`):
    2 failed, 319 passed, 87 deselected

E2E (`-m "e2e and not benchmark"`):
    72 passed, 14 skipped, 322 deselected

* The 2 non-E2E failures are PRE-EXISTING baseline failures (verified before any
  VM Automatic code existed, see `BASELINE.md`):
  1. `test_windows_setup_assets.py::test_setup_selftest_includes_real_subtitle_workflow_fixture`
     – requires gitignored binary evidence (`test_evidence/1.2.2/subtitle_workflow/assets/*`)
     that is not present in the squashed repository.
  2. `test_windows_setup_assets.py::test_windows_powershell_scripts_use_utf8_bom_crlf_and_intact_german`
     – the committed `.ps1` blobs lost their CRLF line endings in the repository squash.
  Neither failure involves VM Automatic; both fail identically on the untouched baseline.
* 14 E2E skips are environment-limited: real ASR model tests (Hugging Face
  unreachable in this sandbox) and drawtext-capable FFmpeg build tests.
* All 70 new VM Automatic unit/integration tests pass, including:
  * deterministic pairing + ignore rules (16),
  * stability incl. lock detection (8),
  * queue: ordering, duplicates, retries, reseed, pause, persistence, crash recovery (12),
  * randomization: seed determinism, full permutation, master-order preservation (11),
  * watcher: real watchdog events, debounce collapse, pre-existing files ignored (7),
  * output validation: exists / non-zero / readable / stable (5),
  * full controller flow with fake runner: detect → stability → READY → QUEUED →
    RUNNING → SUCCEEDED, per-job seeds, retry policy, crash restart, voiceover-only (6),
  * Windows asset integrity: BOM+CRLF, README headline, watchdog dependency (6).

## Real end-to-end (actual FFmpeg renders through the existing pipeline)

`tests/test_vm_automatic_e2e.py` (both pass):
1. `test_real_automation_flow_with_existing_renderer`
   – Job A: voiceover-only job detected in the inbox, stabilized, rendered with the
     existing VideoMerger engine, output verified (exists, non-zero, readable, stable);
     randomized clip order recorded (5-clip pool, full permutation).
     Job B: fresh seed → different clip order.
     Rescan: no re-run of SUCCEEDED jobs (attempts stay 1, outputs unchanged).
     Restart: successes persist, new Job C is processed automatically.
2. `test_slow_copy_is_not_started_early`
   – a 2 s WAV copied in 32 slow chunks: the job is observed as NEW /
     WAITING_FOR_STABILITY during the copy and only starts after the file is
     fully copied AND the stability window has passed; renders to success.

## Feature list (all defaults per spec)

* Watcher OFF until the user enables it (GUI button / startup flag).
* Event-driven watching (watchdog) as primary; 1 s polling fallback when watchdog
  is unavailable; 300 s reconciliation scan as resilience safety net; immediate
  scan at startup (never waits 5 minutes for the first detection).
* Job detection by deterministic pairing (`Topic_001.mp3` + `Topic_001.txt` → job
  `Topic_001`); conflict detection (extra same-stem files are warned + ignored);
  temp/hidden/generated names ignored; own outputs can never become inputs
  (GeneratedOutputStore).
* File stability: size/mtime quiescence (sensible 5 s default, configurable, no
  arbitrary long delays, no hashing of large files) + best-effort exclusive-lock
  probe; slow copies are never started early.
* Persistent sequential queue: one job at a time; deterministic order
  (001 → 002 → 003); duplicate protection (job identity = normalized stem;
  SUCCEEDED jobs are never re-processed).
* Job-local clip randomization: fresh seed per new job, retries reuse the seed,
  "Retry + Randomize Again" reseeds; the master/manual VideoMerger clip order is
  never modified (job-local execution state only).
* Job states: NEW / WAITING_FOR_STABILITY / READY / QUEUED / RUNNING / SUCCEEDED /
  FAILED / RETRY_PENDING / INTERRUPTED, full per-job state history, persisted
  atomically (survives crashes).
* Crash recovery: RUNNING at startup → INTERRUPTED → automatic retry while
  attempts remain; a job is never marked SUCCEEDED after a crash.
* Limited automatic retries (default 3, configurable); manual Retry and
  Retry+Randomize Always available from the GUI.
* Rendering 100 % via the existing VideoMerger pipeline (VideoMergerEngine +
  MainProjectEngine + FFmpeg). VM Automatic changes only the job input and the
  job-local clip order. Every other setting (subtitles, music incl. multi-track
  and Long/Shorts separation, transitions, quality, resolution, fps, codecs,
  watermark, intro/outro, image insertion, Duration Before Merge, loop/hold
  modes, ducking, subtitle style/language) stays authoritative from the
  untouched master configuration.
* Output validation before SUCCEEDED: files exist, non-zero, readable, and
  stable after a re-check; negative validation never marks success.
* Inputs are never deleted by default (optional archiving, OFF by default).
* Single-instance lock; structured log file + in-GUI log view; GUI with folders,
  watcher status, queue, jobs, logs and controls (Start/Stop Watching, Scan Now,
  Pause/Resume Queue, Retry, Retry+Randomize Again, open folder/log).
* Windows startup option (opt-in, HKCU Run), `VM Automatic starten.cmd`,
  `setup_vm_automatic.ps1`, `run_vm_automatic.ps1`, German README.
* Production defaults: Subtitle Debug Overlay OFF, Continue After Alignment
  Warning OFF (master setting authoritative); no silent background processing.

## Known issues / environment flags

1. Baseline hash `bb5470d8…` absent from the repository – verified on the only
   available commit `1db2e915` (see `BASELINE.md`).
2. Branch name: session pinned to `arena/01a09941-videomerger` (task asked for
   `arena/vm-automatic`); the session pin is authoritative.
3. The 2 pre-existing baseline test failures listed above remain (out of scope –
   they involve repository-squash artifacts, not functionality).
4. Sandbox limitations: Hugging Face unreachable (real ASR subtitle E2E skipped),
   drawtext-capable FFmpeg build unavailable (related E2E skipped). All other
   real-render E2E tests run with the bundled FFmpeg.
5. The existing VideoMerger engine truncates the video stream when the clip pool
   does not contain more footage than the voiceover duration ("cut" duration fit);
   VM Automatic inherits this engine behavior by design (no second engine) and
   reports it as a job failure when validation is negative.
