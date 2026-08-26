from __future__ import annotations

import time
from pathlib import Path

from .errors import VideoMergerError
from .models import LogCallback


def _snapshot(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    result: dict[Path, tuple[int, int]] = {}
    for path in paths:
        try:
            stat = path.stat()
        except OSError as exc:
            raise VideoMergerError(f"Datei kann nicht geprüft werden: {path}: {exc}") from exc
        if not path.is_file():
            raise VideoMergerError(f"Eingabedatei fehlt oder ist keine normale Datei: {path}")
        if stat.st_size <= 0:
            raise VideoMergerError(f"Eingabedatei ist leer oder noch nicht vollständig kopiert: {path.name}")
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def wait_for_files_stable(
    paths: list[Path],
    log: LogCallback = lambda _message: None,
    interval: float = 0.6,
    timeout: float = 30.0,
) -> None:
    """Wait until size and mtime are unchanged across two observations."""
    if not paths:
        raise VideoMergerError("Keine Dateien für die Stabilitätsprüfung übergeben.")
    log(f"Prüfe Dateistabilität für {len(paths)} Eingabedatei(en) …")
    deadline = time.monotonic() + max(timeout, interval)
    previous = _snapshot(paths)
    while time.monotonic() < deadline:
        time.sleep(max(0.05, interval))
        current = _snapshot(paths)
        if current == previous:
            log("Dateistabilität: OK (Größe und Änderungszeit unverändert).")
            return
        previous = current
        log("Mindestens eine Datei ändert sich noch; warte auf Abschluss des Kopiervorgangs …")
    changing = ", ".join(path.name for path in paths)
    raise VideoMergerError(
        "Dateien sind nach der Wartezeit noch nicht stabil und werden nicht verarbeitet: " + changing
    )
