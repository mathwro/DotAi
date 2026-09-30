"""Terminal color policy and consistent status formatting."""

from __future__ import annotations

import os
import sys


_COLOR_ENABLED = False


_ANSI = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "cyan": "36",
}


def configure_color(mode: str) -> None:
    global _COLOR_ENABLED
    if mode == "always":
        _COLOR_ENABLED = True
    elif mode == "never":
        _COLOR_ENABLED = False
    else:
        forced = os.environ.get("FORCE_COLOR")
        _COLOR_ENABLED = (
            "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and ((forced is not None and forced != "0") or sys.stdout.isatty())
        )
    if _COLOR_ENABLED and os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode_value = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode_value)):
                kernel32.SetConsoleMode(handle, mode_value.value | 0x0004)
        except (AttributeError, OSError):
            pass


def styled(text: str, *styles: str) -> str:
    if not _COLOR_ENABLED or not styles:
        return text
    codes = ";".join(_ANSI[style] for style in styles)
    return f"\033[{codes}m{text}\033[0m"


def badge(label: str) -> str:
    color = {
        "OK": "green",
        "RUN": "cyan",
        "INACTIVE": "yellow",
        "DRIFT": "yellow",
        "UNVERIFIED": "yellow",
        "UPDATE": "yellow",
        "MISSING": "red",
        "FAIL": "red",
    }.get(label, "blue")
    return styled(f"[{label}]", color, "bold")


def heading(text: str) -> str:
    return styled(text, "blue", "bold")
