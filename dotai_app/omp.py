"""OMP marketplace, plugin, and global extension management."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
from . import manifest as manifests
from . import runtime
from . import terminal


def reconcile_plugins(manifest: dict[str, Any], runner: runtime.Runner, mode: str) -> None:
    for marketplace in manifest["marketplaces"]:
        if mode == "update":
            command = ["omp", "plugin", "marketplace", "update", marketplace["name"]]
        else:
            command = ["omp", "plugin", "marketplace", "add", marketplace["source"]]
        runner.run(command, f"Reconcile marketplace {marketplace['name']}")
    for plugin in manifest["plugins"]:
        if mode == "update":
            command = ["omp", "plugin", "upgrade", "--scope", plugin.get("scope", "user"), plugin["id"]]
        else:
            command = ["omp", "plugin", "install", "--force", "--scope", plugin.get("scope", "user"), plugin["id"]]
        runner.run(command, f"Reconcile plugin {plugin['id']}")


def extension_identity(value: str) -> str:
    if value.startswith(("~/", "~\\")) or Path(value).is_absolute():
        return os.path.normcase(str(runtime.expand_path(value).resolve()))
    return value


def configured_omp_extensions(runner: runtime.Runner) -> list[str] | None:
    raw = runner.output(["omp", "config", "get", "extensions", "--json"])
    if not raw:
        return None
    try:
        value = json.loads(raw).get("value")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return value


def reconcile_omp_extensions(manifest: dict[str, Any], runner: runtime.Runner) -> None:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return
    current = configured_omp_extensions(runner)
    if current is None:
        if runner.dry_run:
            print(f"{terminal.badge('RUN')} OMP extensions: configure {', '.join(desired)}")
            return
        runner.failures.append("OMP extensions: unable to read global OMP configuration")
        print(f"{terminal.badge('FAIL')} OMP extensions: unable to read global OMP configuration")
        return
    known = {extension_identity(item) for item in current}
    additions = [item for item in desired if extension_identity(item) not in known]
    if not additions:
        print(f"{terminal.badge('OK')} OMP extensions: all managed extensions are configured")
        return
    merged = [*current, *additions]
    if runner.dry_run:
        print(f"{terminal.badge('RUN')} OMP extensions: add {', '.join(additions)}")
        return
    runner.run(
        ["omp", "config", "set", "extensions", json.dumps(merged, separators=(",", ":"))],
        "Configure OMP extensions",
    )


def omp_extension_status(manifest: dict[str, Any], runner: runtime.Runner) -> tuple[bool, str]:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return True, "no managed extensions"
    current = configured_omp_extensions(runner)
    if current is None:
        return False, "unable to read global OMP configuration"
    configured = {extension_identity(item) for item in current}
    missing_config = [item for item in desired if extension_identity(item) not in configured]
    missing_files = [item for item in desired if not runtime.expand_path(item).exists()]
    if missing_config:
        return False, f"not configured: {', '.join(missing_config)}"
    if missing_files:
        return False, f"source missing: {', '.join(missing_files)}"
    return True, f"{len(desired)} managed extension(s) configured and available"


def json_contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return needle in value or any(json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(json_contains(item, needle) for item in value)
    return value == needle


def registry_contains(path: Path, needle: str) -> bool:
    try:
        return json_contains(manifests.load_json_object(path), needle)
    except (OSError, runtime.DotAiError):
        return False
