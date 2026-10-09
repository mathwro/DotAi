"""Portable manifest locations; resolving a path never initializes files."""
from __future__ import annotations

import os
from pathlib import Path
from . import runtime


def default_manifest_path(platform_name: str | None = None) -> Path:
    override = os.environ.get("DOTAI_CONFIG_DIR")
    if override:
        base = Path(override).expanduser()
    elif platform_name == "windows" or (platform_name is None and os.name == "nt"):
        appdata = os.environ.get("APPDATA")
        base = (Path(appdata) if appdata else runtime.home_dir() / "AppData" / "Roaming") / "DotAi"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg) if xdg else runtime.home_dir() / ".config") / "dotai"
    return base / "stack.json"


def lock_path(manifest_path: str | Path) -> Path:
    path = Path(manifest_path)
    return path.with_suffix(".lock.json")
