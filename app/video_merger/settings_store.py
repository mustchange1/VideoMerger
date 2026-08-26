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
            allowed = ExportSettings.__dataclass_fields__.keys()
            return ExportSettings(**{key: value for key, value in data.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return ExportSettings()

    def save(self, settings: ExportSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
