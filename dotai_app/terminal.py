"""Terminal color policy and consistent status formatting."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any


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


_SECRET_NAME = r"[\w.-]*(?:token|password|passwd|secret|api[_-]?key|authorization|credential|cookie|private[_-]?key)[\w.-]*"
_SECRET_KEY = re.compile(_SECRET_NAME, re.IGNORECASE)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_JSON_START = re.compile(r"[\[{]")
_LOG_LABEL = re.compile(r"\[(?:[A-Za-z][A-Za-z _-]*|\d+/\d+)\](?=\s|$)")
_LOG_LINE = re.compile(r"(?m)^[ \t]*\[(?:[A-Za-z][A-Za-z _-]*|\d+/\d+)\](?=\s|$)")
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"""((?:["']?{_SECRET_NAME}["']?)\s*(?:=|:)\s*)(?:"([^"]*)"|'([^']*)'|([^\s,;&}}]+))""",
    re.IGNORECASE,
)
_CREDENTIAL_FLAG = re.compile(
    rf"""(--{_SECRET_NAME}\s+)(?:"([^"]*)"|'([^']*)'|([^\s]+))""", re.IGNORECASE
)
_AUTHORIZATION = re.compile(
    r"(\bAuthorization\s*[:=]\s*['\"]?(?:(?:Bearer|Basic)\s+)?)([^\s'\",;]+)", re.IGNORECASE
)
_COOKIE = re.compile(r"(\b(?:Set-Cookie|Cookie)\s*:\s*)[^\r\n]+", re.IGNORECASE)
_URL_PASSWORD = re.compile(r"(https?://[^/\s:@]+:)([^@\s]+)(@)", re.IGNORECASE)
_COMMAND_VALUE = re.compile(
    r"""(?:--(?:header|env)|(?<!\S)-(?:H|e))(?:=|\s+)(?:"([^"]*)"|'([^']*)'|([^\s]+))""", re.IGNORECASE
)


def credential_values(value: Any, *, sensitive: bool = False) -> list[str]:
    """Collect declared credential values, not arbitrary unknown secrets."""
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            values.extend(credential_values(
                item, sensitive=sensitive or bool(_SECRET_KEY.search(str(key))) or key in {"env", "headers"}
            ))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.extend(credential_values(item, sensitive=sensitive))
    elif isinstance(value, str):
        if sensitive and value:
            values.append(value)
        for pattern in (_CREDENTIAL_ASSIGNMENT, _CREDENTIAL_FLAG):
            for match in pattern.finditer(value):
                candidate = next((part for part in match.groups()[1:] if part is not None), "")
                if candidate:
                    values.append(candidate)
        for match in _AUTHORIZATION.finditer(value):
            values.append(match.group(2))
        for match in _URL_PASSWORD.finditer(value):
            values.append(match.group(2))
        for match in _COMMAND_VALUE.finditer(value):
            item = next(part for part in match.groups() if part is not None)
            separator = ":" if ":" in item else "="
            if separator in item:
                values.append(item.split(separator, 1)[1].strip())
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(parsed, (dict, list)):
                values.extend(credential_values(parsed))
    return values


def redact(text: Any, secrets: Any = ()) -> str:
    """Sanitize known credentials and common credential syntax for display only."""
    value = _ANSI_ESCAPE.sub("", str(text))
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    known = [str(secret) for secret in secrets if secret]
    known.extend(credential_values(os.environ))
    value = _URL_PASSWORD.sub(r"\1[redacted]\3", value)
    value = _AUTHORIZATION.sub(r"\1[redacted]", value)
    value = _COOKIE.sub(r"\1[redacted]", value)
    for pattern in (_CREDENTIAL_ASSIGNMENT, _CREDENTIAL_FLAG):
        value = pattern.sub(lambda match: match.group(1) + "[redacted]", value)
    for secret in sorted(set(known), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return value


def omit_protocol(text: str, replacement: str = "Structured tool response captured; content omitted.") -> str:
    """Omit structured responses, including truncated records, but retain log labels."""
    decoder = json.JSONDecoder()
    parts: list[str] = []
    cursor = 0
    scan = 0
    while match := _JSON_START.search(text, scan):
        start = match.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            if text[start] == "[" and _LOG_LABEL.match(text, start):
                scan = start + 1
                continue
            end = len(text)
            boundary = start
            quoted = escaped = False
            for following in _LOG_LINE.finditer(text, start + 1):
                for char in text[boundary:following.start()]:
                    if escaped:
                        escaped = False
                    elif quoted and char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = not quoted
                boundary = following.start()
                if not quoted:
                    end = boundary
                    break
            parts.extend((text[cursor:start], replacement + "\n"))
            cursor = end
            scan = end
            continue
        if isinstance(value, (dict, list)):
            parts.extend((text[cursor:start], replacement))
            cursor = end
        scan = end
    parts.append(text[cursor:])
    return "".join(parts)


def diagnostic_lines(text: str, secrets: Any = ()) -> list[str]:
    """Render bounded prose diagnostics; bracketed log labels are not JSON."""
    return [
        redact(line, secrets)[:500]
        for line in omit_protocol(text).splitlines()
        if line.strip()
    ]


def failure_cause(text: str, secrets: Any = ()) -> str:
    """Extract a plain-language error, never the surrounding protocol payload."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None

    def error_message(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in ("error", "message", "summary", "reason"):
                message = error_message(item.get(key))
                if message:
                    return message
        return ""

    if isinstance(value, dict):
        message = error_message(value)
        if message:
            return redact(omit_protocol(message), list(secrets) + credential_values(value))[:240]
    lines = diagnostic_lines(text, secrets)
    return next((line for line in reversed(lines) if not line.startswith("Structured tool response")), "")[:240]


def describe_changes(before: Any, after: Any, target: Any) -> str:
    """Describe changed components and fields without rendering configuration values."""
    if before == after:
        return ""
    changes: list[str] = []

    def describe(old: Any, new: Any, location: str) -> None:
        if old == new:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                field = f"{location}.{key}" if location else str(key)
                if key not in old:
                    changes.append(f"Add {field}")
                elif key not in new:
                    changes.append(f"Remove {field}")
                else:
                    describe(old[key], new[key], field)
        elif isinstance(old, list) and isinstance(new, list):
            def identity(item: Any) -> str:
                if isinstance(item, dict):
                    name = next((str(item[key]) for key in ("name", "id", "source") if key in item), "")
                    scope = item.get("agent", item.get("scope", ""))
                    return f"{name} ({scope})" if scope else name
                return ""

            old_names = [identity(item) for item in old]
            new_names = [identity(item) for item in new]
            if all(old_names + new_names) and len(set(old_names)) == len(old) and len(set(new_names)) == len(new):
                old_items = dict(zip(old_names, old))
                new_items = dict(zip(new_names, new))
                for name in sorted(old_items.keys() | new_items.keys()):
                    field = f"{location}: {name}"
                    if name not in old_items:
                        changes.append(f"Add {field}")
                    elif name not in new_items:
                        changes.append(f"Remove {field}")
                    else:
                        describe(old_items[name], new_items[name], field)
                if old_names != new_names and set(old_names) == set(new_names):
                    changes.append(f"Reorder {location}")
            else:
                changes.append(f"Change {location} selection ({len(old)} to {len(new)} entries)")
        else:
            changes.append(f"Change {location or 'configuration'}")

    describe(before, after, "")
    secrets = credential_values(before) + credential_values(after)
    return redact(f"Proposed changes to {target}:\n" + "\n".join(f"  {line}" for line in changes), secrets)
