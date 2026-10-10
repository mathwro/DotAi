"""OMP marketplace, plugin, and global extension management."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import json
import os
from . import manifest as manifests
from . import runtime
from . import terminal


def reconcile_plugins(
    manifest: dict[str, Any], runner: runtime.Runner, mode: str, *,
    deactivate: Callable[..., bool] | None = None,
    record_installed: Callable[..., None] | None = None,
    ownership_check: Callable[..., None] | None = None,
) -> None:
    for marketplace in manifest["marketplaces"]:
        try:
            registered = marketplace_record(marketplace) is not None
        except (OSError, UnicodeError, runtime.DotAiError) as exc:
            runner.fail(f"Marketplace {marketplace['name']}", exc)
            continue
        if marketplace.get("enabled") is False:
            if deactivate is not None:
                deactivate("marketplace", marketplace["name"], marketplace, manifest, runner)
            elif registered:
                detail = f"Marketplace {marketplace['name']}: disabling requires verified ownership; run 'dotai disable marketplace:{marketplace['name']}'"
                runner.failures.append(detail)
                print(f"{terminal.badge('DRIFT')} {detail}")
            else:
                print(f"{terminal.badge('INACTIVE')} Marketplace {marketplace['name']}: not registered")
            continue
        if mode != "update" and registered:
            print(f"{terminal.badge('OK')} Marketplace {marketplace['name']}: already registered")
            continue
        if mode == "update" and registered:
            try:
                if ownership_check is None:
                    raise runtime.DotAiError("existing marketplace update requires verified ownership; adopt matching content first")
                ownership_check("marketplace", marketplace["name"], marketplace, manifest, runner)
            except (OSError, ValueError, runtime.DotAiError) as exc:
                runner.failures.append(f"Marketplace {marketplace['name']}: {exc}")
                print(f"{terminal.badge('DRIFT')} Marketplace {marketplace['name']}: {exc}")
                continue
        if mode == "update":
            command = ["omp", "plugin", "marketplace", "update", marketplace["name"]]
        else:
            command = ["omp", "plugin", "marketplace", "add", marketplace["source"]]
        result = runner.run(command, f"Reconcile marketplace {marketplace['name']}")
        if not runner.dry_run and result is not None and result.returncode == 0 and record_installed is not None:
            record_installed("marketplace", marketplace["name"], marketplace, manifest, runner)
    for plugin in manifest["plugins"]:
        if plugin.get("enabled") is False:
            if deactivate is not None:
                deactivate("plugin", plugin["id"], plugin, manifest, runner)
            elif installed_plugin(plugin) is not None:
                detail = f"Plugin {plugin['id']}: disabling requires verified ownership; run 'dotai disable plugin:{plugin['id']}'"
                runner.failures.append(detail)
                print(f"{terminal.badge('DRIFT')} {detail}")
            else:
                print(f"{terminal.badge('INACTIVE')} Plugin {plugin['id']}: not installed")
            continue
        registry = plugin_registry(plugin)
        try:
            installed = installed_plugin(plugin)
        except (OSError, UnicodeError, runtime.DotAiError) as exc:
            runner.failures.append(f"Plugin {plugin['id']}: {exc}")
            print(f"{terminal.badge('DRIFT')} Plugin {plugin['id']}: {exc}")
            continue
        if plugin.get("version") not in (None, "latest"):
            matches = installed is not None and installed["version"] == plugin["version"] and Path(installed["installPath"]).is_dir()
            if matches:
                print(f"{terminal.badge('OK')} Plugin {plugin['id']}: retained observed pinned version {plugin['version']}")
            else:
                detail = f"Plugin {plugin['id']}: requested exact version is absent or mismatched; OMP cannot install an exact marketplace plugin version, so no changes were made"
                runner.failures.append(detail)
                print(f"{terminal.badge('FAIL')} {detail}")
            continue
        if mode == "update" and plugin.get("updatePolicy") == "pinned":
            detail = f"Plugin {plugin['id']}: pinned policy requires an exact observed target version; lock or declare it before update"
            runner.failures.append(detail)
            print(f"{terminal.badge('FAIL')} {detail}")
            continue
        if mode != "update" and installed is not None:
            print(f"{terminal.badge('OK')} Plugin {plugin['id']}: already installed; retained current version")
            continue
        if mode == "update" and installed is not None:
            try:
                if ownership_check is None:
                    raise runtime.DotAiError("existing plugin update requires verified ownership; adopt matching content first")
                ownership_check("plugin", plugin["id"], plugin, manifest, runner)
            except (OSError, ValueError, runtime.DotAiError) as exc:
                runner.failures.append(f"Plugin {plugin['id']}: {exc}")
                print(f"{terminal.badge('DRIFT')} Plugin {plugin['id']}: {exc}")
                continue
        if installed is None:
            try:
                preflight_plugin_install(plugin)
            except (OSError, UnicodeError, runtime.DotAiError) as exc:
                runner.failures.append(f"Plugin {plugin['id']}: {exc}")
                print(f"{terminal.badge('DRIFT')} Plugin {plugin['id']}: {exc}")
                continue
        if mode == "update" and installed is not None:
            command = ["omp", "plugin", "upgrade", "--scope", plugin.get("scope", "user"), plugin["id"]]
        else:
            command = ["omp", "plugin", "install", "--scope", plugin.get("scope", "user"), plugin["id"]]
        result = runner.run(command, f"Reconcile plugin {plugin['id']}")
        if not runner.dry_run and result is not None and result.returncode == 0 and record_installed is not None:
            record_installed("plugin", plugin["id"], plugin, manifest, runner)


def plugin_registry(plugin: dict[str, Any]) -> Path:
    root = Path.cwd() if plugin.get("scope") == "project" else runtime.home_dir()
    return root / ".omp" / "plugins" / "installed_plugins.json"


def read_plugin_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "plugins": {}}
    data = manifests.load_json_object(path)
    records = data.get("plugins")
    if data.get("version") != 2 or not isinstance(records, dict):
        raise runtime.DotAiError(f"Invalid plugin registry at {path}; repair it before installing or updating")
    for entries in records.values():
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or entry.get("scope") not in ("user", "project")
            or not isinstance(entry.get("installPath"), str)
            or not Path(entry["installPath"]).is_absolute()
            or not isinstance(entry.get("version"), str) or not entry["version"]
            for entry in entries
        ):
            raise runtime.DotAiError(f"Incomplete plugin registry metadata at {path}; repair it before mutation")
    return data


def installed_plugin(plugin: dict[str, Any]) -> dict[str, Any] | None:
    entries = read_plugin_registry(plugin_registry(plugin))["plugins"].get(plugin["id"], [])
    selected = [entry for entry in entries if entry["scope"] == plugin.get("scope", "user")]
    if len(selected) > 1:
        raise runtime.DotAiError("Plugin scope has ambiguous installation records; resolve them before mutation")
    return selected[0] if selected else None

def marketplace_record(marketplace: dict[str, Any]) -> dict[str, Any] | None:
    """Match the actual registration name and declared source, not arbitrary JSON."""
    path = runtime.home_dir() / ".omp" / "marketplaces.json"
    records = manifests.load_json_object(path).get("marketplaces", [])
    if not isinstance(records, list):
        raise runtime.DotAiError("Invalid OMP marketplace registry; repair it before reconciliation")
    matches = [entry for entry in records if isinstance(entry, dict) and entry.get("name") == marketplace["name"]]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].get("sourceUri") != marketplace["source"]:
        raise runtime.DotAiError("Marketplace name does not prove the declared source; resolve its registration")
    return matches[0]


def plugin_status(plugin: dict[str, Any]) -> tuple[str, str]:
    try:
        entry = installed_plugin(plugin)
    except (OSError, UnicodeError, runtime.DotAiError) as exc:
        return "DRIFT", str(exc)
    if entry is None or not Path(entry["installPath"]).is_dir():
        return "MISSING", "installation source unavailable for this scope"
    if not entry.get("enabled", True):
        return "INACTIVE", "installed but disabled"
    if plugin.get("version", "latest") not in ("latest", entry["version"]):
        return "DRIFT", "installed version differs from the requested target"
    return "OK", entry["version"]


def preflight_plugin_install(plugin: dict[str, Any]) -> None:
    for scope in ("user", "project"):
        other = {"id": plugin["id"], "scope": scope}
        if scope != plugin.get("scope", "user") and installed_plugin(other) is not None:
            raise runtime.DotAiError("Plugin already belongs to another scope; installing could overwrite its shared cache, so no changes were made")
    cache = runtime.home_dir() / ".omp/plugins/cache/plugins"
    if cache.resolve() != cache:
        raise runtime.DotAiError("Plugin cache root is redirected; resolve it before installation")
    if cache.is_dir():
        name, marketplace = plugin["id"].split("@", 1)
        prefix = (marketplace + "___" + name + "___").lower()
        if any(path.name.lower().startswith(prefix) for path in cache.iterdir()):
            raise runtime.DotAiError("Unregistered or shared plugin cache content already exists; resolve provenance before installing")


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


def reconcile_omp_extensions(
    manifest: dict[str, Any], runner: runtime.Runner, *,
    deactivate: Callable[..., bool] | None = None,
    record_installed: Callable[..., None] | None = None,
) -> None:
    declarations = manifest.get("ompExtensions", [])
    for value in declarations:
        if isinstance(value, dict) and value.get("enabled") is False:
            if deactivate is not None:
                deactivate("extension", value["path"], value, manifest, runner)
            else:
                detail = f"Extension {value['path']}: disabling requires ownership verification; run 'dotai disable extension:{value['path']}'"
                runner.failures.append(detail)
                print(f"{terminal.badge('DRIFT')} {detail}")
    desired = [value if isinstance(value, str) else value["path"] for value in declarations
               if isinstance(value, str) or value.get("enabled", True)]
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
    result = runner.run(
        ["omp", "config", "set", "extensions", json.dumps(merged, separators=(",", ":"))],
        "Configure OMP extensions",
    )
    if result is not None and result.returncode == 0 and record_installed is not None:
        for path in additions:
            record_installed("extension", path, {"path": path}, manifest, runner)


def omp_extension_status(manifest: dict[str, Any], runner: runtime.Runner) -> tuple[bool, str]:
    declarations = manifest.get("ompExtensions", [])
    desired = [value if isinstance(value, str) else value["path"] for value in declarations
               if isinstance(value, str) or value.get("enabled", True)]
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
