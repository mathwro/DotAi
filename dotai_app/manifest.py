"""Manifest validation, initialization, and safe JSON persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import datetime as dt
import difflib
import json
import os
import re
import shutil
import tempfile
from . import runtime
from . import terminal


DEFAULT_MANIFEST = runtime.ROOT / "stack.json"


EXAMPLE_MANIFEST = runtime.ROOT / "stack.example.json"


SUPPORTED_ROUTING_PROVIDERS = frozenset({"github-copilot", "openai-codex", "anthropic"})


SERVER_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")


PLUGIN_ID = re.compile(r"[a-z0-9.-]+@[a-z0-9.-]+")


PLATFORMS = frozenset({"windows", "wsl", "ubuntu", "arch", "macos", "linux", "unix", "default"})


VERSION_MINIMUM_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$")


def initialize_manifest(path: Path, *, allow_custom: bool = False) -> bool:
    if path.exists() or (not allow_custom and path.resolve() != DEFAULT_MANIFEST.resolve()):
        return False
    try:
        payload = EXAMPLE_MANIFEST.read_text(encoding="utf-8")
        data = json.loads(payload)
    except FileNotFoundError as exc:
        raise runtime.DotAiError(f"Example manifest not found: {EXAMPLE_MANIFEST}") from exc
    except json.JSONDecodeError as exc:
        raise runtime.DotAiError(f"Invalid JSON in {EXAMPLE_MANIFEST}: {exc}") from exc
    except UnicodeError as exc:
        raise runtime.DotAiError(f"Manifest is not UTF-8: {EXAMPLE_MANIFEST}: {exc}") from exc
    validate_manifest(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    print(f"{terminal.badge('OK')} Initialized {path} from {EXAMPLE_MANIFEST.name}")
    return True


def initialize_default_manifest(path: Path) -> bool:
    return initialize_manifest(path)


def validate_omp_routing(value: Any, *, allow_legacy: bool = False) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise runtime.DotAiError("Manifest 'ompRouting' must be an object")

    if "roles" in value:
        if not allow_legacy:
            raise runtime.DotAiError("Manifest 'ompRouting.roles' is obsolete; run 'dotai configure omp-routing' to migrate")
        allowed = {
            "roles",
            "agentModelOverrides",
            "usageReservePct",
            "usageReservePolicy",
            "fallbackRevertPolicy",
        }
        if set(value) - allowed:
            raise runtime.DotAiError("Manifest 'ompRouting' has unsupported properties")
        roles = value["roles"]
        if not isinstance(roles, dict) or not roles:
            raise runtime.DotAiError("Manifest 'ompRouting.roles' must be a non-empty object")
        for role, selectors in roles.items():
            if not isinstance(role, str) or not role:
                raise runtime.DotAiError("Manifest 'ompRouting' role names must be non-empty strings")
            if not isinstance(selectors, list) or not selectors:
                raise runtime.DotAiError("Manifest 'ompRouting' role selectors must be non-empty arrays")
            if any(not isinstance(selector, str) or not selector for selector in selectors):
                raise runtime.DotAiError("Manifest 'ompRouting' selectors must be non-empty strings")
        normalized = {"roles": roles}
    else:
        allowed = {
            "providers",
            "primaryProvider",
            "agentModelOverrides",
            "usageReservePct",
            "usageReservePolicy",
            "fallbackRevertPolicy",
        }
        if set(value) - allowed:
            raise runtime.DotAiError("Manifest 'ompRouting' has unsupported properties")
        providers = value.get("providers")
        if not isinstance(providers, list) or not providers:
            raise runtime.DotAiError("Manifest 'ompRouting.providers' must be a non-empty array")
        if any(not isinstance(provider, str) or not provider for provider in providers):
            raise runtime.DotAiError("Manifest 'ompRouting.providers' entries must be non-empty strings")
        if len(providers) != len(set(providers)):
            raise runtime.DotAiError("Manifest 'ompRouting.providers' entries must be unique")
        if any(provider not in SUPPORTED_ROUTING_PROVIDERS for provider in providers):
            raise runtime.DotAiError("Manifest 'ompRouting.providers' contains an unsupported provider")
        primary = value.get("primaryProvider")
        if primary not in providers:
            raise runtime.DotAiError("Manifest 'ompRouting.primaryProvider' must be present in providers")
        normalized = {"providers": providers, "primaryProvider": primary}

    overrides = value.get("agentModelOverrides", {})
    if not isinstance(overrides, dict) or any(
        not isinstance(agent, str) or not agent or not isinstance(model, str) or not model
        for agent, model in overrides.items()
    ):
        raise runtime.DotAiError("Manifest 'ompRouting.agentModelOverrides' must map non-empty strings")

    reserve = value.get("usageReservePct", 10)
    if isinstance(reserve, bool) or not isinstance(reserve, int) or not 0 <= reserve <= 100:
        raise runtime.DotAiError("Manifest 'ompRouting.usageReservePct' must be an integer from 0 to 100")
    reserve_policy = value.get("usageReservePolicy", "auto")
    if not isinstance(reserve_policy, str) or reserve_policy not in {"confirm", "auto", "fail-closed"}:
        raise runtime.DotAiError("Manifest 'ompRouting.usageReservePolicy' is invalid")
    revert_policy = value.get("fallbackRevertPolicy", "cooldown-expiry")
    if not isinstance(revert_policy, str) or revert_policy not in {"cooldown-expiry", "never"}:
        raise runtime.DotAiError("Manifest 'ompRouting.fallbackRevertPolicy' is invalid")

    return {
        **normalized,
        "agentModelOverrides": overrides,
        "usageReservePct": reserve,
        "usageReservePolicy": reserve_policy,
        "fallbackRevertPolicy": revert_policy,
    }


def require_nonempty_string(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value:
        raise runtime.DotAiError(f"Manifest '{path}' must be a non-empty string")


def validate_command(value: Any, path: str) -> None:
    if isinstance(value, str):
        require_nonempty_string(value, path)
    elif not isinstance(value, list) or not value or any(not isinstance(part, str) for part in value):
        raise runtime.DotAiError(f"Manifest '{path}' must be a non-empty command string or array of strings")
    else:
        require_nonempty_string(value[0], f"{path}[0]")


def validate_platform_commands(value: Any, path: str, *, check: bool = False) -> None:
    def validate_entry(commands: Any, entry_path: str) -> None:
        if check:
            validate_command(commands, entry_path)
        else:
            if not isinstance(commands, list):
                raise runtime.DotAiError(f"Manifest '{entry_path}' must be an array of commands")
            for index, command in enumerate(commands):
                validate_command(command, f"{entry_path}[{index}]")

    if isinstance(value, dict):
        for platform, commands in value.items():
            if platform not in PLATFORMS:
                raise runtime.DotAiError(f"Manifest '{path}' has an unsupported platform: {platform}")
            validate_entry(commands, f"{path}.{platform}")
    else:
        validate_entry(value, path)


def validate_mcp_server(server: Any, path: str) -> None:
    if not isinstance(server, dict):
        raise runtime.DotAiError(f"Manifest '{path}' must be an object")
    transport = server.get("type", "stdio")
    if not isinstance(transport, str):
        raise runtime.DotAiError(f"Manifest '{path}.type' must be 'stdio', 'http', or 'sse'")
    if transport == "stdio":
        require_nonempty_string(server.get("command"), f"{path}.command")
        if "args" in server and (
            not isinstance(server["args"], list)
            or any(not isinstance(arg, str) for arg in server["args"])
        ):
            raise runtime.DotAiError(f"Manifest '{path}.args' must be an array of strings")
        if "env" in server and (
            not isinstance(server["env"], dict)
            or any(not isinstance(val, str) for val in server["env"].values())
        ):
            raise runtime.DotAiError(f"Manifest '{path}.env' must map names to strings")
        if "cwd" in server and not isinstance(server["cwd"], str):
            raise runtime.DotAiError(f"Manifest '{path}.cwd' must be a string")
    elif transport in {"http", "sse"}:
        url = server.get("url")
        if not isinstance(url, str) or any(char.isspace() for char in url):
            raise runtime.DotAiError(f"Manifest '{path}.url' must be an HTTP(S) URL")
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # Accessing the port validates its syntax and range.
            valid = parsed.scheme in {"http", "https"} and parsed.hostname is not None
        except ValueError:
            valid = False
        if not valid:
            raise runtime.DotAiError(f"Manifest '{path}.url' must be an HTTP(S) URL")
        if "headers" in server and (
            not isinstance(server["headers"], dict)
            or any(not isinstance(val, str) for val in server["headers"].values())
        ):
            raise runtime.DotAiError(f"Manifest '{path}.headers' must map names to strings")
    else:
        raise runtime.DotAiError(f"Manifest '{path}.type' must be 'stdio', 'http', or 'sse'")
    if "enabled" in server and not isinstance(server["enabled"], bool):
        raise runtime.DotAiError(f"Manifest '{path}.enabled' must be a boolean")
    if "timeout" in server and (
        isinstance(server["timeout"], bool)
        or not isinstance(server["timeout"], int)
        or server["timeout"] < 0
    ):
        raise runtime.DotAiError(f"Manifest '{path}.timeout' must be a non-negative integer")


def load_manifest(path: Path, *, allow_legacy_routing: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise runtime.DotAiError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise runtime.DotAiError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise runtime.DotAiError(f"Unable to read manifest {path}: {exc}") from exc
    except UnicodeError as exc:
        raise runtime.DotAiError(f"Manifest is not UTF-8: {path}: {exc}") from exc
    return validate_manifest(data, allow_legacy_routing=allow_legacy_routing)


def validate_manifest(data: Any, *, allow_legacy_routing: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise runtime.DotAiError("Manifest root must be an object")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise runtime.DotAiError("Manifest 'version' must be 1")
    for section in ("packages", "skills", "marketplaces", "plugins"):
        if not isinstance(data.get(section), list):
            raise runtime.DotAiError(f"Manifest '{section}' must be an array")
    for index, package in enumerate(data["packages"]):
        path_name = f"packages[{index}]"
        if not isinstance(package, dict):
            raise runtime.DotAiError(f"Manifest '{path_name}' must be an object")
        require_nonempty_string(package.get("name"), f"{path_name}.name")
        if package.get("updateGroup", "core") not in ("core", "dependency"):
            raise runtime.DotAiError(f"Manifest '{path_name}.updateGroup' must be 'core' or 'dependency'")
        for field in ("check", "install"):
            if field not in package:
                raise runtime.DotAiError(f"Manifest '{path_name}.{field}' is required")
        validate_platform_commands(package["check"], f"{path_name}.check", check=True)
        for field in ("install", "update", "configure"):
            if field in package:
                validate_platform_commands(package[field], f"{path_name}.{field}")
        minimum_version = package.get("minimumVersion")
        if "minimumVersion" in package and (
            not isinstance(minimum_version, str) or not VERSION_MINIMUM_PATTERN.fullmatch(minimum_version)
        ):
            raise runtime.DotAiError("Manifest package 'minimumVersion' must be a major.minor or major.minor.patch version")
    for index, skill in enumerate(data["skills"]):
        path_name = f"skills[{index}]"
        if not isinstance(skill, dict):
            raise runtime.DotAiError(f"Manifest '{path_name}' must be an object")
        require_nonempty_string(skill.get("source"), f"{path_name}.source")
        if "agent" in skill and not isinstance(skill["agent"], str):
            raise runtime.DotAiError(f"Manifest '{path_name}.agent' must be a string")
        for field in ("skills", "checkSkills"):
            if field in skill and (
                not isinstance(skill[field], list)
                or any(not isinstance(item, str) for item in skill[field])
            ):
                raise runtime.DotAiError(f"Manifest '{path_name}.{field}' must be an array of strings")
    for index, marketplace in enumerate(data["marketplaces"]):
        path_name = f"marketplaces[{index}]"
        if not isinstance(marketplace, dict):
            raise runtime.DotAiError(f"Manifest '{path_name}' must be an object")
        for field in ("name", "source"):
            require_nonempty_string(marketplace.get(field), f"{path_name}.{field}")
    for index, plugin in enumerate(data["plugins"]):
        path_name = f"plugins[{index}]"
        if not isinstance(plugin, dict):
            raise runtime.DotAiError(f"Manifest '{path_name}' must be an object")
        if not isinstance(plugin.get("id"), str) or not PLUGIN_ID.fullmatch(plugin["id"]):
            raise runtime.DotAiError(f"Manifest '{path_name}.id' must be a plugin@marketplace ID")
        if "scope" in plugin and plugin["scope"] not in ("user", "project"):
            raise runtime.DotAiError(f"Manifest '{path_name}.scope' must be 'user' or 'project'")
    extensions = data.get("ompExtensions", [])
    if not isinstance(extensions, list) or any(not isinstance(item, str) or not item for item in extensions):
        raise runtime.DotAiError("Manifest 'ompExtensions' must be an array of non-empty strings")
    if len(extensions) != len(set(extensions)):
        raise runtime.DotAiError("Manifest 'ompExtensions' entries must be unique")
    if "ompRouting" in data:
        data = {
            **data,
            "ompRouting": validate_omp_routing(
                data["ompRouting"], allow_legacy=allow_legacy_routing
            ),
        }
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        raise runtime.DotAiError("Manifest 'mcp' must be an object")
    require_nonempty_string(mcp.get("target"), "mcp.target")
    servers = mcp.get("servers")
    if not isinstance(servers, dict):
        raise runtime.DotAiError("Manifest 'mcp.servers' must be an object")
    for name, server in servers.items():
        if not SERVER_NAME.fullmatch(name):
            raise runtime.DotAiError(f"Invalid MCP server name: {name}")
        validate_mcp_server(server, f"mcp.servers.{name}")
    return data


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise runtime.DotAiError(f"Refusing to modify invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise runtime.DotAiError(f"Refusing to modify non-object JSON at {path}")
    return value


def manifest_diff(before: dict[str, Any], after: dict[str, Any], path: Path) -> str:
    old = json.dumps(before, indent=2).splitlines()
    new = json.dumps(after, indent=2).splitlines()
    return "\n".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=str(path),
            tofile=f"{path} (proposed)",
            lineterm="",
        )
    )


def backup_manifest(path: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    with path.open("rb") as source:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination)
        except Exception:
            backup.unlink(missing_ok=True)
            raise
    return backup


def write_manifest(
    path: Path, manifest: dict[str, Any], *, backup: bool = False
) -> Path | None:
    # The loader represents unconfigured routing as {}; persist its schema form.
    if manifest.get("ompRouting") == {}:
        manifest = {**manifest, "ompRouting": None}
    validate_manifest(manifest)
    payload = json.dumps(manifest, indent=2) + "\n"
    backup_path = backup_manifest(path) if backup else None
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return backup_path
