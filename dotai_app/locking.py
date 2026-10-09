"""Resolve execution targets before changes; persist only verified observations."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen
from . import catalog, portable, runtime, skills

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
    return _digest({"intent": intent, "recipe": recipe})


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
    return _version(runner.output(check)) if check else None


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


def _installed_trees(skill: dict[str, Any], expected: dict[str, str], *, allow_missing: bool = False) -> dict[str, str]:
    actual = {}
    if not isinstance(expected, dict) or set(expected) != _requested_names(skill):
        raise runtime.DotAiError(f"Skills from {skill['source']}: unsafe locked check selection")
    root = skills.skill_root(skill)
    if root.absolute() != root.resolve():
        raise runtime.DotAiError(f"Skills from {skill['source']}: linked or redirected skill root")
    for name in expected:
        folder = skills.skill_root(skill) / name
        if folder.absolute() != folder.resolve():
            raise runtime.DotAiError(f"Skills from {skill['source']}: linked or redirected skill directory {name}")
        if allow_missing and not folder.exists():
            continue
        try:
            actual[name] = skills.installed_skill_tree_hash(folder)
        except (OSError, ValueError) as exc:
            raise runtime.DotAiError(f"Skills from {skill['source']}: missing/unverified tree for {name}") from exc
    return actual


def _upstream_skill_proof(skill: dict[str, Any], record: dict[str, Any], receipt_owned: Callable[[dict[str, Any]], bool] | None = None) -> None:
    if receipt_owned is not None and receipt_owned(skill):
        return
    xdg = os.environ.get("XDG_STATE_HOME")
    path = Path(xdg) / "skills" / ".skill-lock.json" if xdg else runtime.home_dir() / ".agents" / ".skill-lock.json"
    try:
        upstream = json.loads(path.read_text(encoding="utf-8"))
        owners = upstream.get("skills", {}) if isinstance(upstream, dict) and upstream.get("version") == 3 else {}
        if not isinstance(owners, dict):
            owners = {}
    except (OSError, ValueError, KeyError):
        owners = {}
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


def _plugin_observation(plugin: dict[str, Any]) -> dict[str, Any] | None:
    root = Path.cwd() if plugin.get("scope", "user") == "project" else runtime.home_dir()
    registry = root / ".omp" / "plugins" / "installed_plugins.json"
    try:
        value = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Plugin {plugin['id']}: cannot read observed registry") from exc
    if not isinstance(value, dict) or value.get("version") != 2 or not isinstance(value.get("plugins"), dict):
        raise runtime.DotAiError(f"Plugin {plugin['id']}: unsupported observed registry format")
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
    try:
        tree = skills.installed_skill_tree_hash(Path(path))
    except (OSError, ValueError) as exc:
        raise runtime.DotAiError(f"Plugin {plugin['id']}: cannot verify installed tree") from exc
    return {"version": version, "revision": revision, "tree": tree}


def prepare(manifest: dict[str, Any], manifest_path: str | Path, runner: runtime.Runner, mode: str, *, skill_receipt_owned: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Preflight the complete selected plan without changing files or tools."""
    if mode not in {"install", "update", "sync"}:
        raise runtime.DotAiError(f"Unsupported lock preparation mode: {mode}")
    runner._dotai_lock_plan = None
    before = load(manifest_path)
    effective = catalog.materialize(manifest, runner.platform)
    plan = {"path": portable.lock_path(manifest_path), "mode": mode, "before": before, "packages": {}, "skills": {}, "plugins": {}}
    for index, (intent, package) in enumerate(zip(manifest.get("packages", []), effective["packages"])):
        if not _active(package, package=True):
            continue
        name, provenance = package["name"], _provenance(intent)
        locked = _valid(before["packages"].get(name), provenance, name, mode)
        observed = _package_version(package, runner)
        target = package.get("version", "latest")
        if locked and (mode == "sync" or package.get("updatePolicy") == "pinned"):
            target = locked.get("version")
            if not isinstance(target, str) or not catalog.EXACT_VERSION.fullmatch(target):
                raise runtime.DotAiError(f"{name}: invalid locked observed version")
            if observed != target:
                package = catalog.resolve_package({**intent, "version": target}, runner.platform)
            else:
                package["version"] = target
            effective["packages"][index] = package
        elif mode == "sync" and target == "latest" and observed is None:
            raise runtime.DotAiError(f"{name}: no resolved lock for missing latest component; run explicit install/update first")
        elif mode == "sync" and target == "latest":
            target = observed
            package["version"] = target
        plan["packages"][name] = {"intent": provenance, "platform": runner.platform, "source": package.get("source"), "target": target, "package": copy.deepcopy(package)}
    for intent, skill in zip(manifest.get("skills", []), effective.get("skills", [])):
        if not _active(skill):
            continue
        key = skill["source"] + "|" + skill.get("agent", "universal")
        provenance = _digest(intent)
        locked = _valid(before["skills"].get(key), provenance, skill["source"], mode)
        reuse = locked is not None and (mode == "sync" or skill.get("updatePolicy") == "pinned")
        if reuse:
            record = copy.deepcopy(locked)
            if not isinstance(record.get("revision"), str) or not _SHA.fullmatch(record["revision"]) or not isinstance(record.get("installerVersion"), str) or not catalog.EXACT_VERSION.fullmatch(record["installerVersion"]) or not record.get("trees") or not isinstance(record.get("sourcePaths"), dict):
                raise runtime.DotAiError(f"Skills from {skill['source']}: invalid locked source/installer provenance")
        else:
            if mode == "sync" and not skill.get("revision") and not skills.skill_status(skill)[0]:
                raise runtime.DotAiError(f"Skills from {skill['source']}: missing mutable source has no lock; run explicit install/update first")
            repository = _repository(skill["source"])
            installer, integrity = _installer(skill, runner)
            revision, trees, paths = _source_trees(skill, repository)
            record = {"intent": provenance, "platform": runner.platform, "source": skill["source"], "revision": revision, "installerVersion": installer, "installerIntegrity": integrity, "trees": trees, "sourcePaths": paths}
        _validate_skill_record(skill, record)
        actual = _installed_trees(skill, record["trees"], allow_missing=True)
        if mode == "sync" and any(tree != record["trees"][name] for name, tree in actual.items()):
            raise runtime.DotAiError(f"Skills from {skill['source']}: DRIFT, locally modified tree; preserve it and review explicit update")
        if mode == "sync" and not reuse and actual:
            _upstream_skill_proof(skill, record, skill_receipt_owned)
        skill["revision"] = record["revision"]
        skill["installerVersion"] = record["installerVersion"]
        plan["skills"][key] = {"record": record, "skill": copy.deepcopy(skill), "reuse": reuse, "actual": actual}
    for plugin in effective.get("plugins", []):
        if not _active(plugin):
            continue
        key = plugin["id"] + "|" + plugin.get("scope", "user")
        provenance = _digest(plugin)
        locked = _valid(before["plugins"].get(key), provenance, plugin["id"], mode)
        observed = _plugin_observation(plugin)
        wanted = plugin.get("version", "latest")
        target = locked.get("version") if locked and mode == "sync" else wanted
        if target != "latest" and (not observed or observed["version"] != target):
            raise runtime.DotAiError(f"Plugin {plugin['id']}: upstream plugin installer cannot honor exact pin {target}; restore reviewed marketplace source manually")
        if mode == "sync" and locked and observed and any(observed.get(field) != locked.get(field) for field in ("version", "revision", "tree")):
            raise runtime.DotAiError(f"Plugin {plugin['id']}: DRIFT, installed tree/source differs from lock")
        if mode == "sync" and not locked and not observed:
            raise runtime.DotAiError(f"Plugin {plugin['id']}: missing latest plugin has no resolved lock; use explicit install")
        plan["plugins"][key] = {"intent": provenance, "platform": runner.platform, "plugin": copy.deepcopy(plugin), "target": target}
    plan["acceptedManifests"] = {_digest(manifest), _digest(effective)}
    plan["skillReceiptOwned"] = skill_receipt_owned
    runner._dotai_lock_plan = plan
    return effective


