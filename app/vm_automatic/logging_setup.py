"""Logging for VM Automatic (separate from the VideoMerger app log)."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..video_merger.paths import project_root


def configure_vm_logger() -> tuple[logging.Logger, Path]:
    """Daily rotating-by-date log file: logs/vm_automatic_YYYY-MM-DD.log"""
    log_dir = project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"vm_automatic_{datetime.now():%Y-%m-%d}.log"
    logger = logging.getLogger("vm_automatic")
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path
               for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger, path


class TeeLogger:
    """Writes to the file logger AND forwards lines to in-memory listeners
    (the GUI log view). Thread-safe; listeners must be fast."""

    def __init__(self, logger: logging.Logger, listeners: list):
        self._logger = logger
        self._listeners = listeners
        self.log_path: Path | None = getattr(logger, "_vm_path", None)

    def __call__(self, message: str) -> None:
        line = str(message)
        try:
            self._logger.info(line)
        except Exception:  # logging must never kill the automation
            pass
        for listener in list(self._listeners):
            try:
                listener(line)
            except Exception:
                pass
