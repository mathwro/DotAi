"""Read-only stack status and platform diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import re
from . import manifest as manifests
from . import mcp as mcp_config
from . import omp as omp_config
from . import packages as package_manager
from . import routing as model_routing
from . import runtime
from . import skills as skill_manager
from . import terminal


def print_status(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = True
    runner.say(f"{terminal.heading('Platform:')} {runner.platform}")
    runner.say(terminal.heading("Packages:"))
    for package in manifest["packages"]:
        installed, _, version = package_manager.probe_package(package, runner)
        healthy &= installed
        label = "OK" if installed else "MISSING"
        runner.say(f"  {terminal.badge(label)} {package['name']}: {version.splitlines()[0] if version else 'installed'}")
    runner.say(terminal.heading("Skills:"))
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
        runner.say(f"  {terminal.badge(label)} {skill['source']}: {detail}")
    if skill_manager.print_legacy_skill_notice(manifest):
        healthy = False
    if manifest["marketplaces"]:
        runner.say(terminal.heading("Marketplaces:"))
        registry = runtime.home_dir() / ".omp" / "marketplaces.json"
        for marketplace in manifest["marketplaces"]:
            installed = omp_config.registry_contains(registry, marketplace["name"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            runner.say(f"  {terminal.badge(label)} {marketplace['name']}")
    if manifest["plugins"]:
        runner.say(terminal.heading("Plugins:"))
        for plugin in manifest["plugins"]:
            registry = (
                runtime.ROOT / ".omp" / "plugins" / "installed_plugins.json"
                if plugin.get("scope") == "project"
                else runtime.home_dir() / ".omp" / "plugins" / "installed_plugins.json"
            )
            installed = omp_config.registry_contains(registry, plugin["id"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            runner.say(f"  {terminal.badge(label)} {plugin['id']}")
    if manifest.get("ompExtensions"):
        extensions_ok, detail = omp_config.omp_extension_status(manifest, runner)
        healthy &= extensions_ok
        label = "OK" if extensions_ok else "MISSING"
        runner.say(f"{terminal.heading('OMP extensions:')}\n  {terminal.badge(label)} {detail}")
    if manifest.get("ompRouting"):
        label, detail = model_routing.omp_routing_status(manifest, runner)
        healthy &= label == "OK"
        runner.say(f"{terminal.heading('OMP routing:')}\n  {terminal.badge(label)} {detail}")
    else:
        runner.say(f"{terminal.heading('OMP routing:')}\n  {terminal.badge('INACTIVE')} optional routing not configured; preview with 'dotai configure omp-routing --dry-run'")
    mcp_ok, detail = mcp_config.mcp_status(manifest)
    healthy &= mcp_ok
    label = "OK" if mcp_ok else "DRIFT"
    runner.say(f"{terminal.heading('MCP:')}\n  {terminal.badge(label)} {detail}")
    return healthy


def writable_parent(path: Path) -> bool:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


_UPPER_ENV_REF = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _path_entries(raw_path: str) -> set[str]:
    entries: set[str] = set()
    for entry in raw_path.split(os.pathsep):
        if entry:
            try:
                entries.add(os.path.normcase(str(Path(entry).expanduser().resolve())))
            except OSError:
                entries.add(os.path.normcase(entry))
    return entries


def _declares_modern_web_guidance(manifest: dict[str, Any]) -> bool:
    for skill in manifest.get("skills", []):
        source = skill_manager.github_repository_source(skill.get("source")) or str(skill.get("source", "")).lower()
        selected = skill.get("skills", [])
        checks = skill.get("checkSkills", [])
        if (
            source == "googlechrome/modern-web-guidance"
            or "modern-web-guidance" in selected
            or "modern-web-guidance" in checks
        ):
            return True
    return False


def _mcp_env_references(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    servers = manifest.get("mcp", {}).get("servers", {})
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            continue
        for section in ("headers", "env"):
            mapping = server.get(section)
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, str)
                    and _UPPER_ENV_REF.fullmatch(value)
                    and (key == value or bool(terminal._SECRET_NAME.search(key)))
                    and (server_name, value) not in seen
                ):
                    seen.add((server_name, value))
                    refs.append((server_name, value))
    return refs


def _backup_files_for(target: Path) -> set[Path]:
    parent = target.parent
    if not parent.is_dir():
        return set()
    try:
        return {path.resolve() for path in parent.glob(f"{target.name}.bak.*") if path.is_file()}
    except OSError:
        return set()


def doctor(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    healthy = print_status(manifest, runner)
    target, _ = mcp_config.desired_mcp(manifest)
    writable = writable_parent(target)
    healthy &= writable
    runner.say(f"{terminal.heading('Config target:')}\n  {terminal.badge('OK' if writable else 'FAIL')} writable parent for {target}")
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
        runner.say(f"{terminal.heading('Package manager:')}\n  {terminal.badge('OK' if available else 'FAIL')} {manager[0]}")
    if runner.platform != "windows":
        local_bin = runtime.home_dir() / ".local" / "bin"
        try:
            has_entries = local_bin.is_dir() and any(entry.is_file() for entry in local_bin.iterdir())
        except OSError:
            has_entries = False
        if has_entries:
            normalized_local = os.path.normcase(str(local_bin.resolve()))
            if normalized_local not in _path_entries(os.environ.get("PATH", "")):
                healthy = False
                runner.say(
                    f"{terminal.heading('Shell PATH:')}\n  {terminal.badge('DRIFT')} {local_bin} is missing from your shell PATH; tools installed there will not be found outside DotAi"
                )
    if _declares_modern_web_guidance(manifest):
        node_ok, node_detail = package_manager.package_version_check(
            {"minimumVersion": "22.0"}, runner, ["node", "--version"]
        )
        healthy &= node_ok
        first_line = node_detail.splitlines()[0] if node_detail else "not found"
        if node_ok:
            runner.say(
                f"{terminal.heading('Skill runtime:')}\n  {terminal.badge('OK')} Modern Web Guidance Node.js 22+ requirement satisfied ({first_line})"
            )
        else:
            runner.say(
                f"{terminal.heading('Skill runtime:')}\n  {terminal.badge('FAIL')} Modern Web Guidance requires Node.js 22+ (found: {first_line})"
            )
    conflicting_skill_env: list[str] = []
    seen_skill_env: set[str] = set()
    for skill in manifest.get("skills", []):
        for key, declared in skill.get("ompEnvironment", {}).items():
            inherited = os.environ.get(key)
            if inherited and inherited != declared and key not in seen_skill_env:
                seen_skill_env.add(key)
                conflicting_skill_env.append(key)
    if conflicting_skill_env:
        healthy = False
        runner.say(
            f"{terminal.heading('Skill environment:')}\n  {terminal.badge('DRIFT')} inherited shell environment overrides managed OMP defaults: {', '.join(sorted(conflicting_skill_env))}"
        )
    mcp_refs = _mcp_env_references(manifest)
    if mcp_refs:
        unset_refs = [f"{server} ({var})" for server, var in mcp_refs if not (var in os.environ and bool(os.environ[var]))]
        if unset_refs:
            runner.say(
                f"{terminal.heading('MCP environment:')}\n  {terminal.badge('INACTIVE')} unset environment variable reference(s): {', '.join(unset_refs)}"
            )
        else:
            runner.say(
                f"{terminal.heading('MCP environment:')}\n  {terminal.badge('OK')} {len(mcp_refs)} referenced environment variable(s) set in shell"
            )
    backups: set[Path] = set()
    for watched in (target, runtime.home_dir() / ".omp" / "agent" / ".env", manifests.DEFAULT_MANIFEST):
        backups.update(_backup_files_for(watched))
    if backups:
        runner.say(
            f"{terminal.heading('Backups:')}\n  {terminal.badge('INACTIVE')} {len(backups)} backup file(s) retained (*.bak.*); review and prune old copies after rotating credentials"
        )
    return healthy
