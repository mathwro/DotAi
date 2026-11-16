"""Validate CLI additions and removals, scope manifests, and update declared integrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import copy
import re
import shutil
from . import manifest as manifests
from . import mcp as mcp_config
from . import omp as omp_config
from . import runtime
from . import skills as skill_manager
from . import terminal

HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_platform_commands(values: list[str] | None, option: str) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}
    for value in values or []:
        platform_name, separator, command = value.partition("=")
        if not separator or platform_name not in manifests.PLATFORMS or not command:
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
    before = copy.deepcopy(manifest)
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
        environment = parse_assignments(getattr(args, "omp_environment", None), "--omp-env", ENV_NAME)
        if environment:
            value["ompEnvironment"] = environment
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
            if getattr(args, "cwd", None) is not None:
                raise runtime.DotAiError("--cwd requires --command")
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
            if getattr(args, "cwd", None) is not None:
                value["cwd"] = args.cwd
        timeout = getattr(args, "timeout", None)
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
                raise runtime.DotAiError("--timeout must be a non-negative integer")
            value["timeout"] = timeout
        manifest["mcp"]["servers"][args.name] = value
    elif kind == "tool":
        installs = parse_platform_commands(args.install_commands, "--install")
        if not installs:
            raise runtime.DotAiError("At least one --install PLATFORM=COMMAND is required")
        value = {"name": args.name, "check": args.check_command, "install": installs}
        if args.update_group != "core":
            value["updateGroup"] = args.update_group
        minimum_version = getattr(args, "minimum_version", None)
        if minimum_version is not None:
            if not manifests.VERSION_MINIMUM_PATTERN.fullmatch(minimum_version):
                raise runtime.DotAiError("--minimum-version must be a major.minor or major.minor.patch version")
            value["minimumVersion"] = minimum_version
        updates = parse_platform_commands(args.update_commands, "--update")
        if updates:
            value["update"] = updates
        configures = parse_platform_commands(getattr(args, "configure_commands", None), "--configure")
        if configures:
            value["configure"] = configures
        upsert(manifest["packages"], "name", value)
    else:
        raise runtime.DotAiError(f"Unsupported integration kind: {kind}")
    if manifest != before or not path.exists():
        backup = manifests.write_manifest(path, manifest, backup=path.exists())
        if backup is not None:
            terminal.say(f"{terminal.badge('OK')} Manifest backup written to {backup}")
    terminal.say(f"{terminal.badge('OK')} Added {kind} to {path}. Run 'dotai sync' or 'dotai install' to apply it.")
    return 0


def _pop_skill(skills: list[dict[str, Any]], source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    for index, skill in enumerate(skills):
        if skill.get("source") == source:
            updated = list(skills)
            return updated, updated.pop(index)
    normalized = skill_manager.github_repository_source(source)
    if normalized is not None:
        matches = [
            index for index, skill in enumerate(skills)
            if skill_manager.github_repository_source(skill.get("source")) == normalized
        ]
        if len(matches) > 1:
            sources = ", ".join(skills[index]["source"] for index in matches)
            raise runtime.DotAiError(f"Ambiguous skill source '{source}'; matches multiple entries: {sources}")
        if len(matches) == 1:
            updated = list(skills)
            return updated, updated.pop(matches[0])
    raise runtime.DotAiError(f"Skill source not found in manifest: {source}")


def _pop_by_key(items: list[dict[str, Any]], key: str, identifier: str, label: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    for index, item in enumerate(items):
        if item.get(key) == identifier:
            updated = list(items)
            return updated, updated.pop(index)
    raise runtime.DotAiError(f"{label} not found in manifest: {identifier}")


def remove_integration(
    args: argparse.Namespace, manifest: dict[str, Any], path: Path, runner: runtime.Runner,
) -> int:
    kind = args.kind
    uninstall = getattr(args, "uninstall", False)
    if kind == "tool" and uninstall:
        raise runtime.DotAiError(
            "System packages are not uninstalled automatically; remove the manifest entry without --uninstall and use your package manager if needed"
        )
    updated = dict(manifest)
    if kind == "tool":
        updated["packages"], removed = _pop_by_key(manifest["packages"], "name", args.name, "Tool")
        target_label = f"tool {removed['name']}"
    elif kind == "skill":
        updated["skills"], removed = _pop_skill(manifest["skills"], args.source)
        target_label = f"skill {removed['source']}"
    elif kind == "marketplace":
        updated["marketplaces"], removed = _pop_by_key(manifest["marketplaces"], "name", args.name, "Marketplace")
        target_label = f"marketplace {removed['name']}"
    elif kind == "plugin":
        updated["plugins"], removed = _pop_by_key(manifest["plugins"], "id", args.id, "Plugin")
        target_label = f"plugin {removed['id']}"
    elif kind == "mcp":
        servers = dict(manifest["mcp"]["servers"])
        if args.name not in servers:
            raise runtime.DotAiError(f"MCP server not found in manifest: {args.name}")
        removed = servers.pop(args.name)
        updated["mcp"] = {**manifest["mcp"], "servers": servers}
        target_label = f"mcp {args.name}"
    else:
        raise runtime.DotAiError(f"Unsupported integration kind: {kind}")

    persisted = {**updated, "ompRouting": None} if updated.get("ompRouting") == {} else updated
    manifests.validate_manifest(persisted)

    environment: dict[str, str] | None = None
    if uninstall and kind == "skill":
        environment = skill_manager.declared_skill_environment(updated, runner)
        if environment is None or not omp_config.validate_skill_environment_target(runner):
            return 1
    elif uninstall and kind == "mcp":
        target, _ = mcp_config.desired_mcp(manifest)
        if target.exists():
            try:
                mcp_config.load_mcp_config(target)
            except (OSError, runtime.DotAiError) as exc:
                runner.failures.append(str(exc))
                runner.say(f"{terminal.badge('FAIL')} MCP: {exc}")
                return 1

    backup = None
    if runner.dry_run:
        runner.say(f"{terminal.badge('RUN')} Remove {target_label} from {path} (dry run)")
    else:
        backup = manifests.write_manifest(path, persisted, backup=True)
        runner.say(f"{terminal.badge('OK')} Manifest backup written to {backup}")
        runner.say(f"{terminal.badge('OK')} Removed {target_label} from {path}")

    if uninstall:
        if kind == "skill":
            skill_manager.remove_retired_skills(
                [{"kind": "remove", "source": removed["source"], "before": removed, "after": None}],
                runner,
            )
            if not runner.failures and environment is not None:
                omp_config.reconcile_skill_environment(environment, runner)
        elif kind == "mcp":
            try:
                mcp_config.remove_mcp_server(manifest, args.name, runner)
            except (OSError, runtime.DotAiError) as exc:
                runner.failures.append(str(exc))
                runner.say(f"{terminal.badge('FAIL')} MCP: {exc}")
        elif kind == "marketplace":
            omp_config.remove_marketplace(removed["name"], runner)
        elif kind == "plugin":
            omp_config.remove_plugin(removed, runner)

        if runner.failures:
            if backup is not None:
                shutil.copy2(backup, path)
                runner.say(f"{terminal.badge('OK')} Restored {path} after {kind} uninstall failure")
            return 1

    if runner.dry_run:
        runner.say(f"{terminal.badge('RUN')} Dry run: no manifest or machine changes applied.")
    return 0


COMMAND_SECTIONS: dict[str, tuple[str, ...]] = {
    "install": ("packages", "extensions", "skills", "plugins", "mcp"),
    "update": ("packages", "extensions", "skills", "plugins", "mcp"),
    "sync": ("extensions", "skills", "plugins", "mcp"),
    "status": ("packages", "skills", "plugins", "extensions", "routing", "mcp"),
}


def _match_skill_target(skills: list[dict[str, Any]], ident: str) -> list[int]:
    exact = [idx for idx, skill in enumerate(skills) if skill.get("source") == ident]
    if exact:
        return exact
    normalized = skill_manager.github_repository_source(ident)
    if normalized is not None:
        repo_matches = [
            idx for idx, skill in enumerate(skills)
            if skill_manager.github_repository_source(skill.get("source")) == normalized
        ]
        if len(repo_matches) > 1:
            sources = ", ".join(skills[idx]["source"] for idx in repo_matches)
            raise runtime.DotAiError(f"Ambiguous target: {ident} (matches skill sources: {sources})")
        if len(repo_matches) == 1:
            return repo_matches
    name_matches = [
        idx for idx, skill in enumerate(skills)
        if ident in skill.get("skills", []) or ident in skill.get("checkSkills", [])
    ]
    if len(name_matches) > 1:
        sources = ", ".join(skills[idx]["source"] for idx in name_matches)
        raise runtime.DotAiError(f"Ambiguous target: {ident} (matches skill sources: {sources})")
    return name_matches


def scope_manifest(
    manifest: dict[str, Any],
    command: str,
    only_sections: list[str] | None = None,
    targets: list[str] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    valid_sections = set(COMMAND_SECTIONS[command])
    active_sections = set(only_sections) & valid_sections if only_sections else set(valid_sections)
    if not targets:
        scoped = dict(manifest)
        if "packages" not in active_sections:
            scoped["packages"] = []
        if "extensions" not in active_sections:
            scoped["ompExtensions"] = []
        if "skills" not in active_sections:
            scoped["skills"] = []
        if "plugins" not in active_sections:
            scoped["marketplaces"] = []
            scoped["plugins"] = []
        if "mcp" not in active_sections:
            scoped["mcp"] = {**manifest["mcp"], "servers": {}}
        if "routing" not in active_sections:
            scoped["ompRouting"] = None
        return scoped, active_sections

    pkg_indices: set[int] = set()
    skill_indices: set[int] = set()
    mp_names: set[str] = set()
    plugin_indices: set[int] = set()
    mcp_names: set[str] = set()
    ext_indices: set[int] = set()

    packages = manifest.get("packages", [])
    skills = manifest.get("skills", [])
    marketplaces = manifest.get("marketplaces", [])
    plugins = manifest.get("plugins", [])
    servers = manifest.get("mcp", {}).get("servers", {})
    extensions = manifest.get("ompExtensions", [])

    for ident in targets:
        matched = False
        if "packages" in active_sections:
            exact_pkgs = [idx for idx, pkg in enumerate(packages) if pkg.get("name") == ident]
            if exact_pkgs:
                pkg_indices.update(exact_pkgs)
                matched = True
            else:
                lower = ident.lower()
                ci_pkgs = [idx for idx, pkg in enumerate(packages) if str(pkg.get("name", "")).lower() == lower]
                if len(ci_pkgs) > 1:
                    raise runtime.DotAiError(f"Ambiguous target: {ident}")
                if len(ci_pkgs) == 1:
                    pkg_indices.update(ci_pkgs)
                    matched = True
        if "skills" in active_sections:
            s_matches = _match_skill_target(skills, ident)
            if s_matches:
                skill_indices.update(s_matches)
                matched = True
        if "plugins" in active_sections:
            mp_hit = [mp["name"] for mp in marketplaces if mp.get("name") == ident]
            plug_hit = [idx for idx, plug in enumerate(plugins) if plug.get("id") == ident]
            if mp_hit:
                mp_names.update(mp_hit)
                matched = True
            if plug_hit:
                plugin_indices.update(plug_hit)
                for idx in plug_hit:
                    ref_mp = str(plugins[idx].get("id", "")).partition("@")[2]
                    if ref_mp:
                        mp_names.add(ref_mp)
                matched = True
        if "mcp" in active_sections and ident in servers:
            mcp_names.add(ident)
            matched = True
        if "extensions" in active_sections:
            ident_norm = omp_config.extension_identity(ident)
            ext_hit = [
                idx for idx, ext in enumerate(extensions)
                if ext == ident or omp_config.extension_identity(ext) == ident_norm
            ]
            if ext_hit:
                ext_indices.update(ext_hit)
                matched = True
        if not matched:
            raise runtime.DotAiError(f"Unknown target {ident!r} in active sections")

    narrowed_sections: set[str] = set()
    if pkg_indices:
        narrowed_sections.add("packages")
    if skill_indices:
        narrowed_sections.add("skills")
    if mp_names or plugin_indices:
        narrowed_sections.add("plugins")
    if mcp_names:
        narrowed_sections.add("mcp")
    if ext_indices:
        narrowed_sections.add("extensions")

    scoped = dict(manifest)
    scoped["packages"] = [pkg for idx, pkg in enumerate(packages) if idx in pkg_indices]
    scoped["skills"] = [skill for idx, skill in enumerate(skills) if idx in skill_indices]
    scoped["marketplaces"] = [mp for mp in marketplaces if mp.get("name") in mp_names]
    scoped["plugins"] = [plug for idx, plug in enumerate(plugins) if idx in plugin_indices]
    scoped["mcp"] = {
        **manifest["mcp"],
        "servers": {name: srv for name, srv in servers.items() if name in mcp_names},
    }
    scoped["ompExtensions"] = [ext for idx, ext in enumerate(extensions) if idx in ext_indices]
    scoped["ompRouting"] = None
    return scoped, narrowed_sections
