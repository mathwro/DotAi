"""Read-only stack status and platform diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
from . import lifecycle
from . import mcp as mcp_config
from . import omp as omp_config
from . import packages as package_manager
from . import prerequisites
from . import routing as model_routing
from . import runtime
from . import skills as skill_manager
from . import terminal


def print_status(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = prerequisites.status(manifest, runner)
    secrets = terminal.credential_values(manifest) + terminal.credential_values(runner.env)
    print(f"{terminal.heading('Platform:')} {terminal.redact(runner.platform, secrets)}")
    print(terminal.heading("Packages:"))
    for package in prerequisites.enabled_entries(manifest, "packages"):
        if "minimumVersion" in package or package.get("version", "latest") != "latest":
            installed, version = package_manager.package_version_check(package, runner, runtime.selected(package.get("check", []), runner.platform))
        else:
            installed = package_manager.package_check(package, runner)
            version = runner.output(runtime.selected(package.get("check", []), runner.platform)) if installed else "not found"
        healthy &= installed
        label = "OK" if installed else "MISSING"
        parsed_version = package_manager.PACKAGE_VERSION_PATTERN.search(version)
        detail = ".".join(part for part in parsed_version.groups() if part is not None) if parsed_version else ("installed" if installed else "check failed")
        if package.get("version", "latest") != "latest":
            detail += f" (requested {package['version']})"
        print(f"  {terminal.badge(label)} {terminal.redact(package['name'], secrets)}: {detail}")
    print(terminal.heading("Skills:"))
    for skill in prerequisites.enabled_entries(manifest, "skills"):
        installed, detail = skill_manager.skill_status(skill)
        legacy = skill.get("agent") == "pi"
        healthy &= installed and not legacy
        if legacy and installed:
            label = "DRIFT"
        elif installed:
            label = "OK"
        elif detail.startswith("unverified for "):
            label = "UNVERIFIED"
        elif detail.startswith("installed as"):
            label = "INACTIVE"
        else:
            label = "MISSING"
        print(f"  {terminal.badge(label)} {terminal.redact(skill['source'], secrets)}: {terminal.redact(detail, secrets)}")
    if skill_manager.print_legacy_skill_notice({**manifest, "skills": prerequisites.enabled_entries(manifest, "skills")}):
        healthy = False
    if prerequisites.enabled_entries(manifest, "marketplaces"):
        print(terminal.heading("Marketplaces:"))
        for marketplace in prerequisites.enabled_entries(manifest, "marketplaces"):
            try:
                entry = omp_config.marketplace_record(marketplace)
                installed = entry is not None and Path(entry.get("catalogPath", "")).is_file()
                label, detail = ("OK", "registered source available") if installed else ("MISSING", "registration or source unavailable")
            except (OSError, UnicodeError, runtime.DotAiError) as exc:
                installed, label, detail = False, "DRIFT", str(exc)
            healthy &= installed
            print(f"  {terminal.badge(label)} {terminal.redact(marketplace['name'], secrets)}: {terminal.redact(detail, secrets)}")
    if prerequisites.enabled_entries(manifest, "plugins"):
        print(terminal.heading("Plugins:"))
        for plugin in prerequisites.enabled_entries(manifest, "plugins"):
            label, detail = omp_config.plugin_status(plugin)
            healthy &= label == "OK"
            print(f"  {terminal.badge(label)} {terminal.redact(plugin['id'], secrets)}: {terminal.redact(detail, secrets)}")
    if prerequisites.enabled_extensions(manifest):
        extensions_ok, detail = omp_config.omp_extension_status(manifest, runner)
        healthy &= extensions_ok
        label = "OK" if extensions_ok else "MISSING"
        print(f"{terminal.heading('OMP extensions:')}\n  {terminal.badge(label)} {terminal.redact(detail, secrets)}")
    if manifest.get("ompRouting"):
        label, detail = model_routing.omp_routing_status(manifest, runner)
        healthy &= label == "OK"
        print(f"{terminal.heading('OMP routing:')}\n  {terminal.badge(label)} {terminal.redact(detail, secrets)}")
    elif prerequisites.has_integrations(manifest):
        print(f"{terminal.heading('OMP routing:')}\n  {terminal.badge('INACTIVE')} optional routing not configured; preview with 'dotai configure omp-routing --dry-run'")
    if manifest["mcp"]["servers"]:
        mcp_ok, detail = mcp_config.mcp_status(manifest)
        healthy &= mcp_ok
        active = any(server.get("enabled", True) for server in manifest["mcp"]["servers"].values())
        label = "OK" if mcp_ok and active else "INACTIVE" if mcp_ok else "DRIFT"
        print(f"{terminal.heading('MCP:')}\n  {terminal.badge(label)} {terminal.redact(detail, secrets)}")
    disabled = [(kind, identity, value) for kind, identity, value in lifecycle.components(manifest)
                if kind != "mcp" and value.get("enabled") is False]
    if disabled:
        print(terminal.heading("Disabled components:"))
    for kind, identity, value in disabled:
        try:
            observed = lifecycle.observation(kind, identity, value, manifest, runner)
            active = observed.get("active", False)
            label = "DRIFT" if active else "INACTIVE"
            detail = "declared disabled but still active; resolve runtime state explicitly" if active else "disabled and inactive"
            healthy &= not active
        except (OSError, ValueError, runtime.DotAiError) as exc:
            label, detail = "UNVERIFIED", str(exc)
            healthy = False
        print(f"  {terminal.badge(label)} {terminal.redact(kind + ':' + identity, secrets)}: {terminal.redact(detail, secrets)}")
    return healthy


def writable_parent(path: Path) -> bool:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


def doctor(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = print_status(manifest, runner)
    if any(server.get("enabled", True) for server in manifest["mcp"]["servers"].values()):
        target, _ = mcp_config.desired_mcp(manifest)
        writable = writable_parent(target)
        healthy &= writable
        print(f"{terminal.heading('Config target:')}\n  {terminal.badge('OK' if writable else 'FAIL')} writable parent for {terminal.redact(target)}")
    return healthy
