"""Terminal formatting and credential-safe display boundaries."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
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
            and (forced != "0" and (forced is not None or sys.stdout.isatty()))
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


_SECRET_NAME = re.compile(r"token|password|passwd|secret|credential|authorization|cookie|(?:api|access|private)[_-]?key", re.I)
_CREDENTIAL = re.compile(
    r'''(?P<prefix>[\w-]*(?:token|password|passwd|secret|credential|authorization|cookie|api[_-]?key|access[_-]?key|private[_-]?key)[\w-]*["']?\s*(?:[:=]\s*|\s+))'''
    r'''(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s,;}\]&]+)''', re.I,
)
_HEADER = re.compile(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*)([^\r\n]+)")
_BEARER = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([^\s'\",;]+)")
_URL_PASSWORD = re.compile(r"(https?://[^\s/:@]+:)([^\s/@]+)(@)", re.I)
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def credential_values(value: Any) -> list[str]:
    """Collect explicitly named credentials, without resolving secret commands."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_NAME.search(str(key)) and isinstance(item, str) and item:
                found.append(item)
            else:
                found.extend(credential_values(item))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, str) and item.startswith("--") and "=" not in item and _SECRET_NAME.search(item) and index + 1 < len(value):
                found.append(str(value[index + 1]))
            if item in ("--header", "-H", "--env", "-e") and index + 1 < len(value):
                separator = ":" if item in ("--header", "-H") else "="
                _, found_separator, payload = str(value[index + 1]).partition(separator)
                if found_separator and payload.strip():
                    found.append(payload.strip())
            found.extend(credential_values(item))
    elif isinstance(value, str):
        found.extend(match.group("value").strip("\"'") for match in _CREDENTIAL.finditer(value))
        found.extend(match.group(2) for match in _BEARER.finditer(value))
        found.extend(match.group(2) for match in _URL_PASSWORD.finditer(value))
    return found


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask known values and recognizable credential syntax, only for display."""
    for secret in sorted(set(credential_values(dict(os.environ))) | set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _HEADER.sub(r"\1<redacted>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _URL_PASSWORD.sub(r"\1<redacted>\3", text)
    return _CREDENTIAL.sub(lambda match: match.group("prefix") + "<redacted>", text)


def omit_structured(text: str) -> str:
    """Omit JSON values, including multiline objects embedded in diagnostics."""
    try:
        json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        return "<structured content omitted>"
    decoder = json.JSONDecoder()
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] in "[{":
            try:
                _, length = decoder.raw_decode(text[index:])
            except ValueError:
                index += 1
                continue
            parts.extend((text[start:index], "<structured content omitted>"))
            index += length
            start = index
        else:
            index += 1
    parts.append(text[start:])
    return "".join(parts)


def diagnostic(text: str, secrets: Iterable[str] = (), *, limit: int = 2000, lines: int = 12) -> str:
    # Sanitize before truncating: truncation must not expose part of a credential.
    text = _ESCAPE.sub("", text)
    text = "".join(char for char in text if char in "\n\t" or char.isprintable())
    text = redact(omit_structured(text), secrets).strip()
    selected = "\n".join(text.splitlines()[:lines])
    if len(selected) > limit or len(text.splitlines()) > lines:
        return selected[:limit] + "\n<diagnostic truncated>"
    return selected


def say(text: str, *, secrets: Iterable[str] = (), file: Any = None) -> None:
    print(redact(text, secrets), file=file, flush=True)


def describe_skill_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str:
    """Describe selections and changed fields, never arbitrary manifest values."""
    old = {skill["source"]: skill for skill in before}
    new = {skill["source"]: skill for skill in after}
    rows: list[str] = []
    for source in dict.fromkeys([*old, *new]):
        previous, current = old.get(source), new.get(source)
        if previous == current:
            continue
        action = "Add" if previous is None else "Remove" if current is None else "Update"
        rows.append(f"  {action} skills from {source}")
        for label, skill in (("before", previous), ("after", current)):
            if skill is not None:
                selections = ", ".join(skill.get("skills", ["*"]))
                rows.append(f"    {label}: {selections}; agent: {skill.get('agent', 'universal')}")
        if previous is not None and current is not None:
            fields = sorted(key for key in previous.keys() | current.keys() if previous.get(key) != current.get(key))
            rows.append(f"    changed fields: {', '.join(fields)}")
    return redact("\n".join(rows))
