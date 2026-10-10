"""Resolve execution targets before changes; persist only verified observations."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen
from . import catalog, packages, portable, runtime, skills

_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)(?![\w.+-])")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _version(output: str) -> str | None:
    matches = set(_VERSION.findall(output or ""))
    return next(iter(matches)) if len(matches) == 1 else None


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise runtime.DotAiError(f"Refusing linked lock file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "packages": {}, "skills": {}, "plugins": {}}
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Cannot read stack lock {path}; repair it explicitly") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or any(not isinstance(value.get(key), dict) for key in ("packages", "skills", "plugins")):
        raise runtime.DotAiError(f"Invalid stack lock {path}; repair it explicitly")
    return value


def load(manifest_path: str | Path) -> dict[str, Any]:
    """Read resolved facts without creating or changing a file."""
    return _read(portable.lock_path(manifest_path))


def _fetch_json(url: str) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "DotAi-stack-lock"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("metadata exceeds size limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("metadata is not an object")
        return value
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Unable to retrieve immutable source metadata from {url}; check network/access and retry (no latest fallback)") from exc


def _active(entry: dict[str, Any], *, package: bool = False) -> bool:
    return entry.get("enabled", True) and (not package or entry.get("managed") is True)


def _provenance(intent: dict[str, Any]) -> str:
    recipe = catalog.load_catalog()["recipes"].get(intent.get("recipe"), {})
    return _digest({"intent": {**intent, "enabled": intent.get("enabled", True)}, "recipe": recipe})


def _valid(record: Any, intent: str, label: str, mode: str) -> dict[str, Any] | None:
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("intent") != intent:
        if mode == "sync":
            raise runtime.DotAiError(f"{label}: stale lock intent/provenance; review and run explicit install/update")
        return None
    return record


def _package_version(package: dict[str, Any], runner: runtime.Runner) -> str | None:
    check = runtime.selected(package.get("check", []), runner.platform)
    healthy, output = packages.package_version_check({}, runner, check)
    return _version(output) if healthy else None


def _repository(source: str) -> str:
    repository = skills.github_repository_source(source)
    if not repository or (not source.startswith("https://github.com/") and os.environ.get("GH_HOST", "github.com").lower() not in {"", "github.com"}):
        raise runtime.DotAiError(f"Skills from {source}: immutable locking supports public GitHub repository roots; declare a supported root and revision explicitly")
    return repository


def _installer(skill: dict[str, Any], runner: runtime.Runner) -> tuple[str, str | None]:
    wanted = skill.get("installerVersion", "latest")
    if wanted != "latest" and (not isinstance(wanted, str) or not catalog.EXACT_VERSION.fullmatch(wanted)):
        raise runtime.DotAiError("Skills installerVersion must be an exact numeric version")
    metadata = _fetch_json(f"https://registry.npmjs.org/skills/{quote(wanted, safe='')}")
    version = metadata.get("version")
    if not isinstance(version, str) or not catalog.EXACT_VERSION.fullmatch(version) or (wanted != "latest" and version != wanted):
        raise runtime.DotAiError("Cannot resolve the requested skills installer version")
    if tuple(int(part) for part in version.split(".")) < (1, 7, 1):
        raise runtime.DotAiError(f"Skills installer {version}: immutable commit installation is unsupported; use reviewed 1.7.1 or newer")
    engine = metadata.get("engines", {}).get("node", "")
    minimum = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", engine)
    node = _version(runner.output(["node", "--version"]))
    if not minimum or not node or tuple(int(part) for part in node.split(".")) < tuple(int(part) for part in minimum.groups()):
        raise runtime.DotAiError(f"Skills installer {version} requires Node {engine or 'with a supported declared engine'}; install the prerequisite yourself before retrying")
    return version, metadata.get("dist", {}).get("integrity")


def _requested_names(skill: dict[str, Any]) -> set[str]:
    names = skill.get("checkSkills") or [skills.installed_skill_name(name) for name in skill.get("skills", [])]
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) for name in names):
        raise runtime.DotAiError(f"Skills from {skill['source']}: unsafe or wildcard check selection; declare explicit directory names")
    return set(names)


def _validate_skill_record(skill: dict[str, Any], record: dict[str, Any]) -> None:
    names = _requested_names(skill)
    trees, paths = record.get("trees"), record.get("sourcePaths")
    if (record.get("source") != skill["source"] or not isinstance(trees, dict) or set(trees) != names
            or not isinstance(paths, dict) or set(paths) != names):
        raise runtime.DotAiError(f"Skills from {skill['source']}: locked provenance does not match requested selection")
    for name in names:
        digest, path = trees[name], paths[name]
        if (not isinstance(digest, str) or not _SHA.fullmatch(digest) or not isinstance(path, str)
                or "\\" in path or "\0" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                or path.split("/")[-1] != name):
            raise runtime.DotAiError(f"Skills from {skill['source']}: unsafe locked tree/source provenance")


def _source_trees(skill: dict[str, Any], repository: str) -> tuple[str, dict[str, str], dict[str, str]]:
    ref = skill.get("revision") or "HEAD"
    commit = _fetch_json(f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}")
    revision = commit.get("sha")
    tree_sha = commit.get("commit", {}).get("tree", {}).get("sha")
    if not isinstance(revision, str) or not _SHA.fullmatch(revision) or not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
        raise runtime.DotAiError(f"Skills from {repository}: GitHub did not resolve an immutable revision/tree")
    response = _fetch_json(f"https://api.github.com/repos/{repository}/git/trees/{tree_sha}?recursive=1")
    rows = response.get("tree")
    if response.get("truncated") is not False or not isinstance(rows, list):
        raise runtime.DotAiError(f"Skills from {repository}: incomplete source tree; cannot prove resolution")
    directories = {row.get("path"): row.get("sha") for row in rows if isinstance(row, dict) and row.get("type") == "tree" and isinstance(row.get("path"), str) and isinstance(row.get("sha"), str) and _SHA.fullmatch(row["sha"])}
    names = _requested_names(skill)
    trees, paths = {}, {}
    for name in names:
        candidates = [path for path in directories if path.split("/")[-1] == name]
        if len(candidates) != 1:
            raise runtime.DotAiError(f"Skills from {repository}: ambiguous or missing source folder for {name}; supply explicit verified selections")
        paths[name] = candidates[0]
        trees[name] = directories[candidates[0]]
    return revision, trees, paths


def _skill_folders(skill: dict[str, Any]) -> dict[str, Path]:
    root = skills.skill_root(skill)
    if root.absolute() != root.resolve():
        raise runtime.DotAiError(f"Skills from {skill['source']}: linked or redirected skill root")
    folders = {}
    for name in sorted(_requested_names(skill)):
        folder = root / name
        if folder.absolute() != folder.resolve():
            raise runtime.DotAiError(f"Skills from {skill['source']}: linked or redirected skill directory {name}")
        folders[name] = folder
    return folders


def _installed_trees(skill: dict[str, Any], expected: dict[str, str], *, allow_missing: bool = False) -> dict[str, str]:
    actual = {}
    if not isinstance(expected, dict) or set(expected) != _requested_names(skill):
        raise runtime.DotAiError(f"Skills from {skill['source']}: unsafe locked check selection")
    for name, folder in _skill_folders(skill).items():
        if allow_missing and not folder.exists():
            continue
        try:
            actual[name] = skills.installed_skill_tree_hash(folder)
        except (OSError, ValueError) as exc:
            raise runtime.DotAiError(f"Skills from {skill['source']}: missing/unverified tree for {name}") from exc
    return actual


def _skill_owners() -> dict[str, Any]:
    xdg = os.environ.get("XDG_STATE_HOME")
    path = Path(xdg) / "skills" / ".skill-lock.json" if xdg else runtime.home_dir() / ".agents" / ".skill-lock.json"
    try:
        upstream = json.loads(path.read_text(encoding="utf-8"))
        owners = upstream.get("skills", {}) if isinstance(upstream, dict) and upstream.get("version") == 3 else {}
        return owners if isinstance(owners, dict) else {}
    except (OSError, ValueError, KeyError):
        return {}


def _require_current_skill_ownership(skill: dict[str, Any], receipt_owned: Callable[[dict[str, Any]], bool] | None) -> None:
    present = [name for name, folder in _skill_folders(skill).items() if folder.exists()]
    if not present:
        return
    current = copy.deepcopy(skill)
    current["checkSkills"] = present
    if (receipt_owned is not None and receipt_owned(current)) or skills.skill_source_owned(current, _skill_owners()):
        return
    raise runtime.DotAiError(f"Skills from {skill['source']}: current copy is unowned or locally modified; prove source ownership or explicitly adopt it before replacement")


def _upstream_skill_proof(skill: dict[str, Any], record: dict[str, Any], receipt_owned: Callable[[dict[str, Any]], bool] | None = None) -> None:
    if receipt_owned is not None and receipt_owned(skill):
        return
    owners = _skill_owners()
    repository = _repository(skill["source"])
    for name, tree in record["trees"].items():
        owner = owners.get(name, {})
        if not isinstance(owner, dict) or not isinstance(owner.get("sourceUrl"), str) or not isinstance(owner.get("skillPath"), str):
            raise runtime.DotAiError(f"Skills from {skill['source']}: missing upstream source provenance for {name}")
        source_url = owner.get("sourceUrl", "").split("/tree/")[0]
        skill_path = owner.get("skillPath", "").removesuffix("/SKILL.md")
        if (owner.get("sourceType") != "github" or skills.github_repository_source(owner.get("source")) != repository
                or skills.github_repository_source(source_url) != repository or owner.get("skillFolderHash") != tree
                or owner.get("ref") not in (None, record["revision"]) or skill_path != record["sourcePaths"][name]):
            raise runtime.DotAiError(f"Skills from {skill['source']}: upstream provenance does not match resolved revision/tree for {name}")


def _canonical_home_path(path: Path) -> Path:
    configured = Path(os.environ.get("DOTAI_HOME", str(Path.home()))).expanduser().absolute()
    actual = runtime.home_dir()
    if configured != actual and path.is_relative_to(configured):
        return actual / path.relative_to(configured)
    return path


def _plugin_registry(path: Path) -> dict[str, Any] | None:
    path = _canonical_home_path(path)
    if path.absolute() != path.resolve():
        raise runtime.DotAiError(f"Plugin registry has a linked or redirected scoped path: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Cannot read observed plugin registry: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != 2 or not isinstance(value.get("plugins"), dict):
        raise runtime.DotAiError(f"Unsupported observed plugin registry format: {path}")
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in value["plugins"].values()):
        raise runtime.DotAiError(f"Invalid scoped plugin registry records: {path}")
    return value


def _plugin_observation(plugin: dict[str, Any]) -> dict[str, Any] | None:
    root = Path.cwd() if plugin.get("scope", "user") == "project" else runtime.home_dir()
    registry = root / ".omp" / "plugins" / "installed_plugins.json"
    value = _plugin_registry(registry)
    if value is None:
        return None
    rows = value["plugins"].get(plugin["id"], [])
    matches = [row for row in rows if isinstance(row, dict) and row.get("scope") == plugin.get("scope", "user")]
    if not matches:
        return None
    if len(matches) != 1:
        raise runtime.DotAiError(f"Plugin {plugin['id']}: ambiguous scoped registry provenance")
    row = matches[0]
    version, path, revision = row.get("version"), row.get("installPath"), row.get("gitCommitSha")
    if not isinstance(version, str) or not version or not isinstance(path, str) or not Path(path).is_absolute():
        raise runtime.DotAiError(f"Plugin {plugin['id']}: missing observed version/source path")
    if revision is not None and (not isinstance(revision, str) or not _SHA.fullmatch(revision)):
        raise runtime.DotAiError(f"Plugin {plugin['id']}: invalid observed source revision")
    cache = runtime.home_dir() / ".omp" / "plugins" / "cache" / "plugins"
    folder = _canonical_home_path(Path(path))
    if folder == cache or not folder.is_relative_to(cache):
        raise runtime.DotAiError(f"Plugin {plugin['id']}: source directory is outside the allowed OMP cache scope")
    if folder.absolute() != folder.resolve():
        raise runtime.DotAiError(f"Plugin {plugin['id']}: linked or redirected plugin cache directory")
    registries = {registry, runtime.home_dir() / ".omp" / "plugins" / "installed_plugins.json", Path.cwd() / ".omp" / "plugins" / "installed_plugins.json"}
    for candidate in registries:
        observed_registry = value if candidate == registry else _plugin_registry(candidate)
        if observed_registry is None:
            continue
        for identity, records in observed_registry["plugins"].items():
            for reference in records:
                target = reference.get("installPath")
                if isinstance(target, str) and _canonical_home_path(Path(target)).resolve() == folder:
                    if identity != plugin["id"] or reference.get("scope") != plugin.get("scope", "user"):
                        raise runtime.DotAiError(f"Plugin {plugin['id']}: shared or ambiguous cross-scope cache provenance")
    try:
        tree = skills.installed_skill_tree_hash(folder)
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Plugin {plugin['id']}: cannot verify installed tree") from exc
    return {"version": version, "revision": revision, "tree": tree}


def _metadata_field(metadata: dict[str, Any], path: Any) -> Any:
    value: Any = metadata
    if not isinstance(path, list) or not path or any(not isinstance(key, str) for key in path):
        raise runtime.DotAiError("Unsupported source metadata field path")
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise runtime.DotAiError("Requested source metadata field is unavailable")
        value = value[key]
    return value


def _check_python_requirement(requirement: Any, label: str) -> None:
    if not isinstance(requirement, str) or not requirement.strip():
        raise runtime.DotAiError(f"{label}: source has no verifiable Python requirement")
    actual = tuple(sys.version_info[:3])
    for constraint in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,2})\s*", constraint)
        if not match:
            raise runtime.DotAiError(f"{label}: unsupported Python requirement {requirement!r}; review source compatibility explicitly")
        operation, value = match.groups()
        parts = tuple(int(part) for part in value.split("."))
        minimum = parts + (0,) * (3 - len(parts))
        compatible = {
            ">=": actual >= minimum, "<=": actual <= minimum,
            ">": actual > minimum, "<": actual < minimum,
            "==": actual == minimum, "!=": actual != minimum,
        }[operation]
        if not compatible:
            raise runtime.DotAiError(f"{label}: source requires Python {requirement}; current CLI Python is {'.'.join(map(str, actual))}. Supply a compatible external interpreter; DotAi will not download one.")


def _resolve_package_metadata(package: dict[str, Any], target: str, cached: Any = None, runner: runtime.Runner | None = None) -> tuple[str, dict[str, Any]]:
    descriptor = package["versionMetadata"]
    if isinstance(descriptor, dict) and descriptor.get("kind") == "github-release":
        if runner is None:
            raise runtime.DotAiError("Native artifact resolution requires a selected platform")
        if isinstance(cached, dict) and cached.get("version") == target and target != "latest":
            return target, catalog.validate_native_metadata(descriptor, cached, target, runner)
        template = descriptor.get("latest" if target == "latest" else "exact")
        if not isinstance(template, str) or not template.startswith("https://"):
            raise runtime.DotAiError(f"{package['name']}: unsupported native release metadata URL")
        release = _fetch_json(template.replace("{version}", target))
        tag = release.get("tag_name")
        version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else ""
        if not catalog.EXACT_VERSION.fullmatch(version) or (target != "latest" and version != target):
            raise runtime.DotAiError(f"{package['name']}: requested version pin does not match available native release")
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise runtime.DotAiError(f"{package['name']}: native version pin requires a published stable release")
        artifact = catalog.native_artifact_name(descriptor, runner)
        assets = release.get("assets")
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == artifact] if isinstance(assets, list) else []
        if len(matches) != 1 or matches[0].get("state") != "uploaded":
            raise runtime.DotAiError(f"{package['name']}: exact release artifact {artifact} is unavailable")
        asset = matches[0]
        digest = asset.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise runtime.DotAiError(f"{package['name']}: exact release artifact has no verified SHA-256 digest")
        facts = {"version": version, "release": tag, "artifact": artifact, "url": asset.get("browser_download_url"), "sha256": digest[7:].lower()}
        return version, catalog.validate_native_metadata(descriptor, facts, version, runner)
    if isinstance(cached, dict) and cached.get("version") == target and target != "latest":
        _check_python_requirement(cached.get("pythonRequirement"), package["name"])
        return target, copy.deepcopy(cached)
    if not isinstance(descriptor, dict):
        raise runtime.DotAiError(f"{package['name']}: invalid reviewed source metadata descriptor")
    template = descriptor.get("latest" if target == "latest" else "exact")
    if not isinstance(template, str) or not template.startswith("https://"):
        raise runtime.DotAiError(f"{package['name']}: unsupported source metadata URL")
    metadata = _fetch_json(template.replace("{version}", target))
    version = _metadata_field(metadata, descriptor.get("versionPath"))
    if not isinstance(version, str) or not catalog.EXACT_VERSION.fullmatch(version) or (target != "latest" and version != target):
        raise runtime.DotAiError(f"{package['name']}: requested version does not match verified source metadata")
    requirement = _metadata_field(metadata, descriptor.get("pythonRequirementPath"))
    _check_python_requirement(requirement, package["name"])
    return version, {"version": version, "pythonRequirement": requirement}


def _plugin_provenance(plugin: dict[str, Any], manifest: dict[str, Any]) -> str:
    marketplace_name = plugin["id"].rpartition("@")[2]
    matching = [marketplace for marketplace in manifest.get("marketplaces", []) if marketplace.get("name") == marketplace_name]
    if len(matching) > 1:
        raise runtime.DotAiError(f"Plugin {plugin['id']}: ambiguous declared marketplace provenance")
    marketplace = {**matching[0], "enabled": matching[0].get("enabled", True)} if matching else None
    return _digest({"plugin": {**plugin, "enabled": plugin.get("enabled", True)}, "marketplace": marketplace})


def prepare(manifest: dict[str, Any], manifest_path: str | Path, runner: runtime.Runner, mode: str, *, force: bool = False, update_skills: bool = False, refresh_sources: set[str] | None = None, skill_receipt_owned: Callable[[dict[str, Any]], bool] | None = None, provenance_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preflight the complete selected plan without changing files or tools."""
    if mode not in {"install", "update", "sync"}:
        raise runtime.DotAiError(f"Unsupported lock preparation mode: {mode}")
    runner._dotai_lock_plan = None
    before = load(manifest_path)
    effective = catalog.materialize(manifest, runner.platform)
    full_intent = provenance_manifest if provenance_manifest is not None else manifest
    plan = {"path": portable.lock_path(manifest_path).absolute(), "mode": mode, "before": before, "packages": {}, "skills": {}, "plugins": {}}
    for index, (intent, package) in enumerate(zip(manifest.get("packages", []), effective["packages"])):
        if not _active(package, package=True):
            continue
        name, provenance = package["name"], _provenance(intent)
        locked = _valid(before["packages"].get(name), provenance, name, mode)
        observed = _package_version(package, runner)
        healthy = observed is not None and packages.package_check(package, runner)
        reuse = locked is not None and (mode != "update" or package.get("updatePolicy") == "pinned")
        target = locked.get("version") if reuse else package.get("version", "latest")
        metadata = locked.get("sourceMetadata") if reuse else None
        if reuse and (not isinstance(target, str) or not catalog.EXACT_VERSION.fullmatch(target)):
            raise runtime.DotAiError(f"{name}: invalid locked observed version")
        if mode == "sync" and target == "latest" and observed is None:
            raise runtime.DotAiError(f"{name}: no resolved lock for missing latest component; run explicit install/update first")
        if not force and healthy and package.get("version", "latest") == "latest" and (
            mode == "install" or (mode == "sync" and target == "latest")
        ) and (not reuse or observed == target or package.get("updatePolicy") != "pinned"):
            target = observed
            if isinstance(metadata, dict) and metadata.get("version") != target:
                metadata = None
        mutation_needed = (
            force or not healthy or observed != target
            or (mode == "update" and package.get("updatePolicy") != "pinned")
        )
        native_update = bool(
            package.get("nativeUpdate") and "update" not in intent
            and isinstance(package.get("versionMetadata"), dict)
            and package["versionMetadata"].get("kind") == "github-release"
            and packages.package_operation({**package, "version": target}, runner, mode, force) == "update"
        )
        if native_update and target != "latest":
            if observed != target or not healthy:
                raise runtime.DotAiError(f"{name}: native update cannot target an exact version; review 'install --force' to deploy the requested pin")
            package["version"] = target
            package["updatePolicy"] = "pinned"
            native_update = mutation_needed = False
        if native_update:
            _, metadata = _resolve_package_metadata(package, "latest", None, runner)
            minimum = package.get("minimumVersion")
            if minimum:
                available = tuple(int(part) for part in metadata["version"].split("."))
                required = tuple(int(part) for part in minimum.split("."))
                if available + (0,) * (3 - len(available)) < required + (0,) * (3 - len(required)):
                    raise runtime.DotAiError(f"{name}: available native release cannot satisfy minimumVersion {minimum}; no changes made")
            package["nativeAvailableVersion"] = metadata["version"]
            target = "latest"
        if package.get("versionMetadata") and mutation_needed and not native_update:
            target, metadata = _resolve_package_metadata(package, target, metadata, runner)
        if target != "latest":
            if mutation_needed:
                package = catalog.resolve_package({**intent, "version": target}, runner.platform)
            else:
                package["version"] = target
        if mutation_needed and not native_update and isinstance(package.get("versionMetadata"), dict) and package["versionMetadata"].get("kind") == "github-release":
            catalog.bind_native_release(package, metadata, runner, intent)
        effective["packages"][index] = package
        plan["packages"][name] = {"intent": provenance, "platform": runner.platform, "source": package.get("source"), "target": target, "package": copy.deepcopy(package), "sourceMetadata": metadata, "nativeUpdate": native_update}
    for intent, skill in zip(manifest.get("skills", []), effective.get("skills", [])):
        if not _active(skill):
            continue
        key = skill["source"] + "|" + skill.get("agent", "universal")
        provenance = _digest({**intent, "enabled": intent.get("enabled", True)})
        refresh = force or mode == "update" or (mode == "sync" and (update_skills or (refresh_sources is not None and skill["source"] in refresh_sources)))
        skill_mode = "update" if refresh and mode != "install" else mode
        if refresh or force:
            _require_current_skill_ownership(skill, skill_receipt_owned)
        locked = _valid(before["skills"].get(key), provenance, skill["source"], skill_mode)
        reuse = locked is not None and (skill_mode != "update" or skill.get("updatePolicy") == "pinned")
        if reuse:
            record = copy.deepcopy(locked)
            if not isinstance(record.get("revision"), str) or not _SHA.fullmatch(record["revision"]) or not isinstance(record.get("installerVersion"), str) or not catalog.EXACT_VERSION.fullmatch(record["installerVersion"]) or not record.get("trees") or not isinstance(record.get("sourcePaths"), dict):
                raise runtime.DotAiError(f"Skills from {skill['source']}: invalid locked source/installer provenance")
        else:
            if skill_mode == "sync" and not skill.get("revision") and not skills.skill_status(skill)[0]:
                raise runtime.DotAiError(f"Skills from {skill['source']}: missing mutable source has no lock; run explicit install/update first")
            repository = _repository(skill["source"])
            installer, integrity = _installer(skill, runner)
            revision, trees, paths = _source_trees(skill, repository)
            record = {"intent": provenance, "platform": runner.platform, "source": skill["source"], "revision": revision, "installerVersion": installer, "installerIntegrity": integrity, "trees": trees, "sourcePaths": paths}
        _validate_skill_record(skill, record)
        actual = _installed_trees(skill, record["trees"], allow_missing=True)
        differs = any(tree != record["trees"][name] for name, tree in actual.items())
        if differs and not refresh:
            if mode == "install" and reuse:
                _require_current_skill_ownership(skill, skill_receipt_owned)
                refresh = True
            else:
                raise runtime.DotAiError(f"Skills from {skill['source']}: DRIFT, installed content is changed or modified relative to the resolved target; preserve it and review explicit update")
        if skill_mode == "sync" and not reuse and actual:
            present_skill = {**skill, "checkSkills": list(actual)}
            _upstream_skill_proof(present_skill, {**record, "trees": actual}, skill_receipt_owned)
        skill["revision"] = record["revision"]
        skill["installerVersion"] = record["installerVersion"]
        plan["skills"][key] = {"record": record, "skill": copy.deepcopy(skill), "reuse": reuse, "actual": actual, "refresh": refresh}
    for plugin in effective.get("plugins", []):
        if not _active(plugin):
            continue
        key = plugin["id"] + "|" + plugin.get("scope", "user")
        provenance = _plugin_provenance(plugin, full_intent)
        locked = _valid(before["plugins"].get(key), provenance, plugin["id"], mode)
        observed = _plugin_observation(plugin)
        wanted = plugin.get("version", "latest")
        reuse = locked is not None and (mode != "update" or plugin.get("updatePolicy") == "pinned")
        target = locked.get("version") if reuse else wanted
        if target != "latest" and (not observed or observed["version"] != target):
            raise runtime.DotAiError(f"Plugin {plugin['id']}: upstream plugin installer cannot honor exact pin {target}; restore reviewed marketplace source manually")
        if reuse and observed and any(observed.get(field) != locked.get(field) for field in ("version", "revision", "tree")):
            raise runtime.DotAiError(f"Plugin {plugin['id']}: DRIFT, installed tree/source differs from lock")
        if mode == "sync" and not locked and not observed:
            raise runtime.DotAiError(f"Plugin {plugin['id']}: missing latest plugin has no resolved lock; use explicit install")
        if target != "latest":
            plugin["version"] = target
        plan["plugins"][key] = {"intent": provenance, "platform": runner.platform, "plugin": copy.deepcopy(plugin), "target": target}
    plan["acceptedManifests"] = {_digest(manifest), _digest(effective)}
    plan["acceptedProvenance"] = {_digest(full_intent)}
    if provenance_manifest is None:
        plan["acceptedProvenance"].add(_digest(effective))
    plan["effective"] = copy.deepcopy(effective)
    plan["skillReceiptOwned"] = skill_receipt_owned
    runner._dotai_lock_plan = plan
    return effective


