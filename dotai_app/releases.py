"""Application version and best-effort release notifications."""

from __future__ import annotations

from urllib import error as urlerror
from urllib import request as urlrequest
import json
import re
from . import terminal


VERSION = "0.5.0"


RELEASE_URL = "https://api.github.com/repos/mathwro/DotAi/releases/latest"


VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def latest_release_version() -> str | None:
    try:
        request = urlrequest.Request(
            RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"DotAi/{VERSION}"},
        )
        with urlrequest.urlopen(request, timeout=2) as response:
            tag = json.loads(response.read().decode("utf-8")).get("tag_name")
    except urlerror.HTTPError as exc:
        exc.close()
        return None
    except (AttributeError, OSError, TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(tag, str):
        return None
    match = VERSION_PATTERN.fullmatch(tag)
    return ".".join(match.groups()) if match else None


def print_release_notice() -> None:
    latest = latest_release_version()
    current = VERSION_PATTERN.fullmatch(VERSION)
    available = VERSION_PATTERN.fullmatch(latest or "")
    if not current or not available or tuple(map(int, available.groups())) <= tuple(map(int, current.groups())):
        return
    print(f"{terminal.badge('UPDATE')} DotAi {latest} is available (current: {VERSION}); pull the repository to update.")
