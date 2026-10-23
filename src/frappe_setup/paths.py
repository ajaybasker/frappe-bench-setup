"""User-specific locations used by the tool (XDG base directories)."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "frappe-setup"


def logs_dir() -> Path:
    return state_dir() / "logs"


def runs_dir() -> Path:
    return state_dir() / "runs"


def data_dir() -> Path:
    """Tool-managed runtimes (Node.js, Yarn) and generated environment scripts."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "frappe-setup"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "frappe-setup"


def user_bin_dir() -> Path:
    """Where `uv tool install` places executables (bench)."""
    return Path.home() / ".local" / "bin"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path
