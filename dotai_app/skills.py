"""Agent-scoped skill installation, ownership, health, and migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re
import stat
from . import manifest as manifests
from . import runtime
from . import terminal


def skill_command(skill: dict[str, Any]) -> list[str]:
    command = [
        "npx",
        "--yes",
        "skills@latest",
        "add",
        skill["source"],
        "--global",
        "--agent",
        skill.get("agent", "universal"),
    ]
    wanted = skill.get("skills", ["*"])
    for name in wanted:
        command.extend(["--skill", name])
    command.extend(["--copy", "--yes"])
    return command


def skill_root(skill: dict[str, Any]) -> Path:
    agent = skill.get("agent", "universal")
    roots = {
        "pi": runtime.home_dir() / ".pi" / "agent" / "skills",
        "universal": runtime.home_dir() / ".agents" / "skills",
    }
    return roots.get(agent, runtime.home_dir() / f".{agent}" / "skills")


def github_repository_source(source: Any) -> str | None:
    """Normalize only public GitHub repository roots, not refs, subpaths or other hosts."""
    if not isinstance(source, str):
        return None
    if source.startswith("https://github.com/"):
        source = source[len("https://github.com/"):].removesuffix("/").removesuffix(".git")
    else:
        source = source.removeprefix("github:").removesuffix("/")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", source):
        return None
    return source.lower()


def installed_skill_tree_hash(folder: Path) -> str:
    """Reconstruct the Git tree SHA recorded by skills.sh for GitHub skill folders."""
    entries = []
    for path in folder.iterdir():
        mode = path.lstat().st_mode
        name = path.name.encode("utf-8")
        if stat.S_ISDIR(mode):
            digest = bytes.fromhex(installed_skill_tree_hash(path))
            entry_mode, sort_name = b"40000", name + b"/"
        elif stat.S_ISREG(mode):
            content = path.read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).digest()
            entry_mode, sort_name = (b"100755" if mode & stat.S_IXUSR else b"100644"), name
        else:
            # Copies dereference symlinks; their original tree cannot be proven locally.
            raise ValueError("unsupported installed skill file type")
        entries.append((sort_name, entry_mode + b" " + name + b"\0" + digest))
    content = b"".join(entry for _, entry in sorted(entries))
    return hashlib.sha1(b"tree " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def skill_source_owned(skill: dict[str, Any], owners: dict[str, Any]) -> bool:
    source = github_repository_source(skill["source"])
    checks = skill.get("checkSkills", [])
    if source is None or not checks:
        return False
    # Shorthand can select an Enterprise host; never attribute it to a public GitHub lock.
    if not skill["source"].startswith("https://github.com/") and os.environ.get("GH_HOST", "").strip().lower() not in {"", "github.com"}:
        return False
    root = skill_root(skill)
    for name in checks:
        entry = owners.get(name)
        if not isinstance(entry, dict) or (
            entry.get("sourceType") != "github"
            or github_repository_source(entry.get("source")) != source
            or github_repository_source(entry.get("sourceUrl")) != source
            or entry.get("ref") is not None
        ):
            return False
        folder_hash = entry.get("skillFolderHash")
        if not isinstance(folder_hash, str) or not re.fullmatch(r"[0-9a-f]{40}", folder_hash):
            return False
        try:
            if installed_skill_tree_hash(root / name) != folder_hash:
                return False
        except (OSError, ValueError, UnicodeError):
            return False
    return True


def reconcile_skills(
    manifest: dict[str, Any], runner: runtime.Runner, *, update_skills: bool = False,
    refresh_sources: set[str] | None = None,
) -> None:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    lock_path = Path(xdg_state) / "skills" / ".skill-lock.json" if xdg_state else runtime.home_dir() / ".agents" / ".skill-lock.json"
    try:
        lock = manifests.load_json_object(lock_path)
    except (OSError, runtime.DotAiError):
        lock = {}
    owned = lock.get("skills") if lock.get("version") == 3 else None
    owners = owned if isinstance(owned, dict) else {}
    for skill in manifest["skills"]:
        refresh = update_skills or (refresh_sources is not None and skill["source"] in refresh_sources)
        if not refresh and skill_status(skill)[0] and skill_source_owned(skill, owners):
            print(f"{terminal.badge('OK')} Skills from {skill['source']}: already installed")
            continue
        runner.run(skill_command(skill), f"Reconcile skills from {skill['source']}")


def agent_display_matches(agent: str, display: Any) -> bool:
    if not isinstance(display, str):
        return False
    expected = re.sub(r"[^a-z0-9]", "", agent.lower())
    actual = re.sub(r"[^a-z0-9]", "", display.lower())
    return actual == expected or (
        len(expected) >= 3 and (actual.startswith(expected) or actual.endswith(expected))
    )


def installed_skill_names(agent: str, runner: runtime.Runner, source: str | None = None) -> set[str] | None:
    raw = runner.output(
        ["npx", "--yes", "skills@latest", "list", "--global", "--agent", agent, "--json"]
    )
    try:
        installed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(installed, list) or any(not isinstance(skill, dict) for skill in installed):
        return None
    universal_root = (runtime.home_dir() / ".agents" / "skills").resolve()
    return {
        skill["name"]
        for skill in installed
        if isinstance(skill.get("name"), str)
        and (
            (
                agent == "universal"
                and isinstance(skill.get("path"), str)
                and Path(skill["path"]).resolve().is_relative_to(universal_root)
            )
            or (
                isinstance(skill.get("agents"), list)
                and any(agent_display_matches(agent, display) for display in skill["agents"])
            )
        )
        and (source is None or skill.get("source") == source)
    }


def remove_retired_skills(changes: list[dict[str, Any]], runner: runtime.Runner) -> None:
    for change in changes:
        before = change["before"]
        after = change["after"]
        if before is None:
            continue
        wanted_before = before.get("skills", ["*"])
        wanted_after = after.get("skills", ["*"]) if after else []
        same_agent = after is not None and (
            before.get("agent", "universal") == after.get("agent", "universal")
        )
        if same_agent and "*" in wanted_after:
            continue
        if runner.dry_run:
            if "*" in wanted_before:
                print(
                    f"{terminal.badge('RUN')} Remove retired skills from {before['source']}: "
                    "installed names resolved when applied"
                )
                continue
            names = set(wanted_before) - set(wanted_after) if same_agent else set(wanted_before)
        else:
            agent = before.get("agent", "universal")
            installed = installed_skill_names(agent, runner, before["source"])
            if installed is None:
                runner.failures.append(f"Unable to list installed skills from {before['source']}")
                print(f"{terminal.badge('FAIL')} Unable to identify installed skills from {before['source']}")
                continue
            names = installed if "*" in wanted_before else installed.intersection(wanted_before)
            if same_agent:
                names -= set(wanted_after)
        if names:
            command = [
                "npx",
                "--yes",
                "skills@latest",
                "remove",
                *sorted(names),
                "--global",
                *([] if before.get("agent", "universal") == "universal" else ["--agent", before["agent"]]),
                "--yes",
            ]
            failure_count = len(runner.failures)
            runner.run(command, f"Remove retired skills from {before['source']}")
            if runner.dry_run:
                continue
            if len(runner.failures) != failure_count:
                continue
            agent = before.get("agent", "universal")
            remaining = installed_skill_names(agent, runner)
            if remaining is None:
                runner.failures.append(f"Unable to verify retired skills from {before['source']}")
                print(f"{terminal.badge('FAIL')} Unable to verify retired skills from {before['source']}")
                continue
            leftover = names.intersection(remaining)
            if leftover:
                detail = f"Retired skills still installed for {agent}: {', '.join(sorted(leftover))}"
                runner.failures.append(detail)
                print(f"{terminal.badge('FAIL')} {detail}")


def legacy_skill_sources(manifest: dict[str, Any]) -> list[str]:
    return [skill["source"] for skill in manifest["skills"] if skill.get("agent") == "pi"]


def print_legacy_skill_notice(manifest: dict[str, Any]) -> bool:
    sources = legacy_skill_sources(manifest)
    if not sources:
        return False
    print(f"{terminal.badge('DRIFT')} Legacy Pi skill targets: {', '.join(sources)}")
    print("  Run 'dotai fix' to review and migrate them to the OMP universal target.")
    return True


def skill_status(skill: dict[str, Any]) -> tuple[bool, str]:
    agent = skill.get("agent", "universal")
    root = skill_root(skill)
    checks = skill.get("checkSkills", [])
    if not checks:
        return False, f"unverified for {agent}: no named skills configured to check"
    if "*" in checks:
        return False, f"unverified for {agent}: wildcard skills cannot be checked"
    if all((root / name / "SKILL.md").is_file() for name in checks):
        return True, f"installed for {agent}"
    plugin_cache = runtime.home_dir() / ".codex" / "plugins" / "cache"
    if checks and plugin_cache.is_dir():
        found_elsewhere = all(next(plugin_cache.glob(f"**/skills/{name}/SKILL.md"), None) for name in checks)
        if found_elsewhere:
            return False, f"installed as a Codex plugin, but inactive in OMP; not installed for {agent}"
    return False, f"not installed for {agent}"


def legacy_skill_migration(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    updated = dict(manifest)
    migrated: list[str] = []
    skills = []
    for skill in manifest["skills"]:
        if skill.get("agent") == "pi":
            migrated.append(skill["source"])
            skills.append({**skill, "agent": "universal"})
        else:
            skills.append(skill)
    updated["skills"] = skills
    return updated, migrated


def fix_legacy_skills(manifest: dict[str, Any], path: Path, runner: runtime.Runner) -> int:
    updated, migrated = legacy_skill_migration(manifest)
    if not migrated:
        print(f"{terminal.badge('OK')} No legacy Pi-targeted skills found in {path}.")
        return 0

    print(f"{terminal.heading('Proposed skill migration:')}")
    print(manifests.manifest_diff(manifest, updated, path))
    print(f"\nMigrates {len(migrated)} skill source(s) from Pi to the OMP universal target.")
    if runner.dry_run:
        print(f"{terminal.badge('RUN')} Dry run: no manifest changes applied.")
        reconcile_skills(updated, runner)
        return 1 if runner.failures else 0
    try:
        answer = input("Apply these changes and install the migrated skills? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        print(f"{terminal.badge('OK')} No changes applied.")
        return 0

    backup = manifests.write_manifest(path, updated, backup=True)
    print(f"{terminal.badge('OK')} Manifest backup written to {backup}")
    reconcile_skills(updated, runner)
    if runner.failures:
        print(f"{terminal.styled('Skill migration failed:', 'red', 'bold')}")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1
    print(f"{terminal.styled('Skill migration complete.', 'green', 'bold')}")
    return 0


def installed_skill_name(name: str) -> str:
    """Match the skills installer's directory normalization for named selections."""
    sanitized = re.sub(r"[^a-z0-9._]+", "-", name.lower()).strip(".-")
    return sanitized[:255] or "unnamed-skill"
