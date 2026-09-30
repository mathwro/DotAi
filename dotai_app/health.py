"""Read-only stack status and platform diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
from . import mcp as mcp_config
from . import omp as omp_config
from . import packages as package_manager
from . import routing as model_routing
from . import runtime
from . import skills as skill_manager
from . import terminal


def print_status(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = True
    print(f"{terminal.heading('Platform:')} {runner.platform}")
    print(terminal.heading("Packages:"))
    for package in manifest["packages"]:
        if "minimumVersion" in package:
            installed, version = package_manager.package_version_check(package, runner, runtime.selected(package.get("check", []), runner.platform))
        else:
            installed = package_manager.package_check(package, runner)
            version = runner.output(runtime.selected(package.get("check", []), runner.platform)) if installed else "not found"
        healthy &= installed
        label = "OK" if installed else "MISSING"
        print(f"  {terminal.badge(label)} {package['name']}: {version.splitlines()[0] if version else 'installed'}")
    print(terminal.heading("Skills:"))
    for skill in manifest["skills"]:
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
        print(f"  {terminal.badge(label)} {skill['source']}: {detail}")
    if skill_manager.print_legacy_skill_notice(manifest):
        healthy = False
    if manifest["marketplaces"]:
        print(terminal.heading("Marketplaces:"))
        registry = runtime.home_dir() / ".omp" / "marketplaces.json"
        for marketplace in manifest["marketplaces"]:
            installed = omp_config.registry_contains(registry, marketplace["name"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            print(f"  {terminal.badge(label)} {marketplace['name']}")
    if manifest["plugins"]:
        print(terminal.heading("Plugins:"))
        for plugin in manifest["plugins"]:
            registry = (
                runtime.ROOT / ".omp" / "plugins" / "installed_plugins.json"
                if plugin.get("scope") == "project"
                else runtime.home_dir() / ".omp" / "plugins" / "installed_plugins.json"
            )
            installed = omp_config.registry_contains(registry, plugin["id"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            print(f"  {terminal.badge(label)} {plugin['id']}")
    if manifest.get("ompExtensions"):
        extensions_ok, detail = omp_config.omp_extension_status(manifest, runner)
        healthy &= extensions_ok
        label = "OK" if extensions_ok else "MISSING"
        print(f"{terminal.heading('OMP extensions:')}\n  {terminal.badge(label)} {detail}")
    if manifest.get("ompRouting"):
        label, detail = model_routing.omp_routing_status(manifest, runner)
        healthy &= label == "OK"
        print(f"{terminal.heading('OMP routing:')}\n  {terminal.badge(label)} {detail}")
    else:
        print(f"{terminal.heading('OMP routing:')}\n  {terminal.badge('INACTIVE')} optional routing not configured; preview with 'dotai configure omp-routing --dry-run'")
    mcp_ok, detail = mcp_config.mcp_status(manifest)
    healthy &= mcp_ok
    label = "OK" if mcp_ok else "DRIFT"
    print(f"{terminal.heading('MCP:')}\n  {terminal.badge(label)} {detail}")
    return healthy


def writable_parent(path: Path) -> bool:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


def doctor(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = print_status(manifest, runner)
    target, _ = mcp_config.desired_mcp(manifest)
    writable = writable_parent(target)
    healthy &= writable
    print(f"{terminal.heading('Config target:')}\n  {terminal.badge('OK' if writable else 'FAIL')} writable parent for {target}")
    manager_checks = {
        "windows": ["scoop", "--version"],
        "macos": ["brew", "--version"],
        "ubuntu": ["apt-get", "--version"],
        "wsl": ["apt-get", "--version"],
        "arch": ["pacman", "--version"],
    }
    manager = manager_checks.get(runner.platform)
    if manager:
        available = runner.succeeds(manager)
        healthy &= available
        print(f"{terminal.heading('Package manager:')}\n  {terminal.badge('OK' if available else 'FAIL')} {manager[0]}")
    return healthy
