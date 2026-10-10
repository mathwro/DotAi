"""Reviewed recipes materialized only in memory, never saved as user intent."""
from __future__ import annotations

import copy
import json
import re
from typing import Any
from . import portable, runtime

CATALOG_PATH = runtime.ROOT / "component-recipes.json"
EXACT_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")


def load_catalog() -> dict[str, Any]:
    try:
        value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Cannot read reviewed component recipes: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("recipes"), dict) or not isinstance(value.get("prerequisites"), dict):
        raise runtime.DotAiError("Invalid component recipe catalog")
    for prerequisite in value["prerequisites"].values():
        if not isinstance(prerequisite, dict) or not prerequisite.get("check") or set(prerequisite) & {"install", "update", "configure", "uninstall"}:
            raise runtime.DotAiError("Catalog prerequisites must be checks-only")
    return value


def _substitute(value: Any, version: str) -> Any:
    if isinstance(value, str):
        return value.replace("{version}", version)
    if isinstance(value, list):
        return [_substitute(item, version) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, version) for key, item in value.items()}
    return value


def _effective_package(package: dict[str, Any]) -> dict[str, Any]:
    recipe_id = package.get("recipe")
    recipe = {}
    if recipe_id:
        recipe = load_catalog()["recipes"].get(recipe_id)
        if recipe is None:
            raise runtime.DotAiError(f"Unknown component recipe {recipe_id!r}; supply an explicit custom recipe instead")
    effective = copy.deepcopy(recipe)
    scoop = effective.get("scoopManifest")
    if scoop:
        app = effective["scoopApp"]
        install = [portable.scoop_command(app, scoop, replace=True)]
        effective.setdefault("install", {})["windows"] = install
        if effective.get("scoopUpdate", False):
            effective.setdefault("update", {})["windows"] = copy.deepcopy(install)
        effective.setdefault("uninstall", {})["windows"] = [portable.scoop_command(app, scoop, uninstall=True)]
        if effective.get("pinInstall"):
            effective["pinInstall"]["windows"] = copy.deepcopy(install)
    effective.update(copy.deepcopy(package))
    return effective


def resolve_uninstall(package: dict[str, Any], platform_name: str | None = None) -> dict[str, Any]:
    """Resolve explicit removal independently of the desired installation pin."""
    return _effective_package(package)


def resolve_package(package: dict[str, Any], platform_name: str | None = None) -> dict[str, Any]:
    if not package.get("enabled", True) or package.get("managed") is not True:
        return copy.deepcopy(package)
    effective = _effective_package(package)
    version = effective.get("version", "latest")
    if effective.get("updatePolicy") == "pinned" and version == "latest":
        raise runtime.DotAiError(f"{package['name']}: pinned policy requires an exact version pin")
    if version != "latest":
        if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
            raise runtime.DotAiError(f"{package['name']}: invalid exact version pin")
        pin = effective.get("pinInstall")
        supported = effective.get("supportedVersions")
        if not pin or (supported is not None and version not in supported):
            raise runtime.DotAiError(f"{package['name']}: unsupported version pin {version}; no reviewed exact installer")
        if platform_name and not runtime.selected(pin, platform_name):
            raise runtime.DotAiError(f"{package['name']}: version pin {version} is unsupported on {platform_name}")
        effective["install"] = _substitute(pin, version)
        effective["update"] = copy.deepcopy(effective["install"])
    if platform_name and effective.get("enabled", True) and effective.get("managed") is True:
        if not runtime.selected(effective.get("install", []), platform_name):
            raise runtime.DotAiError(f"{package['name']}: unsupported platform {platform_name}")
    return effective


def materialize(manifest: dict[str, Any], platform_name: str | None = None) -> dict[str, Any]:
    effective = copy.deepcopy(manifest)
    effective["packages"] = [resolve_package(package, platform_name) for package in manifest.get("packages", [])]
    catalog = load_catalog()
    prerequisites = copy.deepcopy(catalog["prerequisites"])
    for definition in manifest.get("prerequisites", []):
        prerequisites[definition["name"]] = copy.deepcopy(definition)
    effective["prerequisites"] = list(prerequisites.values())
    effective["integrationRequires"] = copy.deepcopy(catalog.get("integrationRequires", {}))
    effective["integrationRequires"].update(copy.deepcopy(manifest.get("integrationRequires", {})))
    return effective
