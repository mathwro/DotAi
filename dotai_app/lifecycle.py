"""Targeted component intent and ownership-proven runtime lifecycle operations."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import re
import tempfile
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from . import manifest as manifests
from . import catalog, locking, mcp, omp, packages, prerequisites, runtime, skills, terminal

COLLECTIONS = {"tool": ("packages", "name"), "skill": ("skills", "source"),
               "plugin": ("plugins", "id"), "marketplace": ("marketplaces", "name")}
KINDS = (*COLLECTIONS, "mcp", "extension")


def components(manifest: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    result = [(kind, value[field], value) for kind, (section, field) in COLLECTIONS.items()
              for value in manifest.get(section, [])]
    result.extend(("mcp", name, value) for name, value in manifest.get("mcp", {}).get("servers", {}).items())
    result.extend(("extension", value if isinstance(value, str) else value["path"],
                   {"path": value} if isinstance(value, str) else value)
                  for value in manifest.get("ompExtensions", []))
    return result


def resolve_selector(manifest: dict[str, Any], selector: str) -> tuple[str, str, dict[str, Any]]:
    kind, separator, identity = selector.partition(":")
    if not separator or kind not in KINDS or not identity:
        raise runtime.DotAiError(f"Invalid selector {safe_label(selector)}; use TYPE:ID (" + ", ".join(KINDS) + ")")
    candidates = []
    for entry_kind, entry_id, value in components(manifest):
        if entry_kind != kind:
            continue
        qualified = entry_id
        if kind == "skill":
            qualified += "@" + value.get("agent", "universal")
        elif kind == "plugin":
            qualified += "#" + value.get("scope", "user")
        if identity in (entry_id, qualified):
            candidates.append((kind, entry_id, value))
    if len(candidates) != 1:
        detail = "ambiguous; add @AGENT for skills or #SCOPE for plugins" if candidates else "not declared"
        raise runtime.DotAiError(f"Component {safe_label(selector)} is {detail}; inspect 'dotai list'")
    return candidates[0]


def filter_manifest(manifest: dict[str, Any], selectors: list[str] | None) -> dict[str, Any]:
    if not selectors:
        return deepcopy(manifest)
    selected = [resolve_selector(manifest, selector) for selector in selectors]
    result = deepcopy(manifest)
    for section, _ in COLLECTIONS.values():
        result[section] = []
    result["ompExtensions"] = []
    result["ompRouting"] = None
    result.setdefault("mcp", {})["servers"] = {}
    seen = set()
    for kind, identity, value in selected:
        key = component_key(kind, identity, value, manifest)
        if key in seen:
            continue
        seen.add(key)
        if kind in COLLECTIONS:
            result[COLLECTIONS[kind][0]].append(deepcopy(value))
        elif kind == "mcp":
            result["mcp"]["servers"][identity] = deepcopy(value)
        else:
            original = next(entry for entry in manifest["ompExtensions"]
                            if (entry if isinstance(entry, str) else entry["path"]) == identity)
            result["ompExtensions"].append(deepcopy(original))
    # Retain requirement data for shared-registration safety in targeted views.
    result["_lifecycleSharedMcp"] = deepcopy(
        manifest.get("_lifecycleSharedMcp", manifest.get("mcp", {}).get("servers", {}))
    )
    return result


def safe_label(value: Any) -> str:
    text = str(value)
    if text.startswith(("https://", "http://")):
        try:
            parts = urlsplit(text)
            host = parts.hostname or "[invalid host]"
            if parts.port:
                host += f":{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            return "[invalid source URL]"
    return text.replace("\n", " ").replace("\r", " ")


def component_key(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any]) -> str:
    scope = value.get("agent", "universal") if kind == "skill" else value.get("scope", "user")
    target = str(skills.skill_root(value)) if kind == "skill" else ""
    if kind == "mcp":
        target = str(canonical_home_path(mcp.desired_mcp(manifest)[0].absolute()))
    elif kind == "plugin":
        target = str(omp.plugin_registry(value).absolute())
    elif kind == "extension":
        target = omp.extension_identity(identity)
    elif kind == "tool":
        target = str(runtime.home_dir())
    source = value["recipe"] if kind == "tool" and value.get("recipe") else value.get("source", "")
    return hashlib.sha256(json.dumps([kind, identity, scope, source, target]).encode()).hexdigest()


def receipt_path() -> Path:
    return runtime.state_dir() / "component-receipts.json"


def read_receipts() -> dict[str, Any]:
    try:
        data = manifests.load_json_object(receipt_path())
    except (OSError, UnicodeError) as exc:
        raise runtime.DotAiError("Cannot read component ownership receipts; repair the machine-local receipt file before mutation") from exc
    if data and (data.get("version") != 1 or not isinstance(data.get("components"), dict)
                 or any(not isinstance(receipt, dict) or not isinstance(receipt.get("snapshot"), dict)
                        for receipt in data["components"].values())):
        raise runtime.DotAiError("Invalid component ownership receipts; repair the machine-local receipt file before mutation")
    return data.get("components", {})


def write_receipts(receipts: dict[str, Any], runner: runtime.Runner | None = None) -> None:
    if runner is not None:
        if runner.dry_run:
            runner.record_outcome("Component ownership receipts", "planned")
            return
        if receipts == read_receipts():
            runner.record_outcome("Component ownership receipts", "unchanged")
            return
    target = receipt_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "components": receipts}, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
        if runner is not None:
            runner.record_outcome("Component ownership receipts", "changed")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def canonical_home_path(path: Path) -> Path:
    """Normalize only the trusted HOME prefix, not managed descendants."""
    configured = Path(os.environ.get("DOTAI_HOME", Path.home())).expanduser().absolute()
    try:
        relative = path.relative_to(configured)
    except ValueError:
        return path
    return runtime.home_dir() / relative


def fingerprint(path: Path) -> str:
    path = canonical_home_path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise runtime.DotAiError(f"Cannot prove ownership of linked or redirected content at {path}")
    if path.is_dir():
        return skills.installed_skill_tree_hash(path)
    if not path.is_file():
        raise runtime.DotAiError(f"Owned content is missing or unsupported at {path}")
    digest = hashlib.sha256()
    digest.update(str(path.stat().st_mode & 0o777).encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_backend_fingerprint(root: Path) -> str:
    """Hash a manager-owned tree without following its dependency symlinks."""
    root = canonical_home_path(root)
    if not root.is_absolute() or root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise runtime.DotAiError("Tool backend root is missing or redirected; refusing ownership")
    digest = hashlib.sha256()
    digest.update(str(root.stat().st_mode).encode())

    def unreadable(error: OSError) -> None:
        raise error

    for directory, folders, files in os.walk(root, followlinks=False, onerror=unreadable):
        folders.sort()
        files.sort()
        for name in (*folders, *files):
            path = Path(directory) / name
            relative = path.relative_to(root)
            if path.suffix == ".pyc" and "__pycache__" in relative.parts[:-1] and not path.is_symlink():
                continue
            if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
                continue
            mode = path.lstat().st_mode
            if path.is_symlink() or (path.is_dir() and path.resolve() != path):
                entry = ["link", relative.as_posix(), mode, os.readlink(path)]
                if name in folders:
                    folders.remove(name)
            elif path.is_dir():
                entry = ["directory", relative.as_posix(), mode]
            elif path.is_file():
                entry = ["file", relative.as_posix(), mode, fingerprint(path)]
            else:
                raise runtime.DotAiError("Unsupported content in tool backend root; refusing ownership")
            digest.update(json.dumps(entry, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def skill_receipt(skill: dict[str, Any]) -> dict[str, Any] | None:
    return read_receipts().get(component_key("skill", skill["source"], skill, {}))


def skill_receipt_owned(skill: dict[str, Any]) -> bool:
    receipt = skill_receipt(skill)
    checks = skill.get("checkSkills", [])
    if not receipt or not checks:
        return False
    paths = receipt.get("snapshot", {}).get("paths", {})
    try:
        return all(Path(name).name == name and name not in {".", ".."}
                   and paths.get(str(skills.skill_root(skill) / name)) == fingerprint(skills.skill_root(skill) / name)
                   for name in checks)
    except (OSError, ValueError, runtime.DotAiError):
        return False


def tool_value(value: dict[str, Any], platform: str | None = None) -> dict[str, Any]:
    """Resolve operation metadata without imposing the desired install pin."""
    return value if "check" in value else catalog.resolve_uninstall(value, platform)


def checked_tool_path(value: dict[str, Any], runner: runtime.Runner) -> Path | None:
    path = packages.checked_package_path(tool_value(value, runner.platform), runner)
    return canonical_home_path(path.parent.resolve() / path.name) if path is not None else None



def observation(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner) -> dict[str, Any]:
    if kind == "skill":
        root = skills.skill_root(value)
        if root.resolve() != root:
            raise runtime.DotAiError("Skill agent root is linked or redirected; refusing lifecycle mutation")
        checks = value.get("checkSkills", [])
        if not checks or "*" in checks:
            raise runtime.DotAiError("Skill ownership requires explicit named check directories")
        if any(Path(name).name != name or name in {".", ".."} for name in checks):
            raise runtime.DotAiError("Skill check directories must be immediate agent-root children")
        paths = {str(root / name): fingerprint(root / name) for name in checks
                 if (root / name).exists() or (root / name).is_symlink()}
        return {"present": bool(paths), "active": bool(paths), "paths": paths}
    if kind == "mcp":
        target, _ = mcp.desired_mcp(manifest)
        matching = [(alias, found, path, section, enabled) for alias, found, path, section, enabled in mcp.discover_mcp_servers(target)
                    if mcp.server_identity_matches(found, value) and ("cwd" not in value or found.get("cwd") == value["cwd"])]
        if not matching:
            return {"present": False, "active": False}
        if len(matching) != 1:
            raise runtime.DotAiError("MCP identity has multiple provider registrations; resolve ambiguity manually")
        alias, found, path, section, enabled = matching[0]
        return {"present": True, "active": enabled, "path": str(canonical_home_path(path.absolute())), "section": section, "alias": alias,
                "digest": hashlib.sha256(json.dumps(found, sort_keys=True).encode()).hexdigest()}
    if kind == "extension":
        current = omp.configured_omp_extensions(runner)
        if current is None:
            raise runtime.DotAiError("Unable to read OMP extension registrations")
        active = any(omp.extension_identity(entry) == omp.extension_identity(identity) for entry in current)
        source = runtime.expand_path(identity).absolute()
        return {"present": active, "active": active, "paths": {str(source): fingerprint(source)} if source.exists() else {}}
    if kind == "plugin":
        registry = canonical_home_path(omp.plugin_registry(value).absolute())
        if registry.resolve() != registry:
            raise runtime.DotAiError("Plugin registry is linked or redirected; refusing lifecycle mutation")
        data = manifests.load_json_object(registry)
        entries = data.get("plugins", {}).get(identity, [])
        if not isinstance(entries, list):
            raise runtime.DotAiError("Invalid plugin registry entries; repair the OMP registry before mutation")
        selected = [entry for entry in entries if isinstance(entry, dict) and entry.get("scope") == value.get("scope", "user")]
        if not selected:
            return {"present": False, "active": False}
        if len(selected) != 1 or not isinstance(selected[0].get("installPath"), str):
            raise runtime.DotAiError("Plugin scope has ambiguous or incomplete installation metadata")
        entry = selected[0]
        folder = canonical_home_path(Path(entry["installPath"]))
        cache = runtime.home_dir() / ".omp/plugins/cache/plugins"
        if folder.parent != cache:
            raise runtime.DotAiError("Plugin installation lies outside OMP's independent managed cache")
        package = manifests.load_json_object(folder / "package.json")
        package_name = package.get("name", identity.split("@", 1)[0])
        if not isinstance(package_name, str) or not re.fullmatch(r"(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+", package_name) or ".." in package_name:
            raise runtime.DotAiError("Plugin runtime package name is unsafe or unsupported")
        runtime_link = registry.parent / "node_modules" / package_name
        link_target = None
        if runtime_link.exists() or runtime_link.is_symlink():
            if canonical_home_path(runtime_link.parent).resolve() != canonical_home_path(runtime_link.parent):
                raise runtime.DotAiError("Plugin runtime package root is redirected; refusing mutation")
            if not runtime_link.is_symlink() or runtime_link.resolve() != folder:
                raise runtime.DotAiError("Plugin runtime package points to unrelated content; refusing upstream mutation")
            link_target = str(folder)
        runtime_lock = registry.parent / "omp-plugins.lock.json"
        if runtime_lock.resolve() != runtime_lock:
            raise runtime.DotAiError("Plugin runtime configuration is redirected; refusing mutation")
        runtime_config = manifests.load_json_object(runtime_lock)
        configured = runtime_config.get("plugins", {})
        settings = runtime_config.get("settings", {})
        if not isinstance(configured, dict) or not isinstance(settings, dict):
            raise runtime.DotAiError("Invalid plugin runtime configuration; refusing mutation")
        if link_target is None and package_name in configured:
            raise runtime.DotAiError("Runtime plugin configuration has no matching owned package link")
        runtime_digest = hashlib.sha256(json.dumps([configured.get(package_name), settings.get(package_name)], sort_keys=True).encode()).hexdigest()
        return {"present": True, "active": entry.get("enabled", True), "version": entry.get("version", "unknown"),
                "paths": {str(folder): fingerprint(folder)},
                "registryDigest": hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
                "runtimeLink": link_target, "runtimeDigest": runtime_digest}
    if kind == "marketplace":
        data = manifests.load_json_object(runtime.home_dir() / ".omp/marketplaces.json")
        entries = data.get("marketplaces", [])
        if not isinstance(entries, list):
            raise runtime.DotAiError("Invalid marketplace registry; repair the OMP registry before mutation")
        matching = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == identity]
        if not matching:
            return {"present": False, "active": False}
        if len(matching) != 1 or matching[0].get("sourceUri") != value["source"]:
            raise runtime.DotAiError("Marketplace name does not prove the declared source; resolve the registry conflict")
        entry = matching[0]
        cache = runtime.home_dir() / ".omp/plugins/cache/marketplaces" / identity
        paths = {str(cache): fingerprint(cache)} if cache.exists() else {}
        catalog_path = Path(entry.get("catalogPath", ""))
        if not catalog_path.is_file():
            raise runtime.DotAiError("Marketplace catalog source is unavailable")
        if not paths:
            paths[str(catalog_path)] = fingerprint(catalog_path)
        return {"present": True, "active": True, "paths": paths,
                "registryDigest": hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()}
    effective = tool_value(value, runner.platform)
    check = runtime.selected(effective.get("check", []), runner.platform)
    if not check or not runner.succeeds(check):
        return {"present": False, "active": False}
    path = checked_tool_path(value, runner)
    if path is None:
        raise runtime.DotAiError("Tool ownership is UNVERIFIED: the check does not identify an independent tool executable; use a direct matching binary and optional ownershipPath")
    if effective.get("ownershipPath") and canonical_home_path(runtime.expand_path(effective["ownershipPath"]).absolute()) != path:
        raise runtime.DotAiError("Tool ownershipPath does not match its checked executable")
    if path.parent.resolve() != path.parent:
        raise runtime.DotAiError("Tool launcher directory is redirected; refusing ownership")
    expected_target = None
    backend_root = None
    if value.get("recipe") and "install" not in value and "pinInstall" not in value:
        for operation in ("install", "uninstall", "update"):
            expected = catalog.tool_recipe_targets(value, runner, operation)
            if expected is not None and expected.get("launcher") == str(path):
                expected_target = expected.get("target")
                backend_root = expected.get("root")
                break
    target = catalog.tool_launcher_target(path, runner, expected_target=expected_target)
    snapshot = {"present": True, "active": True, "paths": {str(target): fingerprint(target)}}
    guard = path.with_suffix(".shim")
    if runner.platform == "windows" and guard.is_file():
        snapshot["paths"][str(guard)] = fingerprint(guard)
    if path != target and not path.is_symlink():
        snapshot["paths"][str(path)] = fingerprint(path)
    if path.is_symlink():
        snapshot["launcher"] = {"path": str(path), "target": os.readlink(path)}
    if backend_root is not None:
        root = canonical_home_path(Path(backend_root))
        snapshot["backendRoot"] = {"path": str(root), "digest": tool_backend_fingerprint(root)}
    return snapshot


def record_install(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner) -> None:
    """Called only after a successful permitted installation/configuration or adoption."""
    if runner.dry_run:
        return
    if kind == "tool" and checked_tool_path(value, runner) is None:
        print(f"{terminal.badge('UNVERIFIED')} Tool {safe_label(identity)}: installation succeeded, but the custom check cannot prove an independent binary; no ownership receipt was granted.")
        runner.record_outcome(f"Tool {identity} ownership receipt", "skipped", "Custom check does not identify an independent tool executable")
        return
    snapshot = observation(kind, identity, value, manifest, runner)
    if not snapshot.get("present") and not (kind == "extension" and snapshot.get("paths")):
        raise runtime.DotAiError(f"Cannot record ownership: {kind} {safe_label(identity)} is not observed installed")
    receipts = read_receipts()
    key = component_key(kind, identity, value, manifest)
    previous = receipts.get(key, {})
    if kind == "skill":
        snapshot["paths"] = {**previous.get("snapshot", {}).get("paths", {}), **snapshot["paths"]}
    receipts[key] = {"kind": kind, "id": identity, "scope": value.get("agent", value.get("scope", "user")),
                     "source": value.get("source"), "snapshot": snapshot,
                     "revision": value.get("revision"), "installerVersion": value.get("installerVersion")}
    write_receipts(receipts, runner)


def prove_owned(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    receipt = read_receipts().get(component_key(kind, identity, value, manifest))
    if kind == "skill" and (skill_receipt_owned(value) or skills.skill_source_owned(value, skills.skill_owners())):
        return receipt or {"kind": kind, "id": identity, "snapshot": snapshot}
    if receipt and snapshot == receipt.get("snapshot"):
        return receipt
    raise runtime.DotAiError(f"Cannot modify {kind} {safe_label(identity)}: ownership is unproven or content has changed; adopt matching content explicitly or resolve it manually")


def check_tool_recipe_target(value: dict[str, Any], runner: runtime.Runner, operation: str) -> None:
    """Bind generated mutations to the executable footprint they actually manage."""
    expected = catalog.tool_recipe_targets(value, runner, operation)
    if expected is None:
        return
    launcher = checked_tool_path(value, runner)
    if launcher is None or str(launcher) != expected["launcher"]:
        raise runtime.DotAiError("Reviewed tool recipe does not manage the checked launcher; supply an explicit custom operation or use the recipe's matching executable")
    target = catalog.tool_launcher_target(launcher, runner, expected_target=expected.get("target"))
    if "target" in expected and str(target) != expected["target"]:
        raise runtime.DotAiError("Reviewed tool recipe does not manage the checked backend; refusing mutation")
    if expected.get("root"):
        observed = observation("tool", value["name"], value, {"packages": [value]}, runner)
        if observed.get("backendRoot", {}).get("path") != expected["root"]:
            raise runtime.DotAiError("Generated manager operation requires complete backend ownership; custom installers need an explicit reviewed operation or adoption of the unmodified catalog recipe")
    if expected.get("guard"):
        guard = Path(expected["guard"])
        if hashlib.sha256(guard.read_bytes()).hexdigest() != expected.get("guardDigest"):
            raise runtime.DotAiError("Tool forwarding metadata changed; refusing mutation")


def check_update_ownership(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner, *, operation: str = "update") -> None:
    """Preflight an existing component before an explicit domain update."""
    if kind == "tool":
        if value.get("managed") is not True:
            raise runtime.DotAiError("Tool mutation requires managed:true and proven ownership")
        expected = catalog.tool_recipe_targets(value, runner, operation)
        if operation == "install" and expected is not None:
            definition = catalog.load_catalog()["recipes"][value["recipe"]]
            direct_check = runtime.selected(value.get("check", definition.get("check", [])), runner.platform)
            reviewed_check = runtime.selected(definition.get("check", []), runner.platform)
            targets = list(dict.fromkeys(expected[field] for field in ("launcher", "target", "root", "guard") if field in expected))
            if direct_check == reviewed_check and not any(Path(path).exists() or Path(path).is_symlink() for path in targets):
                print("  New managed installation scope: " + ", ".join(safe_label(path) for path in targets))
                return
        observed = observation(kind, identity, value, manifest, runner)
        if not observed.get("present"):
            candidate = checked_tool_path(value, runner)
            expected = catalog.tool_recipe_targets(value, runner, operation)
            receipt = read_receipts().get(component_key(kind, identity, value, manifest))
            paths = list(receipt.get("snapshot", {}).get("paths", {})) if receipt else []
            if receipt and receipt.get("snapshot", {}).get("launcher", {}).get("path"):
                paths.append(receipt["snapshot"]["launcher"]["path"])
            if receipt and receipt.get("snapshot", {}).get("backendRoot", {}).get("path"):
                paths.append(receipt["snapshot"]["backendRoot"]["path"])
            if candidate is not None:
                paths.append(str(candidate))
            if expected is not None:
                paths.extend(expected[key] for key in ("launcher", "target", "guard", "root") if key in expected)
            if any(Path(path).exists() or Path(path).is_symlink() for path in paths):
                raise runtime.DotAiError("Tool check failed but existing executable or backend remains; ownership cannot authorize replacement")
            return
        check_tool_recipe_target(value, runner, operation)
    if kind == "mcp":
        receipt = read_receipts().get(component_key(kind, identity, value, manifest))
        original = receipt.get("snapshot", {}) if receipt else {}
        target = mcp.safe_mcp_target(mcp.desired_mcp(manifest)[0])
        matching = [
            (found, path, section, enabled)
            for alias, found, path, section, enabled in mcp.discover_mcp_servers(target)
            if alias == original.get("alias") and section == "mcpServers"
            and str(canonical_home_path(path.absolute())) == str(target)
        ]
        if len(matching) != 1:
            raise runtime.DotAiError("MCP alias ownership is unavailable; adopt matching configuration explicitly")
        found, path, section, enabled = matching[0]
        snapshot = {
            "present": True, "active": enabled, "path": str(target), "section": section,
            "alias": original["alias"],
            "digest": hashlib.sha256(json.dumps(found, sort_keys=True).encode()).hexdigest(),
        }
    else:
        snapshot = observed if kind == "tool" else observation(kind, identity, value, manifest, runner)
    prove_owned(kind, identity, value, manifest, snapshot)
    # Refreshing a catalog does not remove installed plugin dependents.
    if kind != "marketplace":
        ensure_unshared(kind, identity, value, manifest, snapshot, runner)
    if kind == "tool":
        print("  Verified managed mutation scope: " + ", ".join(safe_label(path) for path in _tool_scope_paths(snapshot)))


def ensure_unshared(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], snapshot: dict[str, Any], runner: runtime.Runner) -> None:
    if kind == "mcp":
        target, _ = mcp.desired_mcp(manifest)
        target = mcp.safe_mcp_target(target)
        if snapshot.get("path") != str(target) or snapshot.get("section") != "mcpServers":
            raise runtime.DotAiError("MCP lifecycle writes are restricted to the selected target mcpServers container; external providers remain unmanaged")
        config = mcp.load_mcp_config(target)
        found = config["mcpServers"][snapshot["alias"]]
        requirements = manifest.get("_lifecycleSharedMcp", manifest["mcp"]["servers"])
        if any(name != identity and required.get("enabled", True)
               and mcp.server_satisfies(found, {field: data for field, data in required.items() if field != "enabled"})
               for name, required in requirements.items()):
            raise runtime.DotAiError("MCP registration is shared by another declaration; remove that requirement explicitly before changing its activation")
    elif kind == "skill":
        records = skills.installed_skill_records(value.get("agent", "universal"), runner, value["source"],
                                                 installer_version=value.get("installerVersion") or (skill_receipt(value) or {}).get("installerVersion") or "latest")
        if records is None:
            raise runtime.DotAiError("Cannot verify skill source and independent agent scope with the selected installer")
        for path in snapshot.get("paths", {}):
            record = next((entry for entry in records if entry["path"] == path), None)
            if record is None or skills.retirement_directory(value, record) != Path(path):
                raise runtime.DotAiError("Skill source or agent scope does not match its declaration")
    elif kind in {"plugin", "marketplace"}:
        for scope in ("user", "project"):
            registry = manifests.load_json_object(omp.plugin_registry({"scope": scope}))
            entries = registry.get("plugins", {})
            if not isinstance(entries, dict):
                raise runtime.DotAiError("Cannot prove independent ownership with an invalid plugin registry")
            for plugin_id, installs in entries.items():
                if kind == "marketplace" and plugin_id.endswith("@" + identity) and installs:
                    raise runtime.DotAiError("Marketplace still has installed plugin dependents; remove them explicitly first")
                if kind == "plugin":
                    for entry in installs if isinstance(installs, list) else []:
                        if not isinstance(entry, dict):
                            continue
                        if plugin_id == identity and entry.get("scope") == value.get("scope", "user"):
                            continue
                        if isinstance(entry.get("installPath"), str) and str(canonical_home_path(Path(entry["installPath"]))) in snapshot.get("paths", {}):
                            raise runtime.DotAiError("Plugin cache is shared with another installation; refusing destructive lifecycle operation")
    elif kind == "tool":
        owned_paths = set(snapshot.get("paths", {}))
        backend_root = snapshot.get("backendRoot", {}).get("path")
        for other in manifest.get("packages", []):
            if other["name"] == identity or not other.get("enabled", True):
                continue
            executable = checked_tool_path(other, runner)
            if executable is not None and backend_root is not None:
                expected_target = None
                if other.get("recipe"):
                    for operation in ("install", "uninstall", "update"):
                        expected = catalog.tool_recipe_targets(other, runner, operation)
                        if expected is not None and expected.get("launcher") == str(executable):
                            expected_target = expected.get("target")
                            break
                target = catalog.tool_launcher_target(executable, runner, expected_target=expected_target)
                if executable.is_relative_to(Path(backend_root)) or target.is_relative_to(Path(backend_root)):
                    raise runtime.DotAiError("Tool backend environment contains another declared tool; refusing shared mutation")
            if executable is not None and str(executable.resolve()) in owned_paths:
                raise runtime.DotAiError("Tool binary is shared by another enabled declaration; refusing uninstall")


def run_command(command: str | list[str], label: str, runner: runtime.Runner) -> None:
    failures = len(runner.failures)
    result = runner.run(command, label)
    if not runner.dry_run and (result is None or result.returncode != 0 or len(runner.failures) != failures):
        raise runtime.DotAiError(f"{label} failed; desired declaration remains unchanged, inspect the reported cause and retry")


def write_mcp_config(target: Path, config: dict[str, Any], runner: runtime.Runner) -> None:
    target = mcp.safe_mcp_target(target)
    if runner.dry_run:
        runner.record_outcome("MCP runtime configuration", "planned")
        return
    if mcp.load_mcp_config(target) == config:
        runner.record_outcome("MCP runtime configuration", "unchanged")
        return
    if target.exists():
        manifests.backup_manifest(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
        runner.record_outcome("MCP runtime configuration", "changed")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mutate_runtime(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner, action: str, *, activate: Callable[[dict[str, Any]], int] | None = None) -> None:
    if kind == "tool" and value.get("managed") is not True:
        raise runtime.DotAiError("Tool lifecycle mutation requires managed:true; explicitly adopt the matching executable first")
    snapshot = observation(kind, identity, value, manifest, runner)
    receipts = read_receipts()
    key = component_key(kind, identity, value, manifest)
    receipt = receipts.get(key)
    enabling = action == "enable"
    if kind == "skill" and receipt and receipt.get("storage") and action in {"enable", "uninstall"}:
        stored_folders = []
        expected = {str(skills.skill_root(value) / name) for name in value.get("checkSkills", [])}
        if set(receipt["storage"]) != expected:
            raise runtime.DotAiError("Disabled skill selections changed; resolve the stored selection before enabling or uninstalling")
        for active, saved in receipt["storage"].items():
            path, stored = Path(active), Path(saved["path"])
            expected_storage = runtime.state_dir() / "disabled-skills" / key / path.name
            if stored != expected_storage or fingerprint(stored) != saved["digest"]:
                raise runtime.DotAiError("Disabled skill storage changed or escaped its scoped location; refusing mutation")
            if enabling and (path.exists() or path.is_symlink() or path.parent.resolve() != path.parent):
                raise runtime.DotAiError("Active skill destination exists or is redirected; refusing overwrite")
            stored_folders.append((path, stored))
        if not runner.dry_run:
            for active, stored in stored_folders:
                if enabling:
                    active.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(stored), str(active))
                else:
                    shutil.rmtree(stored)
                runner.record_outcome(f"{action.capitalize()} stored skill {active.name}", "changed")
            if enabling:
                record_install(kind, identity, value, manifest, runner)
            else:
                receipts.pop(key)
                write_receipts(receipts, runner)
        else:
            runner.record_outcome(f"{action.capitalize()} stored skill selection", "planned")
        return
    if not snapshot.get("present"):
        if kind == "tool" and not enabling:
            owned = receipt.get("snapshot", {}) if receipt else {}
            paths = list(owned.get("paths", {}))
            candidate = checked_tool_path(value, runner)
            if candidate is not None:
                paths.append(str(candidate))
            if owned.get("launcher", {}).get("path"):
                paths.append(owned["launcher"]["path"])
            if owned.get("backendRoot", {}).get("path"):
                paths.append(owned["backendRoot"]["path"])
            if any(Path(path).exists() or Path(path).is_symlink() for path in paths):
                raise runtime.DotAiError("Tool check failed but executable or backend remains; refusing to retire its declaration or receipt")
        if enabling:
            if kind == "mcp":
                mcp.safe_mcp_target(mcp.desired_mcp(manifest)[0])
            selector = qualified_selector(kind, identity, value)
            selected = filter_manifest(manifest, [selector])
            original = resolve_selector(selected, selector)[2]
            replace_declaration(selected, kind, identity, original, {**original, "enabled": True})
            if kind == "extension" and receipt:
                prove_owned(kind, identity, value, manifest, snapshot)
            if activate is None:
                raise runtime.DotAiError("Missing component activation requires the resolved installation coordinator")
            if activate(selected) != 0:
                raise runtime.DotAiError("Component activation failed; declaration remains unchanged")
        elif action == "uninstall" and receipt and not runner.dry_run:
            receipts.pop(key)
            write_receipts(receipts, runner)
        if not enabling and not receipt:
            runner.record_outcome(f"{kind.capitalize()} {identity}", "unchanged", "Not installed or registered")
        return
    if action != "uninstall" and snapshot.get("active") == enabling:
        runner.record_outcome(f"{kind.capitalize()} {identity}", "unchanged", "Activation already matches")
        return
    receipt = prove_owned(kind, identity, value, manifest, snapshot)
    if kind == "tool":
        check_tool_recipe_target(value, runner, "uninstall")
    ensure_unshared(kind, identity, value, manifest, snapshot, runner)
    print(f"{terminal.badge('RUN')} {action.capitalize()} {kind} {safe_label(identity)}: verified ownership and unchanged content")
    if kind == "tool":
        print("  Managed removal scope: " + ", ".join(safe_label(path) for path in _tool_scope_paths(snapshot)))
    if kind == "skill":
        storage = {}
        for path, digest in snapshot["paths"].items():
            folder = Path(path)
            if action == "uninstall":
                if not runner.dry_run:
                    shutil.rmtree(folder)
                runner.record_outcome(f"Uninstall skill {folder.name}", "planned" if runner.dry_run else "changed")
            else:
                target = runtime.state_dir() / "disabled-skills" / key / folder.name
                if target.exists() or target.is_symlink() or target.parent.resolve() != target.parent:
                    raise runtime.DotAiError("Disabled skill destination is occupied or redirected; refusing overwrite")
                storage[path] = {"path": str(target), "digest": digest}
        if action != "uninstall" and not runner.dry_run:
            for active, saved in storage.items():
                target = Path(saved["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(active, str(target))
                runner.record_outcome(f"Disable skill {Path(active).name}", "changed")
            receipt["storage"] = storage
            receipts[key] = receipt
            write_receipts(receipts, runner)
        elif action != "uninstall":
            runner.record_outcome(f"Disable skill selection {identity}", "planned")
    elif kind == "mcp":
        target, _ = mcp.desired_mcp(manifest)
        config = mcp.load_mcp_config(target)
        alias = snapshot["alias"]
        if action == "uninstall":
            del config["mcpServers"][alias]
            if "disabledServers" in config:
                config["disabledServers"] = [name for name in config["disabledServers"] if name != alias]
        else:
            config["mcpServers"][alias]["enabled"] = enabling
            if enabling and "disabledServers" in config:
                config["disabledServers"] = [name for name in config["disabledServers"] if name != alias]
        write_mcp_config(target, config, runner)
    elif kind == "extension":
        current = omp.configured_omp_extensions(runner)
        if current is None:
            raise runtime.DotAiError("Unable to read OMP extension registrations")
        merged = [entry for entry in current if omp.extension_identity(entry) != omp.extension_identity(identity)]
        if enabling:
            merged.append(identity)
        run_command(["omp", "config", "set", "extensions", json.dumps(merged, separators=(",", ":"))], "Change selected OMP extension registration", runner)
    elif kind == "plugin":
        run_command(["omp", "plugin", action, "--scope", value.get("scope", "user"), identity], f"{action.capitalize()} plugin {safe_label(identity)}", runner)
    elif kind == "marketplace":
        if enabling:
            run_command(["omp", "plugin", "marketplace", "add", value["source"]], f"Enable marketplace {safe_label(identity)}", runner)
        else:
            run_command(["omp", "plugin", "marketplace", "remove", identity], f"Unregister marketplace {safe_label(identity)}", runner)
    else:
        effective = catalog.resolve_uninstall(value, runner.platform)
        commands = runtime.selected(effective.get("uninstall", []), runner.platform)
        if not commands:
            raise runtime.DotAiError("Tool uninstall/disable is unsupported; supply a reviewed catalog or custom uninstall command")
        for command in commands:
            run_command(command, f"{action.capitalize()} tool {safe_label(identity)}", runner)
        if not runner.dry_run:
            root = snapshot.get("backendRoot", {}).get("path")
            if root and (Path(root).exists() or Path(root).is_symlink()):
                raise runtime.DotAiError("Tool removal left its owned backend environment; declaration and ownership receipt remain unchanged")
            if any(Path(path).exists() or Path(path).is_symlink() for path in snapshot.get("paths", {})):
                raise runtime.DotAiError("Tool removal did not remove its owned target; runtime may be partially changed, declaration and ownership receipt remain unchanged")
            launcher = snapshot.get("launcher", {}).get("path")
            if launcher and (Path(launcher).exists() or Path(launcher).is_symlink()):
                raise runtime.DotAiError("Tool removal left its owned launcher; declaration and ownership receipt remain unchanged")
            if observation(kind, identity, value, manifest, runner).get("present"):
                print(f"{terminal.badge('INACTIVE')} Owned copy removed; another installation remains available and was not changed")
    if not runner.dry_run:
        if action == "uninstall":
            receipts.pop(key, None)
            write_receipts(receipts, runner)
        elif kind not in {"skill", "marketplace", "tool"}:
            record_install(kind, identity, value, manifest, runner)


def ensure_disabled(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner) -> bool:
    before = len(runner.outcomes)
    try:
        mutate_runtime(kind, identity, value, manifest, runner, "disable")
        print(f"{terminal.badge('INACTIVE')} {kind.capitalize()} {safe_label(identity)}: inactive; enabled is false")
        if len(runner.outcomes) == before:
            runner.record_outcome(f"{kind.capitalize()} {identity}", "unchanged", "Already inactive")
        return True
    except (OSError, ValueError, runtime.DotAiError) as exc:
        if not runner.failures:
            runner.fail(f"{kind.capitalize()} {safe_label(identity)}", str(exc))
        return False


def qualified_selector(kind: str, identity: str, value: dict[str, Any]) -> str:
    if kind == "skill":
        identity += "@" + value.get("agent", "universal")
    elif kind == "plugin":
        identity += "#" + value.get("scope", "user")
    return kind + ":" + identity


def replace_declaration(manifest: dict[str, Any], kind: str, identity: str, value: dict[str, Any], replacement: dict[str, Any] | None) -> None:
    if kind in COLLECTIONS:
        section, _ = COLLECTIONS[kind]
        index = manifest[section].index(value)
        if replacement is None:
            del manifest[section][index]
        else:
            manifest[section][index] = replacement
    elif kind == "mcp":
        if replacement is None:
            del manifest["mcp"]["servers"][identity]
        else:
            manifest["mcp"]["servers"][identity] = replacement
    else:
        index = next(index for index, entry in enumerate(manifest["ompExtensions"])
                     if (entry if isinstance(entry, str) else entry["path"]) == identity)
        if replacement is None:
            del manifest["ompExtensions"][index]
        else:
            manifest["ompExtensions"][index] = replacement


def register_commands(subparsers: Any) -> None:
    listing = subparsers.add_parser("list", help="List declared components and observed ownership")
    listing.add_argument("selectors", nargs="*", metavar="TYPE:ID")
    for command in ("show", "adopt", "remove", "enable", "disable"):
        parser = subparsers.add_parser(command, help=f"{command.capitalize()} an individual declared component")
        parser.add_argument("selector", metavar="TYPE:ID")
        if command != "show":
            parser.add_argument("--dry-run", action="store_true")
        if command == "remove":
            parser.add_argument("--uninstall", action="store_true", help="Also remove only proven, unchanged, independent runtime content")


def _tool_scope_paths(observed: dict[str, Any]) -> list[str]:
    root = observed.get("backendRoot", {}).get("path")
    paths = list(observed.get("paths", {}))
    launcher = observed.get("launcher", {}).get("path")
    if launcher and launcher not in paths:
        paths.append(launcher)
    return ([root] + [path for path in paths if not Path(path).is_relative_to(Path(root))]) if root else paths


def inventory(kind: str, identity: str, value: dict[str, Any], manifest: dict[str, Any], runner: runtime.Runner, manifest_path: Path | None = None) -> None:
    try:
        observed = observation(kind, identity, value, manifest, runner)
        receipt = read_receipts().get(component_key(kind, identity, value, manifest))
        owned = (skill_receipt_owned(value) or skills.skill_source_owned(value, skills.skill_owners())) if kind == "skill" else bool(receipt and receipt.get("snapshot") == observed)
        state = "active" if observed.get("active") else "inactive" if observed.get("present") else "not installed/registered"
        paths = _tool_scope_paths(observed) if kind == "tool" else list(observed.get("paths", {})) or ([observed["path"]] if "path" in observed else [])
    except (OSError, ValueError, runtime.DotAiError) as exc:
        observed, owned, paths = {}, False, []
        state = "unverified: " + safe_label(exc)
    print(f"{terminal.heading(qualified_selector(kind, safe_label(identity), value))}")
    managed = value.get("managed", True)
    print(f"  Desired: {'enabled' if value.get('enabled', True) else 'disabled'}; management: {'declared' if managed else 'unmanaged'}")
    print(f"  Observed: {state}; ownership: {'proven, unchanged' if owned else 'unproven or modified; explicit adoption required'}")
    print(f"  Scope: {value.get('agent', value.get('scope', 'user'))}; source: {safe_label(value.get('source', value.get('recipe', value.get('url', value.get('path', identity)))))}")
    observed_version = observed.get("version", "not recorded")
    if kind == "tool" and observed.get("present"):
        check = runtime.selected(tool_value(value, runner.platform).get("check", []), runner.platform)
        valid, output = packages.package_version_check({}, runner, check)
        match = packages.PACKAGE_VERSION_PATTERN.search(output) if valid else None
        if match:
            observed_version = ".".join(part or "0" for part in match.groups())
    print(f"  Version/revision: desired {safe_label(value.get('revision', value.get('version', 'unspecified')))}; observed {safe_label(observed_version)}")
    if value.get("installerVersion"):
        print(f"  Installer version: {safe_label(value['installerVersion'])}")
    if paths:
        print("  Paths: " + ", ".join(safe_label(path) for path in paths))
    if manifest_path is not None:
        try:
            facts = locking.load(manifest_path)
            section = {"tool": "packages", "skill": "skills", "plugin": "plugins"}.get(kind)
            key = identity + "|" + value.get("agent", "universal") if kind == "skill" else identity + "|" + value.get("scope", "user") if kind == "plugin" else identity
            record = facts.get(section, {}).get(key, {}) if section else {}
            resolved = record.get("revision", record.get("version"))
            if resolved and resolved != "latest":
                print(f"  Reviewed lock target: {terminal.redact(safe_label(resolved))}; recorded for {terminal.redact(record.get('platform', 'unknown platform'))}.")
            else:
                print("  Reviewed lock target: not recorded; desired latest is not an observed version.")
            if record.get("installerVersion"):
                print(f"  Reviewed installer: skills@{terminal.redact(record['installerVersion'])} from the npm skills registry; source revision {terminal.redact(record.get('revision', 'not recorded'))}.")
            elif kind == "tool":
                origin = ("explicit custom manifest commands" if "install" in value or "pinInstall" in value or not value.get("recipe")
                          else "reviewed catalog recipe " + value["recipe"])
                print(f"  Installer origin: {terminal.redact(origin)}; no installer or source lookup performed.")
            metadata = record.get("sourceMetadata", {})
            if isinstance(metadata, dict) and isinstance(metadata.get("url"), str):
                print(f"  Recorded installer source: {terminal.redact(safe_label(metadata['url']))}.")
        except (OSError, ValueError, runtime.DotAiError) as exc:
            print(f"  Reviewed lock target: UNVERIFIED; {terminal.redact(exc)}")


def dispatch(args: argparse.Namespace, manifest: dict[str, Any], path: Path, runner: runtime.Runner, *, activate: Callable[[dict[str, Any], dict[str, Any]], int] | None = None) -> int:
    command = args.command
    if command == "list":
        view = filter_manifest(manifest, getattr(args, "selectors", []))
        entries = components(view)
        if not entries:
            print(f"{terminal.badge('INACTIVE')} No components declared in this selection")
        for kind, identity, value in entries:
            inventory(kind, identity, value, manifest, runner, path)
        return 0
    kind, identity, value = resolve_selector(manifest, args.selector)
    if command == "show":
        inventory(kind, identity, value, manifest, runner, path)
        return 0
    candidate = deepcopy(manifest)
    selected = resolve_selector(candidate, args.selector)[2]
    replacement = deepcopy(selected)
    if command == "remove":
        replacement = None
    elif command == "adopt":
        if kind == "tool":
            replacement["managed"] = True
    elif command in {"enable", "disable"}:
        replacement["enabled"] = command == "enable"
    else:
        raise runtime.DotAiError(f"Unsupported lifecycle command: {command}")
    replace_declaration(candidate, kind, identity, selected, replacement)
    manifests.validate_manifest(candidate)
    print(f"{terminal.badge('RUN')} {'Would ' if runner.dry_run else ''}{command} {kind} {safe_label(identity)} in {path}")
    if command == "adopt":
        observed = observation(kind, identity, value, manifest, runner)
        if not observed.get("present"):
            raise runtime.DotAiError("Adoption requires matching existing installed or registered content")
        if kind == "skill":
            ensure_unshared(kind, identity, value, manifest, observed, runner)
            if len(observed["paths"]) != len(value.get("checkSkills", [])):
                raise runtime.DotAiError("Cannot adopt an incomplete skill selection")
        elif kind == "mcp":
            target, _ = mcp.desired_mcp(manifest)
            if observed.get("path") != str(canonical_home_path(target.absolute())) or observed.get("section") != "mcpServers":
                raise runtime.DotAiError("External MCP providers cannot be adopted for writes; choose the matching target mcpServers registration")
            found = mcp.load_mcp_config(target)["mcpServers"][observed["alias"]]
            required = {key: field for key, field in value.items() if key != "enabled"}
            if not mcp.server_satisfies(found, required):
                raise runtime.DotAiError("Existing MCP registration does not satisfy declared fields; resolve it before adoption")
        elif kind == "extension" and not observed.get("paths"):
            raise runtime.DotAiError("Extension source is unavailable; cannot adopt it")
        if not runner.dry_run:
            record_install(kind, identity, value, manifest, runner)
        else:
            runner.record_outcome(f"{kind.capitalize()} {identity} ownership receipt", "planned")
    elif command == "remove" and getattr(args, "uninstall", False):
        mutate_runtime(kind, identity, value, manifest, runner, "uninstall")
    elif command in {"enable", "disable"}:
        mutate_runtime(kind, identity, {**value, "enabled": command == "enable"}, manifest, runner, command,
                       activate=(lambda selected: activate(selected, candidate)) if activate is not None else None)
    changed = candidate != manifest
    if not runner.dry_run:
        if changed:
            manifests.write_manifest(path, candidate, backup=path.exists())
            runner.record_outcome(f"{kind.capitalize()} {identity} declaration", "changed")
        manifest.clear()
        manifest.update(candidate)
    if not changed:
        runner.record_outcome(f"{kind.capitalize()} {identity} declaration", "unchanged")
    elif runner.dry_run:
        runner.record_outcome(f"{kind.capitalize()} {identity} declaration", "planned")
    outcome = "Preview only; no declarations, receipts, or runtime content changed" if runner.dry_run else "Declaration updated" if changed else "Component operation applied"
    if command == "remove" and not getattr(args, "uninstall", False):
        outcome = "Would forget declaration; installed content remains untouched" if runner.dry_run else "Forgot declaration; installed content remains untouched"
    print(f"{terminal.badge('OK')} {safe_label(identity)}: {outcome}")
    return 0
