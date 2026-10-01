"""Reconciliation history and persisted recommended-skill ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import datetime as dt
import json
import os
import tempfile
from . import manifest as manifests
from . import runtime


def read_state(manifest_path: Path) -> dict[str, Any]:
    try:
        value = json.loads((runtime.state_dir() / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return {}
    if not isinstance(value, dict) or value.get("manifest") != str(manifest_path.resolve()):
        return {}
    return value


def valid_skill_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(skill, dict) and isinstance(skill.get("source"), str) for skill in value
    )


def managed_recommendations(manifest_path: Path) -> list[dict[str, Any]] | None:
    key = os.path.normcase(str(manifest_path.resolve()))
    try:
        values = json.loads((runtime.state_dir() / "recommended-skills.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        values = {}
    stored = values.get(key) if isinstance(values, dict) else None
    if isinstance(stored, dict) and stored.get("version") == 1:
        skills = stored.get("skills")
        if valid_skill_list(skills):
            return list(skills)
    if valid_skill_list(stored):
        sources = {skill["source"] for skill in manifests.load_manifest(manifests.EXAMPLE_MANIFEST)["skills"]}
        return [skill for skill in stored if skill["source"] in sources]
    legacy = read_state(manifest_path).get("managedRecommendedSkills")
    return list(legacy) if valid_skill_list(legacy) else None


def _write_state_file(target: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2) + "\n"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def save_managed_recommendations(manifest_path: Path, skills: list[dict[str, Any]]) -> None:
    directory = runtime.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "recommended-skills.json"
    try:
        values = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        values = {}
    if not isinstance(values, dict):
        values = {}
    values[os.path.normcase(str(manifest_path.resolve()))] = {"version": 1, "skills": skills}
    _write_state_file(target, values)


def save_state(
    manifest_path: Path,
    runner: runtime.Runner,
    operation: str,
    managed_skills: list[dict[str, Any]] | None = None,
) -> None:
    if runner.dry_run or runner.failures:
        return
    if managed_skills is None:
        stored = managed_recommendations(manifest_path)
        if stored is not None:
            managed_skills = stored
        else:
            local_skills = manifests.load_manifest(manifest_path)["skills"]
            managed_skills = [
                skill for skill in manifests.load_manifest(manifests.EXAMPLE_MANIFEST)["skills"] if skill in local_skills
            ]
    save_managed_recommendations(manifest_path, managed_skills)
    directory = runtime.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    value = {
        "lastOperation": operation,
        "lastSuccess": dt.datetime.now(dt.timezone.utc).isoformat(),
        "manifest": str(manifest_path.resolve()),
        "platform": runner.platform,
    }
    target = directory / "state.json"
    _write_state_file(target, value)
