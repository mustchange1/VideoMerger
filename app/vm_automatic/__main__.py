"""VM Automatic entry point: ``python -m app.vm_automatic``."""
from __future__ import annotations

import os
import sys


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("PYTHONUTF8", "1")
    if "--version" in sys.argv:
        from . import __version__

        print(f"VM Automatic {__version__} (VideoMerger companion)")
        return 0
    from .gui import launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
