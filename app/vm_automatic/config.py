"""VM Automatic configuration (independent of the VideoMerger master config).

The VideoMerger master configuration lives in ``config/settings.json``
(:class:`app.video_merger.settings_store.SettingsStore`) and is treated by
VM Automatic as READ-ONLY. This module only stores the automation-specific
settings: folders, watcher/stability timing, queue policy and safe defaults.

Defaults (safe, per the VM Automatic specification):

* watcher:      OFF until the user enables it (Start Watching)
* reconciliation: 300 seconds (configurable)
* stability:    8 seconds (configurable, sensible 5–15 s range)
* retries:      limited (max 3 attempts by default)
* concurrency:  1 (sequential)
* archive:      OFF (inputs are never deleted or moved by default)
* Windows autostart: OFF
* subtitle debug overlay / continue after alignment warning: NOT touched
  here at all – the VideoMerger master settings are authoritative (both
  default OFF and stay OFF unless the user changes VideoMerger itself).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..video_merger.errors import VideoMergerError
from ..video_merger.paths import project_root

SCHEMA_VERSION = 1

WORKFLOW_AUTO = "auto"        # one-click when Intro/Outro/Quote configured, else Main Video
WORKFLOW_MAIN = "main"        # CREATE MAIN VIDEO only
WORKFLOW_COMPLETE = "complete"  # CREATE FINAL VIDEO – ONE CLICK

OUTPUTS_AUTO = "auto"         # follow the master aspect
OUTPUTS_LONG = "long_form"    # 16:9
OUTPUTS_SHORTS = "shorts"     # 9:16
OUTPUTS_BOTH = "both"         # 16:9 and 9:16


def _config_dir() -> Path:
    return project_root() / "config" / "vm_automatic"


@dataclass(slots=True)
class VMAutomaticConfig:
    # Folders (absolute paths; resolved against the project root defaults).
    watch_folder: str = ""      # inbox for audio/script jobs
    output_folder: str = ""     # VM Automatic render output
    clip_pool_folder: str = ""  # the VideoMerger clip library (default <root>/input)

    # Workflow / outputs (see constants above).
    workflow: str = WORKFLOW_AUTO
    outputs: str = OUTPUTS_AUTO

    # Timing.
    reconciliation_seconds: float = 300.0   # secondary safety scan (default 5 minutes)
    stability_seconds: float = 8.0          # file must be unchanged for this long
    stability_interval_seconds: float = 0.5
    debounce_seconds: float = 2.0           # collapse burst filesystem events
    polling_fallback_seconds: float = 1.0   # only used when watchdog is unavailable

    # Queue / retry policy.
    max_attempts: int = 3
    concurrency: int = 1                    # fixed at 1 for now (sequential)
    paused: bool = False

    # Job completeness: the canonical VideoMerger workflow is Voiceover +
    # Script, therefore a job is READY only when BOTH are stable (default,
    # exactly the documented Topic_001.mp3 → WAITING → +Topic_001.txt → READY
    # behavior). Set False to also accept voiceover-only jobs (rendered
    # without subtitles, as the plain VideoMerger pipeline does).
    require_script: bool = True

    # Data safety.
    archive_inputs: bool = False            # move finished inputs to archive_folder
    archive_folder: str = ""
    job_subfolders: bool = True             # <output>/<JobID>/ keeps outputs unambiguous

    # Optional Windows startup (explicitly user-enabled, never automatic).
    windows_autostart: bool = False

    # Extra temporary-file markers to ignore (in addition to the built-in set).
    extra_ignore_markers: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def defaults(cls, root: Path | None = None) -> "VMAutomaticConfig":
        base = (root or project_root()).resolve()
        return cls(
            watch_folder=str(base / "vm_inbox"),
            output_folder=str(base / "output"),
            clip_pool_folder=str(base / "input"),
        )

    # ------------------------------------------------------------------ #
    def resolved_watch_folder(self, root: Path | None = None) -> Path:
        return Path(self.watch_folder or (root or project_root()) / "vm_inbox").expanduser().resolve()

    def resolved_output_folder(self, root: Path | None = None) -> Path:
        return Path(self.output_folder or (root or project_root()) / "output").expanduser().resolve()

    def resolved_clip_pool_folder(self, root: Path | None = None) -> Path:
        return Path(self.clip_pool_folder or (root or project_root()) / "input").expanduser().resolve()

    def resolved_archive_folder(self, root: Path | None = None) -> Path:
        base = self.archive_folder or str((root or project_root()) / "vm_archive")
        return Path(base).expanduser().resolve()

    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """Reject configurations that would violate the data-safety rules."""
        for label, value in (("workflow", self.workflow), ("outputs", self.outputs)):
            allowed = {WORKFLOW_AUTO, WORKFLOW_MAIN, WORKFLOW_COMPLETE} if label == "workflow" \
                else {OUTPUTS_AUTO, OUTPUTS_LONG, OUTPUTS_SHORTS, OUTPUTS_BOTH}
            if value not in allowed:
                raise VideoMergerError(f"VM Automatic: ungültiger Wert für {label}: {value!r}")
        if self.reconciliation_seconds < 10:
            raise VideoMergerError("VM Automatic: Reconcile-Intervall muss mindestens 10 Sekunden betragen.")
        if not (0.5 <= self.stability_seconds <= 300.0):
            raise VideoMergerError("VM Automatic: Stabilitätsdauer muss zwischen 0.5 und 300 Sekunden liegen.")
        if self.max_attempts < 1:
            raise VideoMergerError("VM Automatic: max_attempts muss mindestens 1 sein.")
        if self.concurrency != 1:
            raise VideoMergerError("VM Automatic: Parallel Rendering ist nicht verfügbar (concurrency muss 1 sein).")
        watch = self.resolved_watch_folder()
        output = self.resolved_output_folder()
        if watch == output:
            raise VideoMergerError(
                "VM Automatic: Watch-Ordner und Output-Ordner dürfen nicht identisch sein "
                "(Ausgaben dürfen niemals als Eingaben watched werden)."
            )
        try:
            if watch in output.parents or output in watch.parents:
                raise VideoMergerError(
                    "VM Automatic: Watch-Ordner und Output-Ordner dürfen keine Unterordner "
                    "voneinander sein (Output → Watcher → neuer Job → Output wäre eine Endlosschleife)."
                )
        except VideoMergerError:
            raise
        if self.archive_inputs:
            archive = self.resolved_archive_folder()
            if archive == watch or archive in watch.parents or watch in archive.parents:
                raise VideoMergerError(
                    "VM Automatic: Der Archive-Ordner muss außerhalb des Watch-Ordners liegen."
                )

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | None = None) -> "VMAutomaticConfig":
        path = path or (_config_dir() / "config.json")
        base = cls.defaults()
        if not path.is_file():
            return base
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            allowed = {item.name for item in fields(cls)}
            values = {key: value for key, value in data.items() if key in allowed}
            for field_name in ("watch_folder", "output_folder", "clip_pool_folder", "archive_folder"):
                if not values.get(field_name):
                    values[field_name] = getattr(base, field_name)
            return cls(**values)
        except (OSError, ValueError, TypeError):
            return base

    def save(self, path: Path | None = None) -> None:
        path = path or (_config_dir() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    # ------------------------------------------------------------------ #
    def summary_lines(self) -> list[str]:
        root = project_root()

        def rel(path: Path) -> str:
            resolved = Path(path).resolve()
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                return str(resolved)

        return [
            f"Watch: {rel(self.resolved_watch_folder())}",
            f"Output: {rel(self.resolved_output_folder())}",
            f"Clip Pool: {rel(self.resolved_clip_pool_folder())}",
            f"Workflow: {self.workflow} · Outputs: {self.outputs}",
            f"Reconcile: {int(self.reconciliation_seconds)} s · Stability: {self.stability_seconds:g} s · "
            f"Max attempts: {self.max_attempts}",
        ]
