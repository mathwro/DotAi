"""MCP provider discovery, semantic health, and safe reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile
from . import manifest as manifests
from . import runtime
from . import terminal


def desired_mcp(manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    mcp = manifest["mcp"]
    target = runtime.expand_path(mcp.get("target", "~/.omp/agent/mcp.json"))
    return target, mcp


def mcp_config_paths(target: Path) -> list[Path]:
    candidates = [
        target,
        runtime.home_dir() / ".omp" / "agent" / "mcp.json",
        runtime.ROOT / ".omp" / "mcp.json",
        runtime.ROOT / ".omp" / ".mcp.json",
        runtime.ROOT / "mcp.json",
        runtime.ROOT / ".mcp.json",
        runtime.home_dir() / ".config" / "opencode" / "opencode.json",
        runtime.ROOT / "opencode.json",
        runtime.home_dir() / ".cursor" / "mcp.json",
        runtime.ROOT / ".cursor" / "mcp.json",
        runtime.home_dir() / ".vscode" / "mcp.json",
        runtime.ROOT / ".vscode" / "mcp.json",
        runtime.home_dir() / ".claude" / "mcp.json",
        runtime.ROOT / ".claude" / "mcp.json",
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def discover_mcp_servers(target: Path) -> list[tuple[str, dict[str, Any], Path, str, bool]]:
    discovered: list[tuple[str, dict[str, Any], Path, str, bool]] = []
    for path in mcp_config_paths(target):
        if not path.is_file():
            continue
        try:
            config = manifests.load_json_object(path)
        except (OSError, runtime.DotAiError):
            continue
        disabled = set(config.get("disabledServers", []))
        for section in ("mcpServers", "servers", "mcp"):
            servers = config.get(section)
            if not isinstance(servers, dict):
                continue
            for name, server in servers.items():
                if not isinstance(server, dict):
                    continue
                discovered.append((name, server, path, section, name not in disabled and server.get("enabled") is not False))
    return discovered


def server_identity_matches(found: dict[str, Any], required: dict[str, Any]) -> bool:
    found_type = found.get("type", "http" if "url" in found else "stdio")
    required_type = required.get("type", "http" if "url" in required else "stdio")
    if found_type == "remote":
        found_type = "http"
    if required_type == "remote":
        required_type = "http"
    if found_type != required_type:
        return False
    if "url" in required:
        return str(found.get("url", "")).rstrip("/") == str(required["url"]).rstrip("/")
    found_args = found.get("args", [])
    required_args = required.get("args", [])
    if not isinstance(found_args, list) or not isinstance(required_args, list):
        return False
    return found.get("command") == required.get("command") and found_args == required_args


def server_satisfies(found: dict[str, Any], required: dict[str, Any]) -> bool:
    if not server_identity_matches(found, required):
        return False
    for field in ("cwd", "timeout", "enabled"):
        if field in required and found.get(field, True if field == "enabled" else None) != required[field]:
            return False
    for section in ("env", "headers"):
        expected = required.get(section, {})
        actual = found.get(section, {})
        if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
            return False
    return True


def sync_mcp(manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    target, mcp = desired_mcp(manifest)
    existing = manifests.load_json_object(target)
    servers = dict(existing.get("mcpServers", {}))
    discovered = discover_mcp_servers(target)
    changes = 0
    conflicts: list[str] = []
    used: set[tuple[Path, str, str]] = set()
    for name, required in mcp["servers"].items():
        match = next(
            ((path, section, alias) for alias, found, path, section, enabled in discovered if enabled and server_satisfies(found, required)),
            None,
        )
        if match is not None:
            used.add(match)
            continue
        available = [
            entry for entry in discovered
            if (entry[2], entry[3], entry[0]) not in used
            and not (entry[2] == target and entry[3] == "mcpServers" and entry[0] in mcp["servers"] and entry[0] != name)
        ]
        if required.get("enabled") is False or any(
            not enabled and (alias == name or server_identity_matches(found, required))
            for alias, found, _, _, enabled in discovered
        ):
            conflicts.append(name)
            continue

        alias = name if name in servers else None
        if alias is not None and (target, "mcpServers", alias) in used:
            conflicts.append(name)
            continue
        if alias is None:
            candidates = [
                (alias, found) for alias, found, path, section, _ in available
                if path == target and section == "mcpServers" and server_identity_matches(found, required)
            ]
            if "cwd" in required:
                cwd_matches = [(alias, found) for alias, found in candidates if found.get("cwd") == required["cwd"]]
                if cwd_matches:
                    candidates = cwd_matches
            if len(candidates) > 1:
                conflicts.append(name)
                continue
            if candidates:
                alias = candidates[0][0]
        if alias is None:
            if any(server_identity_matches(found, required) for _, found, _, _, _ in available):
                conflicts.append(name)
                continue
            servers[name] = required
            used.add((target, "mcpServers", name))
            discovered.append((name, required, target, "mcpServers", True))
            changes += 1
            continue

        used.add((target, "mcpServers", alias))
        current = servers[alias]
        same_server = isinstance(current, dict) and server_identity_matches(current, required)
        updated = dict(current) if same_server else {}
        for key, value in required.items():
            if key == "type" and same_server and current.get("type") == "remote" and value == "http":
                continue
            if key in ("env", "headers") and isinstance(updated.get(key), dict):
                updated[key] = {**updated[key], **value}
            else:
                updated[key] = value
        if updated != current:
            servers[alias] = updated
            for index, (found_alias, _, path, section, _) in enumerate(discovered):
                if path == target and section == "mcpServers" and found_alias == alias:
                    discovered[index] = (alias, updated, target, "mcpServers", True)
                    break
            else:
                discovered.append((alias, updated, target, "mcpServers", True))
            changes += 1
    if conflicts:
        detail = f"MCP servers unavailable or disabled: {', '.join(conflicts)}; resolve the provider configuration manually"
        runner.failures.append(detail)
        print(f"{terminal.badge('DRIFT')} {detail}")
        return False
    if changes == 0:
        print(f"{terminal.badge('OK')} MCP: all managed servers are available through discovered provider configs")
        return False
    merged = dict(existing)
    merged.setdefault(
        "$schema", mcp.get("$schema", "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json")
    )
    merged["mcpServers"] = servers
    if runner.dry_run:
        print(f"{terminal.badge('RUN')} MCP: reconcile {changes} unavailable managed servers at {target}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = manifests.backup_manifest(target)
        print(f"{terminal.badge('OK')} MCP: backup written to {backup}")
    payload = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, target)
    print(f"{terminal.badge('OK')} MCP: synchronized {target}")
    return True


def mcp_status(manifest: dict[str, Any]) -> tuple[bool, str]:
    target, mcp = desired_mcp(manifest)
    try:
        manifests.load_json_object(target)
    except runtime.DotAiError as exc:
        return False, str(exc)
    discovered = discover_mcp_servers(target)
    missing: list[str] = []
    sources: set[Path] = set()
    for name, required in mcp["servers"].items():
        matches = [(found, path) for _, found, path, _, enabled in discovered if enabled and server_satisfies(found, required)]
        if not matches:
            missing.append(name)
        else:
            sources.add(matches[0][1])
    if missing:
        return False, f"unavailable: {', '.join(missing)}"
    return True, f"{len(mcp['servers'])} managed servers available across {len(sources)} discovered config(s)"
