"""Validate CLI additions and update declared manifest integrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import re
from . import manifest as manifests
from . import runtime
from . import skills as skill_manager
from . import terminal


HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_platform_commands(values: list[str] | None, option: str) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}
    for value in values or []:
        platform_name, separator, command = value.partition("=")
        if not separator or platform_name not in {"windows", "wsl", "ubuntu", "arch", "macos", "linux", "default"} or not command:
            raise runtime.DotAiError(f"{option} must use PLATFORM=COMMAND")
        commands.setdefault(platform_name, []).append(command)
    return commands


def parse_assignments(
    values: list[str] | None,
    option: str,
    name_pattern: re.Pattern[str],
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for item in values or []:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            raise runtime.DotAiError(f"{option} must use NAME=VALUE")
        if not name_pattern.fullmatch(name):
            raise runtime.DotAiError(f"{option} has an invalid name: {name}")
        if name in assignments:
            raise runtime.DotAiError(f"{option} repeats name: {name}")
        assignments[name] = value
    return assignments


def upsert(items: list[dict[str, Any]], key: str, value: dict[str, Any]) -> None:
    for index, item in enumerate(items):
        if item.get(key) == value[key]:
            items[index] = value
            return
    items.append(value)


def add_integration(args: argparse.Namespace, manifest: dict[str, Any], path: Path) -> int:
    kind = args.kind
    if kind == "skill":
        skills = args.skills or ["*"]
        check_skills = args.check_skills if args.check_skills is not None else (
            [] if "*" in skills else [skill_manager.installed_skill_name(name) for name in skills]
        )
        value = {
            "source": args.source,
            "agent": args.agent,
            "skills": skills,
            "checkSkills": check_skills,
        }
        upsert(manifest["skills"], "source", value)
    elif kind == "marketplace":
        upsert(manifest["marketplaces"], "name", {"name": args.name, "source": args.source})
    elif kind == "plugin":
        upsert(manifest["plugins"], "id", {"id": args.id, "scope": args.scope})
    elif kind == "mcp":
        if not manifests.SERVER_NAME.fullmatch(args.name):
            raise runtime.DotAiError(f"Invalid MCP server name: {args.name}")
        if args.url:
            if args.server_args:
                raise runtime.DotAiError("--arg requires --command")
            if args.server_env:
                raise runtime.DotAiError("--env requires --command")
            value = {"type": args.transport, "url": args.url}
            headers = parse_assignments(args.headers, "--header", HEADER_NAME)
            if headers:
                value["headers"] = headers
        else:
            if args.headers:
                raise runtime.DotAiError("--header requires --url")
            value = {"type": "stdio", "command": args.server_command}
            if args.server_args:
                value["args"] = args.server_args
            server_env = parse_assignments(args.server_env, "--env", ENV_NAME)
            if server_env:
                value["env"] = server_env
        manifest["mcp"]["servers"][args.name] = value
    elif kind == "tool":
        installs = parse_platform_commands(args.install_commands, "--install")
        if not installs:
            raise runtime.DotAiError("At least one --install PLATFORM=COMMAND is required")
        value = {"name": args.name, "managed": True, "check": args.check_command, "install": installs}
        if getattr(args, "requires", None):
            value["requires"] = args.requires
        updates = parse_platform_commands(args.update_commands, "--update")
        if updates:
            value["update"] = updates
        upsert(manifest["packages"], "name", value)
    else:
        raise runtime.DotAiError(f"Unsupported integration kind: {kind}")
    manifests.write_manifest(path, manifest)
    print(f"{terminal.badge('OK')} Added {kind} to {path}. Run 'dotai sync' or 'dotai install' to apply it.")
    return 0
