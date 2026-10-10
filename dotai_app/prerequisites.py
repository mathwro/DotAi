"""Checks-only external requirements selected by enabled component intent."""
from __future__ import annotations

from typing import Any
from . import runtime, terminal
from . import packages


def enabled_entries(manifest: dict[str, Any], section: str) -> list[dict[str, Any]]:
    return [entry for entry in manifest.get(section, []) if entry.get("enabled", True)]


def enabled_extensions(manifest: dict[str, Any]) -> list[str | dict[str, Any]]:
    return [entry for entry in manifest.get("ompExtensions", [])
            if isinstance(entry, str) or entry.get("enabled", True)]


def has_integrations(manifest: dict[str, Any]) -> bool:
    return bool(enabled_entries(manifest, "marketplaces") or enabled_entries(manifest, "plugins")
                or enabled_extensions(manifest) or manifest.get("ompRouting")
                or any(server.get("enabled", True) for server in manifest.get("mcp", {}).get("servers", {}).values()))


def required_prerequisites(manifest: dict[str, Any], runner: runtime.Runner, mode: str = "status", force: bool = False) -> list[dict[str, Any]]:
    definitions = {item["name"]: item for item in manifest.get("prerequisites", [])}
    names: list[str] = []
    metadata = manifest.get("integrationRequires", {})
    for section in ("packages", "skills", "marketplaces", "plugins"):
        entries = enabled_entries(manifest, section)
        if entries:
            names.extend(runtime.selected(metadata.get(section, []), runner.platform))
        for entry in entries:
            names.extend(runtime.selected(entry.get("requires", []), runner.platform))
            if section == "packages" and mode in {"install", "update"}:
                operation = packages.package_operation(entry, runner, mode, force)
                if operation and not runtime.selected(entry.get(operation, []), runner.platform):
                    raise runtime.DotAiError(f"{entry['name']}: no supported {operation} commands for {runner.platform}")
                if operation == "update":
                    names.extend(runtime.selected(entry.get("updateRequires", []), runner.platform))
    other_selections = {
        "ompExtensions": enabled_extensions(manifest),
        "ompRouting": manifest.get("ompRouting"),
        "mcp": [server for server in manifest.get("mcp", {}).get("servers", {}).values() if server.get("enabled", True)],
    }
    for section, selected in other_selections.items():
        if selected:
            names.extend(runtime.selected(metadata.get(section, []), runner.platform))
    for entry in other_selections["mcp"]:
        names.extend(runtime.selected(entry.get("requires", []), runner.platform))
    for entry in other_selections["ompExtensions"]:
        if isinstance(entry, dict):
            names.extend(runtime.selected(entry.get("requires", []), runner.platform))
    # Selected managed components can provide a requirement when this run installs
    # packages. Sync and observation must still check already-installed providers.
    provided = {
        name for package in enabled_entries(manifest, "packages")
        if package.get("managed") is True and mode in {"install", "update"}
        and (runtime.selected(package.get("install", []), runner.platform) or packages.package_check(package, runner))
        for name in package.get("provides", [])
    }
    names = [name for name in dict.fromkeys(names) if name not in provided]
    missing = [name for name in names if name not in definitions]
    if missing:
        raise runtime.DotAiError("No checks-only prerequisite definition for " + ", ".join(missing)
                                 + "; declare its check in prerequisites before applying this selection.")
    return [definitions[name] for name in names]


def status(manifest: dict[str, Any], runner: runtime.Runner, *, record_failures: bool = False, mode: str = "status", force: bool = False, show_available: bool = True) -> bool:
    secrets = terminal.credential_values(manifest) + terminal.credential_values(runner.env)
    try:
        requirements = required_prerequisites(manifest, runner, mode, force)
    except runtime.DotAiError as exc:
        if record_failures:
            runner.failures.append(terminal.redact(exc, secrets))
        print(f"{terminal.badge('FAIL')} Prerequisites: {terminal.redact(exc, secrets)}")
        return False
    healthy = True
    if requirements and show_available:
        print(terminal.heading("External prerequisites:"))
    for prerequisite in requirements:
        available = packages.package_check(prerequisite, runner)
        healthy &= available
        hint = prerequisite.get("hint", f"Install {prerequisite['name']} externally and retry.")
        detail = "available" if available else hint
        if show_available or not available:
            print(f"  {terminal.badge('OK' if available else 'FAIL')} {terminal.redact(prerequisite['name'], secrets)}: {terminal.redact(detail, secrets)}")
        if not available and record_failures:
            runner.failures.append(terminal.redact(f"Prerequisite {prerequisite['name']}: {hint}", secrets))
    return healthy


def preflight(manifest: dict[str, Any], runner: runtime.Runner, mode: str = "sync", force: bool = False, *, show_available: bool = True) -> bool:
    return status(manifest, runner, record_failures=True, mode=mode, force=force, show_available=show_available)
