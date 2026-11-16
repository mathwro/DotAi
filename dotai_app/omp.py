"""OMP marketplaces, plugins, global extensions, and managed child environment defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import re
import tempfile
from . import manifest as manifests
from . import runtime
from . import terminal


def reconcile_plugins(manifest: dict[str, Any], runner: runtime.Runner, mode: str) -> None:
    for marketplace in manifest["marketplaces"]:
        if mode == "update":
            command = ["omp", "plugin", "marketplace", "update", marketplace["name"]]
        else:
            command = ["omp", "plugin", "marketplace", "add", marketplace["source"]]
        runner.run(command, f"Reconcile marketplace {marketplace['name']}")
    for plugin in manifest["plugins"]:
        if mode == "update":
            command = ["omp", "plugin", "upgrade", "--scope", plugin.get("scope", "user"), plugin["id"]]
        else:
            command = ["omp", "plugin", "install", "--force", "--scope", plugin.get("scope", "user"), plugin["id"]]
        runner.run(command, f"Reconcile plugin {plugin['id']}")


def remove_marketplace(name: str, runner: runtime.Runner) -> None:
    registry = runtime.home_dir() / ".omp" / "marketplaces.json"
    if not runner.dry_run and not registry.exists():
        runner.say(f"{terminal.badge('OK')} Marketplace {name}: not registered in {registry}")
        return
    if not runner.dry_run and registry.exists():
        try:
            present = json_contains(manifests.load_json_object(registry), name)
        except (OSError, UnicodeError, runtime.DotAiError):
            present = True
        if not present:
            runner.say(f"{terminal.badge('OK')} Marketplace {name}: not registered in {registry}")
            return
    runner.run(["omp", "plugin", "marketplace", "remove", name], f"Remove marketplace {name}")


def remove_plugin(plugin: dict[str, Any], runner: runtime.Runner) -> None:
    scope = plugin.get("scope", "user")
    plugin_id = plugin["id"]
    registry = (
        runtime.ROOT / ".omp" / "plugins" / "installed_plugins.json"
        if scope == "project"
        else runtime.home_dir() / ".omp" / "plugins" / "installed_plugins.json"
    )
    if not runner.dry_run and not registry.exists():
        runner.say(f"{terminal.badge('OK')} Plugin {plugin_id}: not installed in {scope} scope")
        return
    if not runner.dry_run and registry.exists():
        try:
            present = json_contains(manifests.load_json_object(registry), plugin_id)
        except (OSError, UnicodeError, runtime.DotAiError):
            present = True
        if not present:
            runner.say(f"{terminal.badge('OK')} Plugin {plugin_id}: not installed in {scope} scope")
            return
    runner.run(["omp", "plugin", "uninstall", "--scope", scope, plugin_id], f"Uninstall plugin {plugin_id}")


def extension_identity(value: str) -> str:
    if value.startswith(("~/", "~\\")) or Path(value).is_absolute():
        return os.path.normcase(str(runtime.expand_path(value).resolve()))
    return value


def configured_omp_extensions(runner: runtime.Runner) -> list[str] | None:
    raw = runner.output(["omp", "config", "get", "extensions", "--json"])
    if not raw:
        return None
    try:
        value = json.loads(raw).get("value")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return value


def reconcile_omp_extensions(manifest: dict[str, Any], runner: runtime.Runner) -> None:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return
    current = configured_omp_extensions(runner)
    if current is None:
        if runner.dry_run:
            runner.say(f"{terminal.badge('RUN')} OMP extensions: configure {', '.join(desired)}")
            return
        runner.failures.append("OMP extensions: unable to read global OMP configuration")
        runner.say(f"{terminal.badge('FAIL')} OMP extensions: unable to read global OMP configuration")
        return
    known = {extension_identity(item) for item in current}
    additions = [item for item in desired if extension_identity(item) not in known]
    if not additions:
        runner.say(f"{terminal.badge('OK')} OMP extensions: all managed extensions are configured")
        return
    merged = [*current, *additions]
    if runner.dry_run:
        runner.say(f"{terminal.badge('RUN')} OMP extensions: add {', '.join(additions)}")
        return
    runner.run(
        ["omp", "config", "set", "extensions", json.dumps(merged, separators=(",", ":"))],
        "Configure OMP extensions",
    )


def omp_extension_status(manifest: dict[str, Any], runner: runtime.Runner) -> tuple[bool, str]:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return True, "no managed extensions"
    current = configured_omp_extensions(runner)
    if current is None:
        return False, "unable to read global OMP configuration"
    configured = {extension_identity(item) for item in current}
    missing_config = [item for item in desired if extension_identity(item) not in configured]
    missing_files = [item for item in desired if not runtime.expand_path(item).exists()]
    if missing_config:
        return False, f"not configured: {', '.join(missing_config)}"
    if missing_files:
        return False, f"source missing: {', '.join(missing_files)}"
    return True, f"{len(desired)} managed extension(s) configured and available"


def json_contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return needle in value or any(json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(json_contains(item, needle) for item in value)
    return value == needle


def registry_contains(path: Path, needle: str) -> bool:
    try:
        return json_contains(manifests.load_json_object(path), needle)
    except (OSError, UnicodeError, runtime.DotAiError):
        return False


_SKILL_ENV_BEGIN = b"# BEGIN DotAi skill environment"
_SKILL_ENV_END = b"# END DotAi skill environment"


def _read_skill_environment(target: Path) -> tuple[bytes, bytes, dict[str, str]]:
    if target.is_symlink():
        raise runtime.DotAiError(f"Refusing to modify symlinked OMP environment at {target}")
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        return b"", b"", {}
    if _SKILL_ENV_BEGIN not in current and _SKILL_ENV_END not in current:
        return current, current, {}
    lines = current.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == _SKILL_ENV_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == _SKILL_ENV_END]
    if (
        current.count(_SKILL_ENV_BEGIN) != 1 or current.count(_SKILL_ENV_END) != 1
        or len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]
        or ends[0] != len(lines) - 1
    ):
        raise runtime.DotAiError(f"Refusing malformed or nonterminal DotAi environment block at {target}")
    managed = {}
    for line in lines[starts[0] + 1:ends[0]]:
        match = re.fullmatch(rb"([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:-]+)", line.rstrip(b"\r\n"))
        if match is None or match[1].decode("ascii") in managed:
            raise runtime.DotAiError(f"Refusing unsafe DotAi environment block at {target}")
        managed[match[1].decode("ascii")] = match[2].decode("ascii")
    return current, b"".join(lines[:starts[0]]), managed



def validate_skill_environment_target(runner: runtime.Runner) -> bool:
    target = runtime.home_dir() / ".omp" / "agent" / ".env"
    try:
        _read_skill_environment(target)
    except (OSError, UnicodeError, runtime.DotAiError) as exc:
        detail = f"OMP skill environment: {exc}"
        runner.failures.append(detail)
        runner.say(f"{terminal.badge('FAIL')} {detail}")
        return False
    return True

def reconcile_skill_environment(
    environment: dict[str, str], runner: runtime.Runner, *, preserve_existing: bool = False,
) -> bool:
    """Rebuild only DotAi's terminal block; unmanaged dotenv bytes remain opaque."""
    target = runtime.home_dir() / ".omp" / "agent" / ".env"
    try:
        current, prefix, managed = _read_skill_environment(target)
        if preserve_existing:
            environment = {**managed, **environment}
        payload = prefix
        if environment:
            if payload and not payload.endswith(b"\n"):
                payload += b"\n"
            payload += _SKILL_ENV_BEGIN + b"\n"
            payload += "".join(f"{name}={value}\n" for name, value in sorted(environment.items())).encode("ascii")
            payload += _SKILL_ENV_END + b"\n"
        if payload == current:
            return True
        if runner.dry_run:
            runner.say(f"{terminal.badge('RUN')} OMP skill environment: reconcile managed defaults at {target} (dry run)")
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = manifests.backup_manifest(target)
            runner.say(f"{terminal.badge('OK')} OMP skill environment: backup written to {backup}")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
            os.replace(temp_path, target)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
    except (OSError, UnicodeError, runtime.DotAiError) as exc:
        detail = f"OMP skill environment: {exc}"
        runner.failures.append(detail)
        runner.say(f"{terminal.badge('FAIL')} {detail}")
        return False
    runner.say(f"{terminal.badge('OK')} OMP skill environment: synchronized {target}")
    return True


def skill_environment_status(environment: dict[str, str]) -> tuple[bool, str]:
    if not environment:
        return True, "no managed OMP environment defaults"
    target = runtime.home_dir() / ".omp" / "agent" / ".env"
    try:
        _, _, managed = _read_skill_environment(target)
    except (OSError, UnicodeError, runtime.DotAiError) as exc:
        return False, f"OMP skill environment: {exc}"
    missing = [name for name, value in environment.items() if managed.get(name) != value]
    if missing:
        return False, f"OMP environment defaults missing or changed: {', '.join(sorted(missing))}"
    return True, "OMP environment defaults configured"