def _write_atomic(path: Path, value: dict[str, Any], before: dict[str, Any]) -> bool:
    guard = path.with_name(path.name + ".write")
    try:
        descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise runtime.DotAiError(f"Cannot exclusively record stack lock {path}; another writer may be active") from exc
    os.close(descriptor)
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
        guard.unlink(missing_ok=True)


def record(manifest: dict[str, Any], manifest_path: str | Path, runner: runtime.Runner, mode: str) -> bool:
    """Record observations only after successful, non-dry-run reconciliation."""
    if runner.dry_run or runner.failures:
        return False
    plan = getattr(runner, "_dotai_lock_plan", None)
    if not plan or plan["mode"] != mode or plan["path"] != portable.lock_path(manifest_path):
        raise runtime.DotAiError("Stack lock record requires successful prepare for this manifest and mode")
    if _digest(manifest) not in plan["acceptedManifests"]:
        raise runtime.DotAiError("Manifest intent changed since lock preparation; rerun before recording")
    value = copy.deepcopy(plan["before"])
    for name, item in plan["packages"].items():
        observed = _package_version(item["package"], runner)
        if observed is None or (item["target"] != "latest" and observed != item["target"]):
            raise runtime.DotAiError(f"{name}: observed version does not match resolved target; lock unchanged")
        value["packages"][name] = {key: item[key] for key in ("intent", "platform", "source")}
        value["packages"][name]["version"] = observed
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
    return _write_atomic(plan["path"], value, plan["before"])
