from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path
from threading import RLock

from .paths import project_root


def natural_sort_key(value: str) -> list[tuple[int, str | int]]:
    """Split a filename into comparable numeric/alphabetical segments.

    ``1.mp4, 2.mp4, 10.mp4`` must sort as 1, 2, 10 and not 1, 10, 2.
    """
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def natural_order(values: list[str]) -> list[str]:
    return sorted(values, key=natural_sort_key)


def randomize_order(values: list[str], rng: random.Random | None = None) -> list[str]:
    """Unbiased Fisher-Yates shuffle of exactly the supplied current list.

    The result must be a genuine permutation: every element stays present,
    nothing is re-added, and the shuffle never inspects filenames, hashes or
    timestamps. Use an explicit ``random.Random`` instance in tests.
    """
    result = list(values)
    rng = rng or random.Random()
    for index in range(len(result) - 1, 0, -1):
        other = rng.randrange(index + 1)
        result[index], result[other] = result[other], result[index]
    return result


class ProjectOrderStore:
    """Persist discovery history and the active manual order per input folder.

    ``first_in`` records the original discovery sequence. ``active`` is the
    exact preview/export sequence and may be changed by the GUI. A rescan keeps
    surviving entries in each sequence, removes missing entries, and appends
    genuinely new files in detector order. No alphabetical sorting occurs.

    Version-1 state (a mapping of folder paths to filename lists) is migrated
    transparently by using the old sequence for both fields.
    """

    _VERSION = 2

    def __init__(self, path: Path | None = None):
        self.path = path or (project_root() / "config" / "project_order.json")
        self._lock = RLock()

    @staticmethod
    def _folder_key(folder: Path) -> str:
        resolved = str(folder.expanduser().resolve())
        return os.path.normcase(resolved)

    @staticmethod
    def _entry(first_in: list[str] | None = None, active: list[str] | None = None) -> dict[str, list[str]]:
        return {"first_in": list(first_in or []), "active": list(active or [])}

    def _load(self) -> dict[str, dict[str, list[str]]]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                return {}
            raw_folders = value.get("folders") if value.get("schema_version") == self._VERSION else value
            if not isinstance(raw_folders, dict):
                return {}
            folders: dict[str, dict[str, list[str]]] = {}
            for key, raw_entry in raw_folders.items():
                if isinstance(raw_entry, list):
                    names = [str(name) for name in raw_entry]
                    folders[str(key)] = self._entry(names, names)
                elif isinstance(raw_entry, dict):
                    first_in = raw_entry.get("first_in", [])
                    active = raw_entry.get("active", first_in)
                    if isinstance(first_in, list) and isinstance(active, list):
                        folders[str(key)] = self._entry(
                            [str(name) for name in first_in],
                            [str(name) for name in active],
                        )
            return folders
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, folders: dict[str, dict[str, list[str]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"schema_version": self._VERSION, "folders": folders}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _reconcile(previous: list[str], detected_names: list[str]) -> list[str]:
        available = set(detected_names)
        ordered = [name for name in previous if name in available]
        known = set(ordered)
        for name in detected_names:
            if name not in known:
                ordered.append(name)
                known.add(name)
        return ordered

    @staticmethod
    def _relative_names(root: Path, paths: list[Path]) -> list[str]:
        return [path.expanduser().resolve().relative_to(root).as_posix() for path in paths]

    def order(self, folder: Path | str, detected: list[Path]) -> list[Path]:
        """Reconcile a scan and return the persisted active render order.

        A brand-new folder starts with the natural numeric/alphabetical order
        of the detected files (1, 2, 3, 10 – never 1, 10, 2, 3). Existing
        folders keep their persisted active order; new files are appended in
        detector order so manual/randomized sequences survive rescans.
        """
        root = Path(folder).expanduser().resolve()
        key = self._folder_key(root)
        detected_names = self._relative_names(root, detected)
        by_name = dict(zip(detected_names, detected))
        with self._lock:
            folders = self._load()
            entry = folders.get(key)
            if entry is None:
                first_in = list(detected_names)
                active = natural_order(detected_names)
                folders[key] = self._entry(first_in, active)
                self._save(folders)
                return [by_name[name] for name in active]
            first_in = self._reconcile(entry["first_in"], detected_names)
            active = self._reconcile(entry["active"], detected_names)
            folders[key] = self._entry(first_in, active)
            self._save(folders)
        return [by_name[name] for name in active]

    def set_active_order(self, folder: Path | str, ordered: list[Path]) -> None:
        """Persist ``ordered`` as the exact active order without re-sorting it."""
        root = Path(folder).expanduser().resolve()
        key = self._folder_key(root)
        names = self._relative_names(root, ordered)
        if len(names) != len(set(names)):
            raise ValueError("Die manuelle Reihenfolge enthält doppelte Dateien.")
        with self._lock:
            folders = self._load()
            entry = folders.get(key, self._entry(names, names))
            # Preserve history, but append entries if this method is called
            # before the first normal scan has initialized the store.
            first_in = list(entry["first_in"])
            known = set(first_in)
            for name in names:
                if name not in known:
                    first_in.append(name)
                    known.add(name)
            folders[key] = self._entry(first_in, names)
            self._save(folders)

    def reset_to_first_in(self, folder: Path | str, current: list[Path]) -> list[Path]:
        """Restore active order from stored First-In history for current files."""
        root = Path(folder).expanduser().resolve()
        key = self._folder_key(root)
        current_names = self._relative_names(root, current)
        by_name = dict(zip(current_names, current))
        with self._lock:
            folders = self._load()
            entry = folders.get(key, self._entry(current_names, current_names))
            first_in = self._reconcile(entry["first_in"], current_names)
            folders[key] = self._entry(first_in, first_in)
            self._save(folders)
        return [by_name[name] for name in first_in]

    def reset_to_default(self, folder: Path | str, current: list[Path]) -> list[Path]:
        """Restore the natural numeric/alphabetical default order.

        This deliberately returns the *natural* order of the currently present
        files (1, 2, 3, 10), never a previously randomized sequence and never
        the detector First-In history. First-In history itself is preserved.
        """
        root = Path(folder).expanduser().resolve()
        key = self._folder_key(root)
        current_names = self._relative_names(root, current)
        by_name = dict(zip(current_names, current))
        ordered = natural_order(current_names)
        with self._lock:
            folders = self._load()
            entry = folders.get(key, self._entry(current_names, current_names))
            first_in = self._reconcile(entry["first_in"], current_names)
            folders[key] = self._entry(first_in, ordered)
            self._save(folders)
        return [by_name[name] for name in ordered]

    def set_randomized_order(
        self,
        folder: Path | str,
        current: list[Path],
        rng: random.Random | None = None,
    ) -> list[Path]:
        """Persist a genuine Fisher-Yates permutation of the current files."""
        root = Path(folder).expanduser().resolve()
        key = self._folder_key(root)
        current_names = self._relative_names(root, current)
        by_name = dict(zip(current_names, current))
        shuffled = randomize_order(current_names, rng)
        with self._lock:
            folders = self._load()
            entry = folders.get(key, self._entry(current_names, current_names))
            first_in = self._reconcile(entry["first_in"], current_names)
            folders[key] = self._entry(first_in, shuffled)
            self._save(folders)
        return [by_name[name] for name in shuffled]

    def reset(self, folder: Path | str) -> None:
        """Remove all history for a folder (legacy/API maintenance operation)."""
        key = self._folder_key(Path(folder))
        with self._lock:
            folders = self._load()
            folders.pop(key, None)
            self._save(folders)


class GeneratedOutputStore:
    """Remember app-created files so they can never become later inputs."""

    def __init__(self, path: Path | None = None):
        self.path = path or (project_root() / "config" / "generated_outputs.json")
        self._lock = RLock()

    @staticmethod
    def _key(path: Path | str) -> str:
        return os.path.normcase(str(Path(path).expanduser().resolve()))

    def _load(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return [str(item) for item in value] if isinstance(value, list) else []
        except (OSError, ValueError, TypeError):
            return []

    def paths(self) -> set[str]:
        with self._lock:
            return set(self._load())

    def add(self, path: Path | str) -> None:
        key = self._key(path)
        with self._lock:
            values = self._load()
            if key not in values:
                values.append(key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
