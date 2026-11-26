"""Application version and best-effort release notifications."""

from __future__ import annotations

from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
import json
import os
import re
import tempfile
import time
from . import runtime
from . import terminal


VERSION = "0.7.3"


RELEASE_URL = "https://api.github.com/repos/mathwro/DotAi/releases/latest"


VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


RELEASE_CACHE_TTL_SECONDS = 86_400


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


def _read_cached_release(now: float) -> str | None:
    try:
        data = json.loads((runtime.state_dir() / "release-check.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    checked_at = data.get("checkedAt")
    latest = data.get("latestVersion")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return None
    if not isinstance(latest, str) or not VERSION_PATTERN.fullmatch(latest):
        return None
    if not 0 <= now - float(checked_at) < RELEASE_CACHE_TTL_SECONDS:
        return None
    return latest


def _write_cached_release(latest: str, now: float) -> None:
    temp_path: Path | None = None
    try:
        directory = runtime.state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "release-check.json"
        payload = json.dumps({"checkedAt": now, "latestVersion": latest}, indent=2) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        os.replace(temp_path, target)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def print_release_notice(*, force_refresh: bool = False, persist_cache: bool = True) -> None:
    if os.environ.get("DOTAI_NO_UPDATE_CHECK", "").strip().lower() in {"1", "true"}:
        return
    now = time.time()
    latest = None if force_refresh else _read_cached_release(now)
    if latest is None:
        latest = latest_release_version()
        if latest and VERSION_PATTERN.fullmatch(latest) and persist_cache:
            _write_cached_release(latest, now)
    current = VERSION_PATTERN.fullmatch(VERSION)
    available = VERSION_PATTERN.fullmatch(latest or "")
    if not current or not available or tuple(map(int, available.groups())) <= tuple(map(int, current.groups())):
        return
    terminal.say(f"{terminal.badge('UPDATE')} DotAi {latest} is available (current: {VERSION}); pull the repository to update.")
