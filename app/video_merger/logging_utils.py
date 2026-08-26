from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .paths import project_root


def configure_file_logger() -> tuple[logging.Logger, Path]:
    log_dir = project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"videomerger_{datetime.now():%Y-%m-%d}.log"
    logger = logging.getLogger("videomerger")
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger, path