def prepared(manifest: dict[str, Any], manifest_path: str | Path, runner: runtime.Runner, mode: str, *, provenance_manifest: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return a copied, previously reviewed execution plan without resolving it."""
    plan = getattr(runner, "_dotai_lock_plan", None)
    if not plan or plan["mode"] != mode or plan["path"] != portable.lock_path(manifest_path).absolute():
        return None
    if _digest(manifest) not in plan["acceptedManifests"]:
        return None
    full_intent = provenance_manifest if provenance_manifest is not None else manifest
    if _digest(full_intent) not in plan["acceptedProvenance"]:
        return None
    _require_snapshot(plan)
    return copy.deepcopy(plan["effective"])


def _require_snapshot(plan: dict[str, Any]) -> None:
    if _read(plan["path"]) != plan["before"]:
        raise runtime.DotAiError("Stack lock changed since prepare; rerun after concurrent reconciliation finishes")


def _owns_guard(guard: Path, identity: os.stat_result) -> bool:
    try:
        current = guard.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


@contextmanager
def _writer(path: Path):
    guard = path.with_name(path.name + ".write")
    try:
        descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise runtime.DotAiError(f"Cannot exclusively reconcile stack lock {path}; another writer may be active") from exc
    identity = None
    try:
        try:
            identity = os.fstat(descriptor)
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        yield guard, identity
    finally:
        if identity is not None and _owns_guard(guard, identity):
            guard.unlink(missing_ok=True)


@contextmanager
def execution(manifest_path: str | Path, runner: runtime.Runner):
    """Hold a preflighted stack's exclusive lease across actions and recording."""
    plan = getattr(runner, "_dotai_lock_plan", None)
    if not plan or plan["path"] != portable.lock_path(manifest_path).absolute():
        raise runtime.DotAiError("Stack execution requires successful prepare for this manifest")
    if runner.dry_run:
        _require_snapshot(plan)
        yield
        return
    if getattr(runner, "_dotai_lock_lease", None) is not None:
        raise runtime.DotAiError("Another stack execution is active for this runner")
    with _writer(plan["path"]) as guard:
        _require_snapshot(plan)
        runner._dotai_lock_lease = (plan, guard)
        try:
            yield
        finally:
            runner._dotai_lock_lease = None


def _write_atomic(path: Path, value: dict[str, Any], before: dict[str, Any], *, guard_held: bool = False) -> bool:
    with nullcontext() if guard_held else _writer(path):
        temporary = None
        try:
            if _read(path) != before:
                raise runtime.DotAiError("Stack lock changed since prepare; rerun after concurrent reconciliation finishes")
            if value == before and path.exists():
                return False
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".dotai-lock-", delete=False) as handle:
                temporary = Path(handle.name)
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def record(manifest: dict[str, Any], manifest_path: str | Path, runner: runtime.Runner, mode: str) -> bool:
    """Record observations only after successful, non-dry-run reconciliation."""
    if runner.dry_run or runner.failures:
        return False
    plan = getattr(runner, "_dotai_lock_plan", None)
    if not plan or plan["mode"] != mode or plan["path"] != portable.lock_path(manifest_path).absolute():
        raise runtime.DotAiError("Stack lock record requires successful prepare for this manifest and mode")
    if _digest(manifest) not in plan["acceptedManifests"]:
        raise runtime.DotAiError("Manifest intent changed since lock preparation; rerun before recording")
    value = copy.deepcopy(plan["before"])
    for name, item in plan["packages"].items():
        observed = _package_version(item["package"], runner)
        if observed is None or (item["target"] != "latest" and observed != item["target"]):
            raise runtime.DotAiError(f"{name}: observed version does not match resolved target; lock unchanged")
        if item.get("nativeUpdate"):
            _, item["sourceMetadata"] = _resolve_package_metadata(item["package"], observed, item["sourceMetadata"], runner)
            executable = packages.checked_package_path(item["package"], runner)
            if executable is None:
                raise runtime.DotAiError(f"{name}: native release executable is unverified; lock unchanged")
            executable = catalog.tool_launcher_target(executable, runner)
            digest = hashlib.sha256()
            with executable.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != item["sourceMetadata"]["sha256"]:
                raise runtime.DotAiError(f"{name}: observed native binary differs from the release artifact; lock unchanged")
        value["packages"][name] = {key: item[key] for key in ("intent", "platform", "source")}
        value["packages"][name]["version"] = observed
        if item["sourceMetadata"] is not None:
            value["packages"][name]["sourceMetadata"] = copy.deepcopy(item["sourceMetadata"])
    for key, item in plan["skills"].items():
        resolved = copy.deepcopy(item["record"])
        if _installed_trees(item["skill"], resolved["trees"]) != resolved["trees"]:
            raise runtime.DotAiError(f"Skills from {item['skill']['source']}: modified tree does not prove resolved revision; lock unchanged")
        if not item["reuse"] or item["actual"] != resolved["trees"]:
            _upstream_skill_proof(item["skill"], resolved, plan["skillReceiptOwned"])
            actual_installer = _version(runner.output(["npx", "--yes", "skills@" + resolved["installerVersion"], "--version"]))
            if actual_installer != resolved["installerVersion"]:
                raise runtime.DotAiError("Skills installer observed version does not match resolved installer; lock unchanged")
        value["skills"][key] = resolved
    for key, item in plan["plugins"].items():
        observed = _plugin_observation(item["plugin"])
        if not observed or (item["target"] != "latest" and observed["version"] != item["target"]):
            raise runtime.DotAiError(f"Plugin {item['plugin']['id']}: no matching observed version; lock unchanged")
        value["plugins"][key] = {"intent": item["intent"], "platform": item["platform"], **observed}
    lease = getattr(runner, "_dotai_lock_lease", None)
    if lease is not None and (lease[0] is not plan or not _owns_guard(*lease[1])):
        raise runtime.DotAiError("Stack execution lease changed; rerun after concurrent reconciliation finishes")
    return _write_atomic(plan["path"], value, plan["before"], guard_held=lease is not None)
