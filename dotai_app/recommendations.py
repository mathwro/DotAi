"""Review and apply repository-recommended skill and baseline stack changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
from . import manifest as manifests
from . import omp as omp_config
from . import runtime
from . import skills as skill_manager
from . import state as app_state
from . import terminal


def replace_skill(skills: list[dict[str, Any]], source: str, value: dict[str, Any] | None) -> None:
    for index, skill in enumerate(skills):
        if skill.get("source") == source:
            if value is None:
                skills.pop(index)
            else:
                skills[index] = value
            return
    if value is not None:
        skills.append(value)



def replace_package(packages: list[dict[str, Any]], name: str, value: dict[str, Any] | None) -> None:
    for index, package in enumerate(packages):
        if package.get("name") == name:
            if value is None:
                packages.pop(index)
            else:
                packages[index] = value
            return
    if value is not None:
        packages.append(value)


def replace_extension(extensions: list[str], item: str, present: bool) -> None:
    identity = omp_config.extension_identity(item)
    for index, existing in enumerate(extensions):
        if omp_config.extension_identity(existing) == identity:
            if not present:
                extensions.pop(index)
            return
    if present:
        extensions.append(item)


def recommended_skill_plan(
    manifest: dict[str, Any], manifest_path: Path, enforce: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    desired = manifests.load_manifest(manifests.EXAMPLE_MANIFEST)["skills"]
    stored = app_state.managed_recommendations(manifest_path)
    managed = stored if stored is not None else [skill for skill in desired if skill in manifest["skills"]]
    accepted_map, raw_changes, raw_conflicts = _plan_keyed_section(
        "skills",
        {skill["source"]: skill for skill in manifest["skills"]},
        {skill["source"]: skill for skill in desired},
        {skill["source"]: skill for skill in managed},
        enforce,
    )
    changes = [
        {"kind": item["kind"], "source": item["id"], "before": item["before"], "after": item["after"]}
        for item in raw_changes
    ]
    return list(accepted_map.values()), changes, [source for _, source in raw_conflicts]


def apply_recommended_skill_changes(
    manifest: dict[str, Any],
    managed: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    updated = dict(manifest)
    updated["skills"] = list(manifest["skills"])
    accepted = list(managed)
    for change in changes:
        replace_skill(updated["skills"], change["source"], change["after"])
        replace_skill(accepted, change["source"], change["after"])
    return updated, accepted


def print_recommended_skill_notice(manifest: dict[str, Any], manifest_path: Path) -> None:
    _, changes, conflicts = recommended_skill_plan(manifest, manifest_path)
    if not changes and not conflicts:
        return
    if changes:
        terminal.say(terminal.heading("Available recommended skill changes (not applied):"))
        for change in changes:
            skill = change["after"] if change["after"] is not None else change["before"]
            selections = ", ".join(skill.get("skills", ["*"]))
            terminal.say(f"  {change['kind'].capitalize()} {change['source']} ({selections})")
    for source in conflicts:
        terminal.say(f"{terminal.badge('DRIFT')} Recommended source {source}: local entry differs; preserving it")
    terminal.say("Review with sync --recommended-skills --dry-run; apply with sync --recommended-skills.")
    if conflicts:
        terminal.say("Add --enforce to explicitly adopt recommendations for locally differing sources.")


def _apply_skill_changes(
    manifest: dict[str, Any], manifest_path: Path, runner: runtime.Runner,
    managed: list[dict[str, Any]], selected: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    updated, accepted = apply_recommended_skill_changes(manifest, managed, selected)
    backup = None
    if not runner.dry_run:
        backup = manifests.write_manifest(manifest_path, updated, backup=True)
        terminal.say(f"{terminal.badge('OK')} Manifest backup written to {backup}")
    skill_manager.remove_retired_skills(selected, runner)
    if runner.failures:
        if backup is not None:
            shutil.copy2(backup, manifest_path)
            terminal.say(f"{terminal.badge('OK')} Restored {manifest_path} after skill removal failure")
        return manifest, managed
    if not runner.dry_run:
        app_state.save_managed_recommendations(manifest_path, accepted)
    return updated, accepted


def review_recommended_skills(
    manifest: dict[str, Any], manifest_path: Path, runner: runtime.Runner, enforce: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    managed, changes, conflicts = recommended_skill_plan(manifest, manifest_path, enforce)
    conditional_cleanup = False
    if enforce:
        recommended_sources = {skill["source"] for skill in manifests.load_manifest(manifests.EXAMPLE_MANIFEST)["skills"]}
        managed_sources = {skill["source"] for skill in managed}
        user_skills = [
            skill for skill in manifest["skills"]
            if skill["source"] not in recommended_sources and skill["source"] not in managed_sources
        ]
        if user_skills:
            terminal.say(terminal.heading("User-owned skill sources outside the recommendations:"))
            for skill in user_skills:
                for name in skill.get("skills", ["*"]):
                    suffix = f"/{name}" if name != "*" else ""
                    terminal.say(f"  {skill['source']}{suffix}")
            removals = [
                {"kind": "remove", "source": skill["source"], "before": skill, "after": None}
                for skill in user_skills
            ]
            if runner.dry_run:
                terminal.say(f"{terminal.badge('RUN')} Optional cleanup preview: applied only if removal is confirmed; no files changed.")
                remove_users = True
                conditional_cleanup = True
            else:
                try:
                    answer = input("Remove these sources from the manifest and their installed skills? [y/N] ")
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                remove_users = answer.strip().lower() in {"y", "yes"}
            if remove_users:
                manifest, managed = _apply_skill_changes(manifest, manifest_path, runner, managed, removals)
                if runner.failures:
                    return manifest, managed
                managed, changes, conflicts = recommended_skill_plan(manifest, manifest_path, enforce)
            else:
                terminal.say(f"{terminal.badge('INACTIVE')} Preserving user-owned skills; skipping their installation during enforced sync.")
    for source in conflicts:
        terminal.say(f"{terminal.badge('DRIFT')} Recommended source {source}: local entry was modified; preserving it")
    if not changes:
        terminal.say(f"{terminal.badge('OK')} Recommended skills: no changes available")
        return manifest, managed

    proposed, _ = apply_recommended_skill_changes(manifest, managed, changes)
    title = "Proposed recommended skill changes"
    if conditional_cleanup:
        title += ", conditional on approving the cleanup above"
    runner.say(terminal.heading(title + ":"))
    runner.say(terminal.describe_skill_changes(manifest["skills"], proposed["skills"]))
    if runner.dry_run:
        selected = changes
        terminal.say(f"{terminal.badge('RUN')} Dry run: no manifest or skill changes applied.")
    else:
        try:
            answer = input("Apply [a]ll, review [e]ach, or [n]one? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in {"a", "all"}:
            selected = changes
        elif answer in {"e", "each", "r", "review"}:
            selected = []
            for change in changes:
                try:
                    answer = input(terminal.redact(f"Apply {change['kind']} for {change['source']}? [y/N] "))
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer.strip().lower() in {"y", "yes"}:
                    selected.append(change)
        else:
            selected = []

    if not selected:
        terminal.say(f"{terminal.badge('OK')} No recommended skill changes applied.")
        return manifest, managed

    return _apply_skill_changes(manifest, manifest_path, runner, managed, selected)


def _plan_keyed_section(
    section: str,
    local_map: dict[str, dict[str, Any]],
    wanted_map: dict[str, dict[str, Any]],
    managed_map: dict[str, dict[str, Any]],
    enforce: bool,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[tuple[str, str]]]:
    accepted = dict(managed_map)
    changes: list[dict[str, Any]] = []
    conflicts: list[tuple[str, str]] = []

    for item_id, before in managed_map.items():
        after = wanted_map.get(item_id)
        current = local_map.get(item_id)
        if after is None:
            if current is None or current == before:
                changes.append({"section": section, "kind": "remove", "id": item_id, "before": before, "after": None})
            elif enforce:
                changes.append({"section": section, "kind": "remove", "id": item_id, "before": current, "after": None})
            else:
                conflicts.append((section, item_id))
        elif after != before:
            if current == before:
                changes.append({"section": section, "kind": "update", "id": item_id, "before": before, "after": after})
            elif current == after:
                accepted[item_id] = after
            elif enforce:
                changes.append({
                    "section": section,
                    "kind": "add" if current is None else "update",
                    "id": item_id,
                    "before": current,
                    "after": after,
                })
            else:
                conflicts.append((section, item_id))
        elif current != before:
            if enforce:
                changes.append({
                    "section": section,
                    "kind": "add" if current is None else "update",
                    "id": item_id,
                    "before": current,
                    "after": after,
                })
            else:
                conflicts.append((section, item_id))

    for item_id, after in wanted_map.items():
        if item_id in managed_map:
            continue
        current = local_map.get(item_id)
        if current is None:
            changes.append({"section": section, "kind": "add", "id": item_id, "before": None, "after": after})
        elif current == after:
            accepted[item_id] = after
        elif enforce:
            changes.append({"section": section, "kind": "update", "id": item_id, "before": current, "after": after})
        else:
            conflicts.append((section, item_id))

    return accepted, changes, conflicts


def recommended_stack_plan(
    manifest: dict[str, Any], manifest_path: Path, enforce: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, str]]]:
    example = manifests.load_manifest(manifests.EXAMPLE_MANIFEST)
    desired_packages = example["packages"]
    desired_servers = example["mcp"]["servers"]
    desired_extensions = example.get("ompExtensions", [])

    stored = app_state.managed_stack_baseline(manifest_path)
    local_packages_list = manifest["packages"]
    local_servers = manifest["mcp"]["servers"]
    local_extensions = manifest.get("ompExtensions", [])

    if stored is not None:
        managed_packages = stored["packages"]
        managed_servers = stored["mcpServers"]
        managed_extensions = stored["ompExtensions"]
    else:
        managed_packages = [pkg for pkg in desired_packages if pkg in local_packages_list]
        managed_servers = {
            name: server for name, server in desired_servers.items() if local_servers.get(name) == server
        }
        local_ext_ids = {omp_config.extension_identity(ext) for ext in local_extensions}
        managed_extensions = [ext for ext in desired_extensions if omp_config.extension_identity(ext) in local_ext_ids]

    accepted_pkg_map, pkg_changes, pkg_conflicts = _plan_keyed_section(
        "packages",
        {pkg["name"]: pkg for pkg in local_packages_list},
        {pkg["name"]: pkg for pkg in desired_packages},
        {pkg["name"]: pkg for pkg in managed_packages},
        enforce,
    )
    accepted_skills, raw_skill_changes, raw_skill_conflicts = recommended_skill_plan(
        manifest, manifest_path, enforce
    )
    skill_changes = [
        {
            "section": "skills",
            "kind": item["kind"],
            "id": item["source"],
            "source": item["source"],
            "before": item["before"],
            "after": item["after"],
        }
        for item in raw_skill_changes
    ]
    skill_conflicts = [("skills", source) for source in raw_skill_conflicts]

    accepted_mcp, mcp_changes, mcp_conflicts = _plan_keyed_section(
        "mcpServers",
        local_servers,
        desired_servers,
        managed_servers,
        enforce,
    )

    accepted_extensions = list(managed_extensions)
    ext_changes: list[dict[str, Any]] = []
    ext_conflicts: list[tuple[str, str]] = []
    local_ext_ids = {omp_config.extension_identity(ext) for ext in local_extensions}
    wanted_ext_ids = {omp_config.extension_identity(ext) for ext in desired_extensions}
    managed_ext_ids = {omp_config.extension_identity(ext) for ext in managed_extensions}
    for ext in managed_extensions:
        ext_id = omp_config.extension_identity(ext)
        if ext_id not in wanted_ext_ids:
            ext_changes.append({"section": "ompExtensions", "kind": "remove", "id": ext, "before": ext, "after": None})
        elif ext_id not in local_ext_ids:
            if enforce:
                ext_changes.append({"section": "ompExtensions", "kind": "add", "id": ext, "before": None, "after": ext})
            else:
                ext_conflicts.append(("ompExtensions", ext))
    for ext in desired_extensions:
        ext_id = omp_config.extension_identity(ext)
        if ext_id in managed_ext_ids:
            continue
        if ext_id in local_ext_ids:
            replace_extension(accepted_extensions, ext, True)
        else:
            ext_changes.append({"section": "ompExtensions", "kind": "add", "id": ext, "before": None, "after": ext})
    baseline = {
        "packages": list(accepted_pkg_map.values()),
        "skills": accepted_skills,
        "mcpServers": accepted_mcp,
        "ompExtensions": accepted_extensions,
    }
    changes = [*pkg_changes, *skill_changes, *mcp_changes, *ext_changes]
    conflicts = [*pkg_conflicts, *skill_conflicts, *mcp_conflicts, *ext_conflicts]
    return baseline, changes, conflicts


def apply_recommended_stack_changes(
    manifest: dict[str, Any],
    baseline: dict[str, Any],
    changes: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(manifest)
    updated["packages"] = list(manifest["packages"])
    updated["skills"] = list(manifest["skills"])
    updated["mcp"] = {**manifest["mcp"], "servers": dict(manifest["mcp"]["servers"])}
    if "ompExtensions" in manifest or any(c["section"] == "ompExtensions" for c in changes):
        updated["ompExtensions"] = list(manifest.get("ompExtensions", []))

    accepted_packages = list(baseline["packages"])
    accepted_skills = list(baseline.get("skills", []))
    accepted_mcp = dict(baseline["mcpServers"])
    accepted_extensions = list(baseline["ompExtensions"])

    for change in changes:
        section = change["section"]
        item_id = change["id"]
        after = change["after"]
        if section == "packages":
            replace_package(updated["packages"], item_id, after)
            replace_package(accepted_packages, item_id, after)
        elif section == "skills":
            replace_skill(updated["skills"], item_id, after)
            replace_skill(accepted_skills, item_id, after)
        elif section == "mcpServers":
            if after is None:
                updated["mcp"]["servers"].pop(item_id, None)
                accepted_mcp.pop(item_id, None)
            else:
                updated["mcp"]["servers"][item_id] = after
                accepted_mcp[item_id] = after
        elif section == "ompExtensions":
            present = after is not None
            replace_extension(updated.setdefault("ompExtensions", []), item_id, present)
            replace_extension(accepted_extensions, item_id, present)

    return updated, {
        "packages": accepted_packages,
        "skills": accepted_skills,
        "mcpServers": accepted_mcp,
        "ompExtensions": accepted_extensions,
    }


def _change_label(change: dict[str, Any]) -> str:
    section = change["section"]
    item_id = change["id"]
    if section == "packages":
        return f"package {item_id}"
    if section == "skills":
        skill = change["after"] if change["after"] is not None else change["before"]
        selections = ", ".join(skill.get("skills", ["*"]))
        return f"skill source {item_id} ({selections})"
    if section == "mcpServers":
        server = change["after"] if change["after"] is not None else change["before"]
        transport = server.get("type", "stdio") if isinstance(server, dict) else "stdio"
        return f"MCP server {item_id} ({transport})"
    return f"OMP extension {item_id}"


def _conflict_label(section: str, item_id: str) -> str:
    if section == "packages":
        return f"Recommended package {item_id}"
    if section == "skills":
        return f"Recommended source {item_id}"
    if section == "mcpServers":
        return f"Recommended MCP server {item_id}"
    return f"Recommended OMP extension {item_id}"


def print_recommended_stack_notice(manifest: dict[str, Any], manifest_path: Path, mode: str) -> None:
    if not manifests.EXAMPLE_MANIFEST.is_file():
        return
    _, changes, conflicts = recommended_stack_plan(manifest, manifest_path)
    if not changes and not conflicts:
        return
    if changes:
        terminal.say(terminal.heading("Available recommended baseline changes (not applied):"))
        for change in changes:
            terminal.say(f"  {change['kind'].capitalize()} {_change_label(change)}")
    for section, item_id in conflicts:
        terminal.say(f"{terminal.badge('DRIFT')} {_conflict_label(section, item_id)}: local entry differs; preserving it")
    terminal.say(f"Review with {mode} --recommended --dry-run; apply with {mode} --recommended.")
    if conflicts:
        terminal.say("Add --enforce to explicitly adopt recommendations for locally differing entries.")


def review_recommended_stack(
    manifest: dict[str, Any],
    manifest_path: Path,
    runner: runtime.Runner,
    enforce: bool = False,
    mode: str = "update",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    baseline, changes, conflicts = recommended_stack_plan(manifest, manifest_path, enforce)
    stack_only = {
        "packages": baseline["packages"],
        "mcpServers": baseline["mcpServers"],
        "ompExtensions": baseline["ompExtensions"],
    }
    for section, item_id in conflicts:
        terminal.say(f"{terminal.badge('DRIFT')} {_conflict_label(section, item_id)}: local entry differs; preserving it")
    if not changes:
        terminal.say(f"{terminal.badge('OK')} Recommended baseline: no changes available")
        return manifest, baseline["skills"], stack_only

    proposed, _ = apply_recommended_stack_changes(manifest, baseline, changes)
    runner.say(terminal.heading("Proposed recommended baseline changes:"))
    runner.say(terminal.describe_stack_changes(manifest, proposed))
    if runner.dry_run:
        selected = changes
        terminal.say(f"{terminal.badge('RUN')} Dry run: no manifest or baseline changes applied.")
    else:
        try:
            answer = input("Apply [a]ll, review [e]ach, or [n]one? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in {"a", "all"}:
            selected = changes
        elif answer in {"e", "each", "r", "review"}:
            selected = []
            for change in changes:
                prompt_target = (
                    f"package {change['id']}" if change["section"] == "packages"
                    else f"skills from {change['id']}" if change["section"] == "skills"
                    else f"MCP server {change['id']}" if change["section"] == "mcpServers"
                    else f"OMP extension {change['id']}"
                )
                try:
                    reply = input(terminal.redact(f"Apply {change['kind']} for {prompt_target}? [y/N] "))
                except (EOFError, KeyboardInterrupt):
                    reply = ""
                if reply.strip().lower() in {"y", "yes"}:
                    selected.append(change)
        else:
            selected = []

    if not selected:
        terminal.say(f"{terminal.badge('OK')} No recommended baseline changes applied.")
        return manifest, baseline["skills"], stack_only

    updated, accepted = apply_recommended_stack_changes(manifest, baseline, selected)
    backup = None
    if not runner.dry_run:
        backup = manifests.write_manifest(manifest_path, updated, backup=True)
        terminal.say(f"{terminal.badge('OK')} Manifest backup written to {backup}")
    skill_selected = [change for change in selected if change["section"] == "skills"]
    if skill_selected:
        skill_manager.remove_retired_skills(skill_selected, runner)
        if runner.failures:
            if backup is not None:
                shutil.copy2(backup, manifest_path)
                terminal.say(f"{terminal.badge('OK')} Restored {manifest_path} after skill removal failure")
            return manifest, baseline["skills"], stack_only
    accepted_stack = {
        "packages": accepted["packages"],
        "mcpServers": accepted["mcpServers"],
        "ompExtensions": accepted["ompExtensions"],
    }
    if not runner.dry_run:
        app_state.save_managed_recommendations(manifest_path, accepted["skills"])
        app_state.save_managed_stack_baseline(manifest_path, accepted_stack)
    if mode == "sync" and any(change["section"] == "packages" for change in selected):
        terminal.say(
            f"{terminal.badge('INACTIVE')} Package entries changed; sync skips package commands. "
            "Run 'dotai install' or 'dotai update' to apply package changes."
        )
    return updated, accepted["skills"], accepted_stack
