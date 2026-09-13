"""Optional Windows startup registration for VM Automatic.

Uses the standard Windows-native mechanism: the per-user Run key
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``. It is:

* OFF by default and NEVER installed silently,
* per-user only (no admin rights, no system-wide changes),
* removable with a single call (the GUI checkbox),
* a no-op with a clear status on non-Windows platforms.
"""
from __future__ import annotations

import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "VM Automatic"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _command_line(root: Path) -> str:
    root = root.resolve()
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    main = root / "app" / "vm_automatic" / "__main__.py"
    return f'"{pythonw}" "{main}"'


def status(root: Path | None = None) -> str:
    """Return 'on', 'off' or 'unavailable'."""
    if not _is_windows():
        return "unavailable"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_NAME)
        return "on" if value else "off"
    except FileNotFoundError:
        return "off"


def enable(root: Path | None = None) -> str:
    if not _is_windows():
        return "unavailable"
    import winreg

    root = (root or Path(__file__).resolve().parents[2])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _command_line(root))
    return "on"


def disable(root: Path | None = None) -> str:
    if not _is_windows():
        return "unavailable"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _RUN_NAME)
    except FileNotFoundError:
        pass
    return "off"


def set_state(enabled: bool, root: Path | None = None) -> str:
    return enable(root) if enabled else disable(root)
