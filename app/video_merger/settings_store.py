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
            # Accept the natural alternate spelling from early multi-folder
            # project files without exposing two GUI settings.
            if "source_folders" not in data and "input_folders" in data:
                data["source_folders"] = data["input_folders"]
            allowed = ExportSettings.__dataclass_fields__.keys()
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
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
