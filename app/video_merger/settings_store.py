from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import ExportSettings
from .paths import project_root


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (project_root() / "config" / "settings.json")

    def load(self) -> ExportSettings:
        if not self.path.is_file():
            return ExportSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ExportSettings()
            # 1.3.0 used ``video_speed`` for the only speed control. Migrate
            # that saved value once into the canonical Before Merge setting;
            # an explicitly saved value remains an explicit override.
            if "duration_before_merge" not in data and "video_speed" in data:
                data["duration_before_merge"] = data["video_speed"]
            # A project saved before the explicit visual sections carries only
            # the Main Video end padding (``final_pause``). That padding IS the
            # Long-Form outro, so migrate the saved value instead of replacing
            # the visible tail of an old project with the new default.
            if "long_form_outro_seconds" not in data and "final_pause" in data:
                data["long_form_outro_seconds"] = data["final_pause"]
            # Music volume and transition settings were split per output
            # (Long-Form and Shorts are independent now). A project saved before
            # the split carries only the shared values, so copy them into both
            # new output-specific settings: an existing project keeps exactly
            # its saved loudness and transition instead of silently jumping to
            # the new defaults, and a project without any of these keys receives
            # the new defaults (44 % / Cross Dissolve / 2.0 s for both outputs).
            # Keys that are already present are never overwritten.
            for shared, outputs in (
                ("music_volume", ("long_form_music_volume", "shorts_music_volume")),
                ("transition_type", ("long_form_transition_type", "shorts_transition_type")),
                (
                    "transition_duration",
                    ("long_form_transition_duration", "shorts_transition_duration"),
                ),
            ):
                if shared not in data:
                    continue
                for name in outputs:
                    if name not in data:
                        data[name] = data[shared]
            # Accept the natural alternate spelling from early multi-folder
            # project files without exposing two GUI settings.
            if "source_folders" not in data and "input_folders" in data:
                data["source_folders"] = data["input_folders"]
            allowed = {
                name for name, field in ExportSettings.__dataclass_fields__.items()
                if field.init
            }
            return ExportSettings(**{key: value for key, value in data.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return ExportSettings()

    def save(self, settings: ExportSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = asdict(settings)
        # Do not persist two user-facing names for one control. ``video_speed``
        # is read only as a legacy migration alias in load().
        payload.pop("video_speed", None)
        payload.pop("subtitle_output_mode_was_defaulted", None)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
