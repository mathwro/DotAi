"""Agent-scoped skill installation, ownership, health, and migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import hashlib
import json
import os
import re
import shutil
import stat
from . import manifest as manifests
from . import runtime
from . import terminal


def skill_command(skill: dict[str, Any]) -> list[str]:
    source = skill["source"]
    revision = skill.get("revision")
    if revision:
        repository = github_repository_source(source)
        if repository is None or not re.fullmatch(r"[A-Za-z0-9._-]+", revision):
            raise runtime.DotAiError(f"Cannot pin skill source {source}: use a GitHub repository root and an unambiguous revision")
        source = f"https://github.com/{repository}/tree/{revision}"
    command = [
        "npx",
        "--yes",
        f"skills@{skill.get('installerVersion', 'latest')}",
        "add",
        source,
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
    if root.resolve() != root:
        return False
    for name in checks:
        folder = root / name
        if Path(name).name != name or name in {".", ".."} or folder.resolve() != folder:
            return False
        entry = owners.get(name)
        if not isinstance(entry, dict) or (
            entry.get("sourceType") != "github"
            or github_repository_source(entry.get("source")) != source
            or github_repository_source(entry.get("sourceUrl")) != source
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


def skill_owners() -> dict[str, Any]:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    lock_path = Path(xdg_state) / "skills" / ".skill-lock.json" if xdg_state else runtime.home_dir() / ".agents" / ".skill-lock.json"
    try:
        lock = manifests.load_json_object(lock_path)
    except (OSError, UnicodeError, runtime.DotAiError):
        return {}
    owned = lock.get("skills") if lock.get("version") == 3 else None
    return owned if isinstance(owned, dict) else {}


def reconcile_skills(
    manifest: dict[str, Any], runner: runtime.Runner, *, mode: str = "sync", update_skills: bool = False,
    refresh_sources: set[str] | None = None, recommended_only: bool = False,
    receipt_owned: Callable[[dict[str, Any]], bool] | None = None,
    deactivate: Callable[..., bool] | None = None,
    record_installed: Callable[..., None] | None = None,
) -> None:
    recommended_sources = {
        skill["source"] for skill in manifests.recommended_skills()
    } if recommended_only else None
    owners = skill_owners()
    for skill in manifest["skills"]:
        if recommended_sources is not None and skill["source"] not in recommended_sources:
            print(f"{terminal.badge('INACTIVE')} Skills from {skill['source']}: preserved; skipped during enforced sync")
            continue
        if skill.get("enabled") is False:
            if deactivate is not None:
                deactivate("skill", skill["source"], skill, manifest, runner)
            else:
                detail = f"Skills from {skill['source']}: disabling requires ownership verification; use 'dotai disable skill:{skill['source']}'"
                runner.failures.append(detail)
                print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        refresh = mode == "update" or update_skills or (refresh_sources is not None and skill["source"] in refresh_sources)
        root = skill_root(skill)
        if root.resolve() != root:
            detail = f"Skills from {skill['source']}: agent root is redirected; resolve {root} before installation"
            runner.failures.append(detail)
            print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        checks = skill.get("checkSkills", [])
        existing = [name for name in checks if (skill_root(skill) / name).exists() or (skill_root(skill) / name).is_symlink()]
        current = {**skill, "checkSkills": existing}
        owned = skill_source_owned(current, owners) or (receipt_owned is not None and receipt_owned(current))
        if existing and not owned:
            detail = f"Skills from {skill['source']}: existing content has unknown provenance or local modifications; adopt matching content or resolve it manually before updating"
            runner.failures.append(detail)
            print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        if not checks:
            detail = f"Skills from {skill['source']}: selections are unverified; declare --check-skill names before installation"
            runner.failures.append(detail)
            print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        if not refresh and skill_status(skill)[0] and owned:
            print(f"{terminal.badge('OK')} Skills from {skill['source']}: already installed; retained current revision")
            continue
        selections = skill.get("skills", ["*"])
        if not selections or "*" in selections:
            detail = f"Skills from {skill['source']}: wildcard mutation cannot prove every installer destination; declare individual --skill selections before installation or refresh"
            runner.failures.append(detail)
            print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        hidden = {installed_skill_name(name) for name in selections} - set(checks)
        if any((root / name).exists() or (root / name).is_symlink() for name in hidden):
            detail = f"Skills from {skill['source']}: existing installer-normalized destinations differ from authoritative checks; resolve directory mappings before installing"
            runner.failures.append(detail)
            print(f"{terminal.badge('DRIFT')} {detail}")
            continue
        install = skill
        missing = [name for name in checks if name not in existing]
        if not refresh and existing and missing:
            mapped = {installed_skill_name(name): name for name in selections}
            if "*" in selections or len(mapped) != len(selections) or set(mapped) != set(checks):
                detail = f"Skills from {skill['source']}: cannot map missing check directories to installer selections safely; use unambiguous named selections or update explicitly"
                runner.failures.append(detail)
                print(f"{terminal.badge('DRIFT')} {detail}")
                continue
            install = {**skill, "skills": [mapped[name] for name in missing], "checkSkills": missing}
        result = runner.run(skill_command(install), f"{'Update' if refresh else 'Install missing'} skills from {skill['source']}")
        if not runner.dry_run and result is not None and result.returncode == 0 and record_installed is not None:
            record_installed("skill", skill["source"], install, manifest, runner)


def agent_display_matches(agent: str, display: Any) -> bool:
    if not isinstance(display, str):
        return False
    expected = re.sub(r"[^a-z0-9]", "", agent.lower())
    actual = re.sub(r"[^a-z0-9]", "", display.lower())
    return actual == expected or (
        len(expected) >= 3 and (actual.startswith(expected) or actual.endswith(expected))
    )


def installed_skill_records(agent: str, runner: runtime.Runner, source: str | None = None, *, installer_version: str = "latest") -> list[dict[str, Any]] | None:
    raw = runner.output(
        ["npx", "--yes", f"skills@{installer_version}", "list", "--global", "--agent", agent, "--json"]
    )
    try:
        installed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(installed, list) or any(not isinstance(skill, dict) for skill in installed):
        return None
    root = skill_root({"agent": agent}).resolve()
    canonical = skill_root({"agent": "universal"}).resolve()
    records = []
    for skill in installed:
        if source is not None and skill.get("source") != source:
            continue
        if (
            not isinstance(skill.get("name"), str) or not skill["name"]
            or skill.get("scope") not in ("global", "project")
            or not isinstance(skill.get("path"), str) or not skill["path"]
            or "\0" in skill["path"]
            or not isinstance(skill.get("agents"), list)
            or any(not isinstance(display, str) for display in skill["agents"])
        ):
            return None
        if skill["scope"] != "global":
            continue
        if agent != "universal" and not any(agent_display_matches(agent, display) for display in skill["agents"]):
            continue
        path = Path(skill["path"])
        parent = path.parent.resolve()
        if agent == "universal" and parent == skill_root({"agent": "pi"}).resolve() and any(agent_display_matches("pi", display) for display in skill["agents"]):
            continue
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            return None
        if parent not in {root, canonical}:
            # The installer can include other agents despite --agent. Ignore
            # them for verification, but never trust foreign deletion targets.
            if source is not None:
                return None
            continue
        if agent == "universal" and parent != canonical:
            continue
        records.append(skill)
    return records


def retirement_directory(skill: dict[str, Any], record: dict[str, Any]) -> Path:
    """Allow copy retirement only inside the configured agent's unshared root."""
    agent = skill.get("agent", "universal")
    root = skill_root(skill)
    folder = root / Path(record["path"]).name
    if root.resolve() != root or folder.resolve() != folder:
        raise runtime.DotAiError(f"Cannot retire linked skill directory: {folder}")
    if agent == "universal":
        for display in record["agents"]:
            if agent_display_matches("universal", display):
                continue
            # Pi is an existing separately owned root. Unknown agent paths
            # cannot establish that the canonical folder is an independent copy.
            other = skill_root({"agent": "pi"}) / folder.name
            if not agent_display_matches("pi", display) or not other.is_dir() or other.resolve() == folder:
                raise runtime.DotAiError(f"Cannot retire shared or ambiguous skill directory: {folder}")
    return folder


