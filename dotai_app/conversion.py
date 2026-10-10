"""Explicit, reviewed version-1 conversion with no environment operations."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from . import catalog
from . import manifest as manifests
from . import runtime, terminal

RETIRED_FIELDS = manifests.NON_CHECK_PREREQUISITE_FIELDS


def read_legacy(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise runtime.DotAiError(f"Cannot read legacy manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise runtime.DotAiError("Conversion requires an explicit version-1 source manifest; normal version-2 files need no conversion.")
    return data


def plan(data: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    candidate = copy.deepcopy(data)
    candidate["version"] = 2
    packages = candidate.get("packages")
    if not isinstance(packages, list):
        raise runtime.DotAiError("Legacy manifest packages must be an array")
    prerequisites = candidate.setdefault("prerequisites", [])
    if not isinstance(prerequisites, list):
        raise runtime.DotAiError("Legacy manifest prerequisites must be an array")
    retained = []
    review = []
    retired = []
    recipes = catalog.load_catalog()["recipes"]
    for package in packages:
        if not isinstance(package, dict):
            raise runtime.DotAiError("Legacy package entries must be objects")
        manifests.require_nonempty_string(package.get("name"), "packages.name")
        prerequisite = manifests.managed_prerequisite(package)
        if prerequisite:
            definition = {key: value for key, value in package.items() if key not in RETIRED_FIELDS}
            definition["name"] = prerequisite
            prerequisites.append(definition)
            retired.append(package["name"])
        else:
            package.pop("updateGroup", None)
            package["managed"] = True
            retained.append(package)
            review.append(package["name"])
            if "recipe" not in package:
                matches = [name for name, recipe in recipes.items() if recipe.get("check") == package.get("check")]
                if len(matches) == 1:
                    package["recipe"] = matches[0]
    candidate["packages"] = retained
    manifests.validate_manifest(candidate, allow_legacy_routing=True)
    if len(review) != len(set(review)):
        raise runtime.DotAiError("Legacy package names must be unique before ownership can be reviewed")
    return candidate, review, retired


def confirm(question: str) -> bool:
    try:
        return input(terminal.redact(question) + " [y/N] ").strip().casefold() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def convert_manifest(path: Path, *, destination: Path | None = None, dry_run: bool = False,
                     manage: list[str] | None = None, yes: bool = False) -> int:
    source = read_legacy(path)
    secrets = terminal.credential_values(source)
    candidate, review, retired = plan(source)
    target = destination if destination is not None else path
    same_target = target.resolve() == path.resolve()
    if not same_target and target.exists():
        raise runtime.DotAiError(f"Conversion destination already exists: {target}; choose a new path.")
    approved = set(manage or [])
    unknown = approved - set(review)
    if unknown:
        raise runtime.DotAiError(terminal.redact("Ownership review names are not retained packages: " + ", ".join(sorted(unknown)), secrets))
    print(terminal.heading("Version-2 conversion preview:"))
    print(f"  Source: {terminal.redact(path, secrets)}")
    print(f"  Destination: {terminal.redact(target, secrets)}")
    print("  Change the manifest format from version 1 to version 2.")
    for name in retired:
        print(f"  Keep {terminal.redact(name, secrets)} as an external check; retire its installation, update, and configuration commands.")
    for name in review:
        print(f"  Retain {terminal.redact(name, secrets)} and its commands; explicit management permission requires ownership review.")
    print("  Preserve skills, integration entries, routing, and user metadata. No environment operations.")
    print("  A private, exact source backup will precede the confirmed write.")
    if dry_run:
        print(f"{terminal.badge('RUN')} Preview only; no files or environment state changed.")
        return 0
    unreviewed = [name for name in review if name not in approved]
    if yes and unreviewed:
        raise runtime.DotAiError(terminal.redact("Review package ownership explicitly with --manage NAME before --yes: " + ", ".join(unreviewed), secrets))
    for name in unreviewed:
        if not confirm(terminal.redact(f"Authorize DotAi to manage the existing commands for {name}? Review custom ownership before accepting.", secrets)):
            print(f"{terminal.badge('INACTIVE')} Conversion declined; the original manifest is unchanged.")
            return 0
    if not yes and not confirm(terminal.redact(f"Convert {path} to version 2 at {target} after backing up the source?", secrets)):
        print(f"{terminal.badge('INACTIVE')} Conversion declined; no files changed.")
        return 0
    # Recheck immediately before mutation; previews and refusals create nothing.
    if not same_target and target.exists():
        raise runtime.DotAiError(f"Conversion destination already exists: {target}")
    if read_legacy(path) != source:
        raise runtime.DotAiError("The source manifest changed during review; preview the new file before converting.")
    backup = manifests.backup_manifest(path)
    manifests.write_manifest(target, candidate, allow_legacy_routing=True, exclusive=not same_target)
    print(f"{terminal.badge('OK')} Converted manifest at {terminal.redact(target, secrets)}; original backup: {terminal.redact(backup, secrets)}")
    return 0
