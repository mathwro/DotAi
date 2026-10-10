"""Reviewed recipes materialized only in memory, never saved as user intent."""
from __future__ import annotations

import copy
import json
import hashlib
import platform
from pathlib import Path
import shutil
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
    if "install" in package or "pinInstall" in package:
        if "versionMetadata" not in package:
            effective.pop("versionMetadata", None)
        if "supportedVersions" not in package:
            effective.pop("supportedVersions", None)
        if "install" in package and "pinInstall" not in package:
            effective.pop("pinInstall", None)
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
        if not effective.get("nativeUpdate") or "update" in package:
            effective["update"] = copy.deepcopy(effective["install"])
    if platform_name and effective.get("enabled", True) and effective.get("managed") is True:
        if not runtime.selected(effective.get("install", []), platform_name):
            raise runtime.DotAiError(f"{package['name']}: unsupported platform {platform_name}")
    return effective


def native_architecture(runner: runtime.Runner) -> str:
    """Match the official binary installer's host architecture, including Rosetta."""
    machine = platform.machine().lower()
    if runner.platform == "windows":
        machine = runner.env.get("PROCESSOR_ARCHITEW6432", runner.env.get("PROCESSOR_ARCHITECTURE", machine)).lower()
    elif runner.platform == "macos":
        if runner.output(["/usr/sbin/sysctl", "-in", "hw.optional.arm64"]) == "1":
            machine = "arm64"
    if machine in {"x86_64", "amd64", "x64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise runtime.DotAiError(f"Unsupported native release architecture {machine}")


def native_artifact_name(descriptor: dict[str, Any], runner: runtime.Runner) -> str:
    template = runtime.selected(descriptor.get("artifacts", {}), runner.platform)
    if not isinstance(template, str):
        raise runtime.DotAiError(f"Native release artifacts are unavailable on {runner.platform}")
    return template.replace("{arch}", native_architecture(runner))


def validate_native_metadata(descriptor: dict[str, Any], facts: Any, version: str, runner: runtime.Runner | None = None) -> dict[str, Any]:
    if not isinstance(facts, dict) or not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
        raise runtime.DotAiError("Invalid native release version metadata")
    artifact = facts.get("artifact")
    templates = descriptor.get("artifacts", {}).values()
    names = {template.replace("{arch}", arch) for template in templates for arch in ("x64", "arm64")}
    expected = descriptor["download"].replace("{version}", version).replace("{artifact}", str(artifact))
    if (facts.get("version") != version or facts.get("release") != "v" + version
            or artifact not in names or facts.get("url") != expected
            or not isinstance(facts.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", facts["sha256"])):
        raise runtime.DotAiError("Native release artifact, version, or digest provenance is invalid")
    if runner is not None and artifact != native_artifact_name(descriptor, runner):
        raise runtime.DotAiError("Locked native artifact does not match this platform/architecture; explicitly update its resolution")
    return copy.deepcopy(facts)


def bind_native_release(package: dict[str, Any], facts: dict[str, Any], runner: runtime.Runner, intent: dict[str, Any]) -> None:
    """Freeze executable install inputs without replacing explicit custom commands."""
    if "install" in intent or "pinInstall" in intent:
        return
    descriptor = package["versionMetadata"]
    validate_native_metadata(descriptor, facts, package["version"], runner)
    if runner.platform == "windows":
        binary = package["scoopManifest"]["bin"]
        manifest = {"version": facts["version"], "url": facts["url"] + "#/" + binary, "hash": facts["sha256"], "bin": binary}
        package["install"] = {"windows": [portable.scoop_command(package["scoopApp"], manifest, replace=True)]}
    else:
        package["install"] = _substitute(package["pinInstall"], package["version"])
        command = runtime.selected(package["install"], runner.platform)[0]
        for index, argument in enumerate(command):
            if argument == "{artifact}":
                command[index] = json.dumps(facts, separators=(",", ":"))
                break
        else:
            raise runtime.DotAiError("Reviewed native installer has no frozen artifact input")


def tool_launcher_target(path: Path, runner: runtime.Runner, *, expected_target: str | Path | None = None) -> Path:
    """Observe the actual forwarded Scoop binary without executing its manifest."""
    if expected_target is not None:
        expected_target = Path(expected_target)
    shim = path.with_suffix(".shim")
    if runner.platform != "windows" or not shim.exists():
        actual = path.resolve(strict=True)
        if expected_target is None or actual == expected_target.resolve(strict=True):
            return actual
        if runner.platform != "windows" or path.is_symlink() or expected_target.is_symlink():
            raise runtime.DotAiError("Tool launcher does not forward to the reviewed backend")
        if path.parent.resolve() != path.parent or expected_target.parent.resolve() != expected_target.parent:
            raise runtime.DotAiError("Tool launcher or backend directory is redirected")
        if not actual.is_file() or not expected_target.is_file() or actual.stat().st_size != expected_target.stat().st_size:
            raise runtime.DotAiError("Copied tool launcher does not match the reviewed backend")
        with actual.open("rb") as launcher, expected_target.open("rb") as backend:
            while chunk := launcher.read(65536):
                if chunk != backend.read(65536):
                    raise runtime.DotAiError("Copied tool launcher differs from the reviewed backend")
        return expected_target.resolve(strict=True)
    if path.is_symlink() or shim.is_symlink() or path.parent.resolve() != path.parent:
        raise runtime.DotAiError("Refusing redirected Scoop shim provenance")
    text = shim.read_text(encoding="utf-8-sig")
    matches = re.findall(r'(?m)^\s*path\s*=\s*"([^"]+)"\s*$', text)
    if len(matches) != 1 or any(line.strip() and not re.fullmatch(r'\s*path\s*=\s*"[^"]+"\s*', line) for line in text.splitlines()):
        raise runtime.DotAiError("Scoop shim has unsupported forwarding provenance")
    target = Path(matches[0])
    if not target.is_absolute():
        raise runtime.DotAiError("Scoop shim target is not absolute")
    actual = target.resolve(strict=True)
    if expected_target is not None and actual != expected_target.resolve(strict=True):
        raise runtime.DotAiError("Scoop shim forwards to a different backend")
    return actual


def tool_recipe_targets(value: dict[str, Any], runner: runtime.Runner, operation: str) -> dict[str, str] | None:
    """Describe generated mutator scope from reviewed declarative footprints."""
    if operation in value or not value.get("recipe"):
        return None
    recipe = load_catalog()["recipes"].get(value["recipe"])
    if recipe is None:
        raise runtime.DotAiError(f"Unknown component recipe {value['recipe']!r}")
    effective = _effective_package(value)
    if operation == "update" and recipe.get("nativeUpdate") and value.get("version", "latest") == "latest":
        steps = runtime.selected(effective.get("update", []), runner.platform)
        check = runtime.selected(effective.get("check", []), runner.platform)
        if steps and isinstance(steps[0], list) and isinstance(check, list) and check:
            command_path = shutil.which(runner.argv(steps[0])[0], path=runner.env.get("PATH"))
            check_path = shutil.which(runner.argv(check)[0], path=runner.env.get("PATH"))
            if command_path and check_path and tool_launcher_target(Path(command_path), runner) == tool_launcher_target(Path(check_path), runner):
                return None
    footprint = runtime.selected(recipe.get("footprint", {}), runner.platform)
    if not isinstance(footprint, dict):
        raise runtime.DotAiError("Reviewed recipe has no verifiable mutator footprint")
    kind = footprint.get("kind")
    if kind == "standalone":
        launcher = str(Path(runner.env["HOME"]) / footprint["launcher"])
        path = Path(launcher)
        if path.is_symlink() or path.parent.resolve() != path.parent:
            raise runtime.DotAiError("Standalone recipe launcher is redirected")
        return {"launcher": launcher, "target": launcher}
    if kind == "uv":
        tool_dir = runner.env.get("UV_TOOL_DIR") or runner.output(["uv", "tool", "dir"])
        bin_dir = runner.env.get("UV_TOOL_BIN_DIR") or runner.output(["uv", "tool", "dir", "--bin"])
        if not tool_dir or not bin_dir or not Path(tool_dir).is_absolute() or not Path(bin_dir).is_absolute():
            raise runtime.DotAiError("Cannot verify uv tool installation directories; inspect uv tool dir and uv tool dir --bin")
        tool_dir = str(Path(tool_dir).resolve())
        bin_dir = str(Path(bin_dir).resolve())
        launcher = footprint["launcher"] + (".exe" if runner.platform == "windows" else "")
        backend = Path(tool_dir) / footprint["package"] / ("Scripts" if runner.platform == "windows" else "bin") / launcher
        root = Path(tool_dir) / footprint["package"]
        if root.is_symlink() or root.parent.resolve() != root.parent:
            raise runtime.DotAiError("UV tool environment is redirected")
        return {"launcher": str(Path(bin_dir) / launcher), "target": str(backend.resolve()), "root": str(root)}
    if kind == "scoop":
        root = Path(runner.env.get("SCOOP", str(Path(runner.env["USERPROFILE"]) / "scoop"))).resolve()
        launcher = root / "shims" / footprint["launcher"]
        backend = root / "apps" / footprint["app"] / "current" / footprint["launcher"]
        app_root = root / "apps" / footprint["app"]
        if app_root.is_symlink() or app_root.parent.resolve() != app_root.parent or not backend.resolve().is_relative_to(app_root):
            raise runtime.DotAiError("Scoop current target escapes the reviewed app directory")
        result = {"launcher": str(launcher), "target": str(backend.resolve()), "root": str(app_root)}
        shim = launcher.with_suffix(".shim")
        if launcher.exists():
            if not shim.is_file() or tool_launcher_target(launcher, runner) != backend.resolve(strict=True):
                raise runtime.DotAiError("Scoop shim does not forward to the reviewed app target")
            result.update({"guard": str(shim), "guardDigest": hashlib.sha256(shim.read_bytes()).hexdigest()})
        return result
    raise runtime.DotAiError("Unsupported reviewed recipe footprint")


def materialize(manifest: dict[str, Any], platform_name: str | None = None, *, inspect_only: bool = False) -> dict[str, Any]:
    effective = copy.deepcopy(manifest)
    effective["packages"] = [
        _effective_package(package) if inspect_only and package.get("enabled", True) and package.get("managed") is True
        else resolve_package(package, platform_name)
        for package in manifest.get("packages", [])
    ]
    catalog = load_catalog()
    prerequisites = copy.deepcopy(catalog["prerequisites"])
    for definition in manifest.get("prerequisites", []):
        prerequisites[definition["name"]] = copy.deepcopy(definition)
    effective["prerequisites"] = list(prerequisites.values())
    effective["integrationRequires"] = copy.deepcopy(catalog.get("integrationRequires", {}))
    effective["integrationRequires"].update(copy.deepcopy(manifest.get("integrationRequires", {})))
    return effective