def remove_retired_skills(changes: list[dict[str, Any]], runner: runtime.Runner) -> None:
    for change in changes:
        before = change["before"]
        after = change["after"]
        if before is None:
            continue
        wanted_before = before.get("skills", ["*"])
        wanted_after = after.get("skills", ["*"]) if after else []
        names_before = set(before.get("checkSkills") or [installed_skill_name(name) for name in wanted_before])
        names_after = set(after.get("checkSkills") or [installed_skill_name(name) for name in wanted_after]) if after else set()
        agent = before.get("agent", "universal")
        same_agent = after is not None and agent == after.get("agent", "universal")
        if same_agent and "*" in wanted_after:
            continue
        if runner.dry_run:
            if "*" in wanted_before:
                print(f"{terminal.badge('RUN')} Would remove retired skills from {before['source']} only after confirmation and ownership verification: installed directories resolved when applied")
                continue
            for name in sorted(names_before - names_after if same_agent else names_before):
                print(f"{terminal.badge('RUN')} Would remove retired {agent} skill directory only after confirmation and ownership verification: {skill_root(before) / name}")
            continue

        installed = installed_skill_records(agent, runner, before["source"], installer_version=before.get("installerVersion", "latest"))
        if installed is None:
            runner.failures.append(f"Unable to list installed skills from {before['source']}")
            print(f"{terminal.badge('FAIL')} Unable to identify installed skills from {before['source']}")
            continue
        retired = {
            Path(record["path"]).name: record for record in installed
            if ("*" in wanted_before or Path(record["path"]).name in names_before)
            and (not same_agent or Path(record["path"]).name not in names_after)
        }
        if not retired:
            continue
        try:
            # Preflight every directory before removing any. The installer's
            # remove command can affect other agents even with an explicit scope.
            folders = [retirement_directory(before, record) for record in retired.values()]
            for folder in folders:
                print(f"{terminal.badge('RUN')} Remove retired {agent} skill directory: {folder}")
                if folder.exists():
                    shutil.rmtree(folder)
        except (OSError, runtime.DotAiError) as exc:
            runner.failures.append(str(exc))
            print(f"{terminal.badge('FAIL')} {exc}")
            continue
        remaining = installed_skill_records(agent, runner, installer_version=before.get("installerVersion", "latest"))
        if remaining is None:
            runner.failures.append(f"Unable to verify retired skills from {before['source']}")
            print(f"{terminal.badge('FAIL')} Unable to verify retired skills from {before['source']}")
            continue
        leftover = set(retired).intersection(Path(record["path"]).name for record in remaining)
        leftover.update(folder.name for folder in folders if folder.exists() or folder.is_symlink())
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




def installed_skill_name(name: str) -> str:
    """Match the skills installer's directory normalization for named selections."""
    sanitized = re.sub(r"[^a-z0-9._]+", "-", name.lower()).strip(".-")
    return sanitized[:255] or "unnamed-skill"
