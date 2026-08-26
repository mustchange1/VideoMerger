from __future__ import annotations

import os
import shlex
import subprocess


def hidden_process_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def safe_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    return env


def format_command_for_log(arguments: list[str]) -> str:
    """Return a copy/paste-oriented representation without invoking a shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)
