"""Reconciliation history and persisted baseline recommendation ownership."""

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


def valid_package_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(package, dict) and isinstance(package.get("name"), str) for package in value
    )


def valid_mcp_servers(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(name, str) and isinstance(server, dict) for name, server in value.items()
    )


def valid_extension_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def managed_stack_baseline(manifest_path: Path) -> dict[str, Any] | None:
    key = os.path.normcase(str(manifest_path.resolve()))
    try:
        values = json.loads((runtime.state_dir() / "recommended-baseline.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return None
    stored = values.get(key) if isinstance(values, dict) else None
    if not isinstance(stored, dict) or stored.get("version") != 1:
        return None
    packages = stored.get("packages")
    mcp_servers = stored.get("mcpServers")
    extensions = stored.get("ompExtensions")
    if not (valid_package_list(packages) and valid_mcp_servers(mcp_servers) and valid_extension_list(extensions)):
        return None
    return {
        "packages": list(packages),
        "mcpServers": dict(mcp_servers),
        "ompExtensions": list(extensions),
    }


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



def save_managed_stack_baseline(manifest_path: Path, baseline: dict[str, Any]) -> None:
    directory = runtime.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "recommended-baseline.json"
    try:
        values = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        values = {}
    if not isinstance(values, dict):
        values = {}
    values[os.path.normcase(str(manifest_path.resolve()))] = {
        "version": 1,
        "packages": list(baseline["packages"]),
        "mcpServers": dict(baseline["mcpServers"]),
        "ompExtensions": list(baseline["ompExtensions"]),
    }
    _write_state_file(target, values)

def save_state(
    manifest_path: Path,
    runner: runtime.Runner,
    operation: str,
    managed_skills: list[dict[str, Any]] | None = None,
    managed_baseline: dict[str, Any] | None = None,
) -> None:
    if runner.dry_run or runner.failures:
        return
    local_manifest: dict[str, Any] | None = None
    example_manifest: dict[str, Any] | None = None
    if managed_skills is None:
        stored = managed_recommendations(manifest_path)
        if stored is not None:
            managed_skills = stored
        else:
            local_manifest = manifests.load_manifest(manifest_path)
            example_manifest = manifests.load_manifest(manifests.EXAMPLE_MANIFEST)
            local_skills = local_manifest["skills"]
            managed_skills = [skill for skill in example_manifest["skills"] if skill in local_skills]
    if managed_baseline is None:
        stored_baseline = managed_stack_baseline(manifest_path)
        if stored_baseline is not None:
            managed_baseline = stored_baseline
        else:
            if local_manifest is None:
                local_manifest = manifests.load_manifest(manifest_path)
            if example_manifest is None:
                example_manifest = manifests.load_manifest(manifests.EXAMPLE_MANIFEST)
            local_packages = local_manifest["packages"]
            local_servers = local_manifest["mcp"]["servers"]
            local_extensions = set(local_manifest.get("ompExtensions", []))
            managed_baseline = {
                "packages": [pkg for pkg in example_manifest["packages"] if pkg in local_packages],
                "mcpServers": {
                    name: server
                    for name, server in example_manifest["mcp"]["servers"].items()
                    if local_servers.get(name) == server
                },
                "ompExtensions": [
                    ext for ext in example_manifest.get("ompExtensions", []) if ext in local_extensions
                ],
            }
    save_managed_recommendations(manifest_path, managed_skills)
    save_managed_stack_baseline(manifest_path, managed_baseline)
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
