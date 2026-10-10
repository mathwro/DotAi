"""Review and apply repository-recommended skill changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
from . import manifest as manifests
from . import runtime
from . import skills as skill_manager
from . import state as app_state
from . import terminal
from . import catalog, lifecycle, locking, prerequisites


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


def recommended_skill_plan(
    manifest: dict[str, Any], manifest_path: Path, enforce: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    desired = manifests.recommended_skills()
    stored = app_state.managed_recommendations(manifest_path)
    if stored is not None:
        managed = stored
    else:
        managed = [skill for skill in desired if skill in manifest["skills"]]

    local = {skill["source"]: skill for skill in manifest["skills"]}
    wanted = {skill["source"]: skill for skill in desired}
    accepted_baseline = list(managed)
    changes: list[dict[str, Any]] = []
    conflicts: list[str] = []

    for before in managed:
        source = before["source"]
        after = wanted.get(source)
        current = local.get(source)
        if after is None:
            if current is None or current == before:
                changes.append({"kind": "remove", "source": source, "before": before, "after": None})
            elif enforce:
                changes.append({"kind": "remove", "source": source, "before": current, "after": None})
            else:
                conflicts.append(source)
        elif after != before:
            if current == before:
                changes.append({"kind": "update", "source": source, "before": before, "after": after})
            elif current == after:
                replace_skill(accepted_baseline, source, after)
            elif enforce:
                changes.append(
                    {
                        "kind": "add" if current is None else "update",
                        "source": source,
                        "before": current,
                        "after": after,
                    }
                )
            else:
                conflicts.append(source)
        elif current != before:
            if enforce:
                changes.append(
                    {
                        "kind": "add" if current is None else "update",
                        "source": source,
                        "before": current,
                        "after": after,
                    }
                )
            else:
                conflicts.append(source)

    managed_sources = {skill["source"] for skill in managed}
    for after in desired:
        source = after["source"]
        if source in managed_sources:
            continue
        current = local.get(source)
        if current is None:
            changes.append({"kind": "add", "source": source, "before": None, "after": after})
        elif current == after:
            replace_skill(accepted_baseline, source, after)
        elif enforce:
            changes.append({"kind": "update", "source": source, "before": current, "after": after})
        else:
            conflicts.append(source)

    return accepted_baseline, changes, conflicts


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
        print(terminal.heading("Available recommended skill changes (not applied):"))
        for change in changes:
            skill = change["after"] if change["after"] is not None else change["before"]
            selections = ", ".join(skill.get("skills", ["*"]))
            print(terminal.redact(f"  {change['kind'].capitalize()} {change['source']} ({selections})"))
    for source in conflicts:
        print(terminal.redact(f"{terminal.badge('DRIFT')} Recommended source {source}: local entry differs; preserving it"))
    print("Review with sync --recommended-skills --dry-run; apply with sync --recommended-skills.")
    if conflicts:
        print("Add --enforce to explicitly adopt recommendations for locally differing sources.")


def _apply_skill_changes(
    manifest: dict[str, Any], manifest_path: Path, runner: runtime.Runner,
    managed: list[dict[str, Any]], selected: list[dict[str, Any]], *,
    update_skills: bool = False, recommended_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    updated, accepted = apply_recommended_skill_changes(manifest, managed, selected)
    candidate = updated
    if recommended_only:
        recommended_keys = {
            (skill["source"], skill.get("agent", "universal"))
            for skill in manifests.recommended_skills()
        }
        candidate = {
            **updated,
            "skills": [
                skill for skill in updated["skills"]
                if (skill["source"], skill.get("agent", "universal")) in recommended_keys
            ],
        }
    refresh_sources = {
        change["after"]["source"] for change in selected if change["after"] is not None
    }
    try:
        manifests.validate_manifest(updated)
        if not prerequisites.preflight(catalog.materialize(candidate, runner.platform), runner, mode="sync"):
            return manifest, managed
        effective = locking.prepare(
            candidate, manifest_path, runner, "sync", update_skills=update_skills,
            refresh_sources=refresh_sources, skill_receipt_owned=lifecycle.skill_receipt_owned,
            provenance_manifest=updated if recommended_only else None,
        )
    except (OSError, runtime.DotAiError) as exc:
        runner.fail("Recommended skill plan", str(exc))
        return manifest, managed
    if not prerequisites.preflight(effective, runner, mode="sync", show_available=False):
        return manifest, managed
    try:
        with locking.execution(manifest_path, runner):
            backup = None
            if not runner.dry_run:
                backup = manifests.write_manifest(manifest_path, updated, backup=True)
                print(terminal.redact(f"{terminal.badge('OK')} Manifest backup written to {backup}"))
            skill_manager.remove_retired_skills(selected, runner)
            if runner.failures:
                if backup is not None:
                    shutil.copy2(backup, manifest_path)
                    print(terminal.redact(f"{terminal.badge('OK')} Restored {manifest_path} after skill removal failure"))
                return manifest, managed
            if not runner.dry_run:
                app_state.save_managed_recommendations(manifest_path, accepted)
    except runtime.DotAiError as exc:
        runner.fail("Recommended skill plan", str(exc))
        return manifest, managed
    return updated, accepted


def review_recommended_skills(
    manifest: dict[str, Any], manifest_path: Path, runner: runtime.Runner, enforce: bool = False, *,
    update_skills: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    managed, changes, conflicts = recommended_skill_plan(manifest, manifest_path, enforce)
    conditional_cleanup = False
    if enforce:
        recommended_sources = {skill["source"] for skill in manifests.recommended_skills()}
        managed_sources = {skill["source"] for skill in managed}
        user_skills = [
            skill for skill in manifest["skills"]
            if skill["source"] not in recommended_sources and skill["source"] not in managed_sources
        ]
        if user_skills:
            print(terminal.heading("User-owned skill sources outside the recommendations:"))
            for skill in user_skills:
                for name in skill.get("skills", ["*"]):
                    suffix = f"/{name}" if name != "*" else ""
                    print(terminal.redact(f"  {skill['source']}{suffix}"))
            removals = [
                {"kind": "remove", "source": skill["source"], "before": skill, "after": None}
                for skill in user_skills
            ]
            if runner.dry_run:
                print(f"{terminal.badge('RUN')} Optional cleanup preview: applied only if removal is confirmed; no files changed.")
                remove_users = True
                conditional_cleanup = True
            else:
                try:
                    answer = input("Remove these sources from the manifest and their installed skills? [y/N] ")
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                remove_users = answer.strip().lower() in {"y", "yes"}
            if remove_users:
                manifest, managed = _apply_skill_changes(
                    manifest, manifest_path, runner, managed, removals,
                    update_skills=update_skills, recommended_only=enforce,
                )
                if runner.failures:
                    return manifest, managed
                managed, changes, conflicts = recommended_skill_plan(manifest, manifest_path, enforce)
            else:
                print(f"{terminal.badge('INACTIVE')} Preserving user-owned skills; skipping their installation during enforced sync.")
    for source in conflicts:
        print(terminal.redact(f"{terminal.badge('DRIFT')} Recommended source {source}: local entry was modified; preserving it"))
    if not changes:
        print(f"{terminal.badge('OK')} Recommended skills: no changes available")
        return manifest, managed

    proposed, _ = apply_recommended_skill_changes(manifest, managed, changes)
    if conditional_cleanup:
        print(terminal.heading("Proposed recommended skill changes, conditional on approving the cleanup above:"))
    else:
        print(terminal.heading("Proposed recommended skill changes (apply only after confirmation):"))
    print(terminal.describe_changes(manifest, proposed, manifest_path))
    if runner.dry_run:
        selected = changes
        print(f"{terminal.badge('RUN')} Dry run: no manifest or skill changes applied.")
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
        print(f"{terminal.badge('OK')} No recommended skill changes applied.")
        return manifest, managed

    return _apply_skill_changes(
        manifest, manifest_path, runner, managed, selected,
        update_skills=update_skills, recommended_only=enforce,
    )
