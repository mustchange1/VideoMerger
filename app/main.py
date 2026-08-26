from __future__ import annotations

import os
import sys
from pathlib import Path

# Running this file directly puts app/ on sys.path.
from video_merger.gui.main_window import launch


if __name__ == "__main__":
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    raise SystemExit(launch())
