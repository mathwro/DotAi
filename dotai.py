#!/usr/bin/env python3
"""Declarative, cross-platform manager for this AI development stack."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

VERSION = "0.2.0"
ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "stack.json"
EXAMPLE_MANIFEST = ROOT / "stack.example.json"
ROUTING_RECOMMENDATIONS = ROOT / "routing-recommendations.json"
ROUTING_ROLES = ("default", "task", "smol", "slow")
SUPPORTED_ROUTING_PROVIDERS = frozenset({"github-copilot", "openai-codex", "anthropic"})
DEFAULT_AGENT_MODEL_OVERRIDES = {"sonic": "@smol", "task": "@task"}
SERVER_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RELEASE_URL = "https://api.github.com/repos/mathwro/DotAi/releases/latest"
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
THINKING_SUFFIXES = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "auto"})
PLACEHOLDER = re.compile(r"\{(home|repo|python)\}")

_COLOR_ENABLED = False
_ANSI = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "cyan": "36",
}


def configure_color(mode: str) -> None:
    global _COLOR_ENABLED
    if mode == "always":
        _COLOR_ENABLED = True
    elif mode == "never":
        _COLOR_ENABLED = False
    else:
        forced = os.environ.get("FORCE_COLOR")
        _COLOR_ENABLED = (
            "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and ((forced is not None and forced != "0") or sys.stdout.isatty())
        )
    if _COLOR_ENABLED and os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode_value = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode_value)):
                kernel32.SetConsoleMode(handle, mode_value.value | 0x0004)
        except (AttributeError, OSError):
            pass


def styled(text: str, *styles: str) -> str:
    if not _COLOR_ENABLED or not styles:
        return text
    codes = ";".join(_ANSI[style] for style in styles)
    return f"\033[{codes}m{text}\033[0m"


def badge(label: str) -> str:
    color = {
        "OK": "green",
        "RUN": "cyan",
        "INACTIVE": "yellow",
        "DRIFT": "yellow",
        "UPDATE": "yellow",
        "MISSING": "red",
        "FAIL": "red",
    }.get(label, "blue")
    return styled(f"[{label}]", color, "bold")


def latest_release_version() -> str | None:
    try:
        request = urlrequest.Request(
            RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"DotAi/{VERSION}"},
        )
        with urlrequest.urlopen(request, timeout=2) as response:
            tag = json.loads(response.read().decode("utf-8")).get("tag_name")
    except urlerror.HTTPError as exc:
        exc.close()
        return None
    except (AttributeError, OSError, TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(tag, str):
        return None
    match = VERSION_PATTERN.fullmatch(tag)
    return ".".join(match.groups()) if match else None


def print_release_notice() -> None:
    latest = latest_release_version()
    current = VERSION_PATTERN.fullmatch(VERSION)
    available = VERSION_PATTERN.fullmatch(latest or "")
    if not current or not available or tuple(map(int, available.groups())) <= tuple(map(int, current.groups())):
        return
    print(f"{badge('UPDATE')} DotAi {latest} is available (current: {VERSION}); pull the repository to update.")


def heading(text: str) -> str:
    return styled(text, "blue", "bold")


class DotAiError(RuntimeError):
    pass


def home_dir() -> Path:
    return Path(os.environ.get("DOTAI_HOME", Path.home())).expanduser().resolve()


def state_dir() -> Path:
    override = os.environ.get("DOTAI_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", home_dir() / "AppData" / "Local"))
        return base / "DotAi"
    return Path(os.environ.get("XDG_STATE_HOME", home_dir() / ".local" / "state")) / "dotai"


def expand_path(value: str) -> Path:
    if value == "~":
        return home_dir()
    if value.startswith("~/") or value.startswith("~\\"):
        return home_dir() / value[2:]
    return Path(os.path.expandvars(value)).expanduser()


def detect_platform() -> str:
    override = os.environ.get("DOTAI_PLATFORM")
    if override:
        return override
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    proc_version = Path("/proc/version")
    if proc_version.exists() and "microsoft" in proc_version.read_text(errors="ignore").lower():
        return "wsl"
    if Path("/etc/arch-release").exists():
        return "arch"
    release = Path("/etc/os-release")
    text = release.read_text(errors="ignore").lower() if release.exists() else ""
    if "ubuntu" in text or "debian" in text:
        return "ubuntu"
    return "linux"


def platform_keys(name: str) -> list[str]:
    aliases = {
        "wsl": ["wsl", "ubuntu", "linux", "default"],
        "ubuntu": ["ubuntu", "linux", "default"],
        "arch": ["arch", "linux", "default"],
        "macos": ["macos", "unix", "default"],
        "windows": ["windows", "default"],
        "linux": ["linux", "default"],
    }
    return aliases.get(name, [name, "default"])


def initialize_manifest(path: Path, *, allow_custom: bool = False) -> bool:
    if path.exists() or (not allow_custom and path.resolve() != DEFAULT_MANIFEST.resolve()):
        return False
    try:
        payload = EXAMPLE_MANIFEST.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DotAiError(f"Example manifest not found: {EXAMPLE_MANIFEST}") from exc
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
    print(f"{badge('OK')} Initialized {path} from {EXAMPLE_MANIFEST.name}")
    return True


def initialize_default_manifest(path: Path) -> bool:
    return initialize_manifest(path)


def validate_routing_recommendations(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "agentModelOverrides", "providers"}:
        raise DotAiError("Routing recommendations must have exactly version, agentModelOverrides, and providers")
    if isinstance(value["version"], bool) or value["version"] != 1:
        raise DotAiError("Routing recommendations 'version' must be 1")

    overrides = value["agentModelOverrides"]
    if overrides != DEFAULT_AGENT_MODEL_OVERRIDES:
        raise DotAiError("Routing recommendation agentModelOverrides must match the managed defaults")

    providers = value["providers"]
    if not isinstance(providers, dict) or set(providers) != SUPPORTED_ROUTING_PROVIDERS:
        raise DotAiError("Routing recommendations must define exactly the supported providers")
    for provider, settings in providers.items():
        if not isinstance(provider, str) or not provider:
            raise DotAiError("Routing recommendation provider names must be non-empty strings")
        if not isinstance(settings, dict) or set(settings) != {"roles"}:
            raise DotAiError("Routing recommendation providers must contain exactly roles")
        roles = settings["roles"]
        if not isinstance(roles, dict) or set(roles) != set(ROUTING_ROLES):
            raise DotAiError("Routing recommendation providers must define exactly the managed roles")
        for selectors in roles.values():
            if not isinstance(selectors, list) or not selectors:
                raise DotAiError("Routing recommendation roles must be non-empty arrays")
            if any(
                not isinstance(selector, str)
                or not selector
                or not selector.startswith(f"{provider}/")
                for selector in selectors
            ):
                raise DotAiError("Routing recommendation selectors must be non-empty and match their provider")
    return value


def load_routing_recommendations(
    path: Path = ROUTING_RECOMMENDATIONS,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DotAiError(f"Unable to read routing recommendations from {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DotAiError(f"Invalid JSON in {path}: {exc}") from exc
    return validate_routing_recommendations(value)


def validate_omp_routing(value: Any, *, allow_legacy: bool = False) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DotAiError("Manifest 'ompRouting' must be an object")

    if "roles" in value:
        if not allow_legacy:
            raise DotAiError("Manifest 'ompRouting.roles' is obsolete; run 'dotai configure omp-routing' to migrate")
        allowed = {
            "roles",
            "agentModelOverrides",
            "usageReservePct",
            "usageReservePolicy",
            "fallbackRevertPolicy",
        }
        if set(value) - allowed:
            raise DotAiError("Manifest 'ompRouting' has unsupported properties")
        roles = value["roles"]
        if not isinstance(roles, dict) or not roles:
            raise DotAiError("Manifest 'ompRouting.roles' must be a non-empty object")
        for role, selectors in roles.items():
            if not isinstance(role, str) or not role:
                raise DotAiError("Manifest 'ompRouting' role names must be non-empty strings")
            if not isinstance(selectors, list) or not selectors:
                raise DotAiError("Manifest 'ompRouting' role selectors must be non-empty arrays")
            if any(not isinstance(selector, str) or not selector for selector in selectors):
                raise DotAiError("Manifest 'ompRouting' selectors must be non-empty strings")
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
            raise DotAiError("Manifest 'ompRouting' has unsupported properties")
        providers = value.get("providers")
        if not isinstance(providers, list) or not providers:
            raise DotAiError("Manifest 'ompRouting.providers' must be a non-empty array")
        if any(not isinstance(provider, str) or not provider for provider in providers):
            raise DotAiError("Manifest 'ompRouting.providers' entries must be non-empty strings")
        if len(providers) != len(set(providers)):
            raise DotAiError("Manifest 'ompRouting.providers' entries must be unique")
        if any(provider not in SUPPORTED_ROUTING_PROVIDERS for provider in providers):
            raise DotAiError("Manifest 'ompRouting.providers' contains an unsupported provider")
        primary = value.get("primaryProvider")
        if primary not in providers:
            raise DotAiError("Manifest 'ompRouting.primaryProvider' must be present in providers")
        normalized = {"providers": providers, "primaryProvider": primary}

    overrides = value.get("agentModelOverrides", {})
    if not isinstance(overrides, dict) or any(
        not isinstance(agent, str) or not agent or not isinstance(model, str) or not model
        for agent, model in overrides.items()
    ):
        raise DotAiError("Manifest 'ompRouting.agentModelOverrides' must map non-empty strings")

    reserve = value.get("usageReservePct", 10)
    if isinstance(reserve, bool) or not isinstance(reserve, int) or not 0 <= reserve <= 100:
        raise DotAiError("Manifest 'ompRouting.usageReservePct' must be an integer from 0 to 100")
    reserve_policy = value.get("usageReservePolicy", "auto")
    if not isinstance(reserve_policy, str) or reserve_policy not in {"confirm", "auto", "fail-closed"}:
        raise DotAiError("Manifest 'ompRouting.usageReservePolicy' is invalid")
    revert_policy = value.get("fallbackRevertPolicy", "cooldown-expiry")
    if not isinstance(revert_policy, str) or revert_policy not in {"cooldown-expiry", "never"}:
        raise DotAiError("Manifest 'ompRouting.fallbackRevertPolicy' is invalid")

    return {
        **normalized,
        "agentModelOverrides": overrides,
        "usageReservePct": reserve,
        "usageReservePolicy": reserve_policy,
        "fallbackRevertPolicy": revert_policy,
    }


def load_manifest(path: Path, *, allow_legacy_routing: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DotAiError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DotAiError(f"Invalid JSON in {path}: {exc}") from exc
    if data.get("version") != 1:
        raise DotAiError("Manifest 'version' must be 1")
    for section in ("packages", "skills", "marketplaces", "plugins"):
        if not isinstance(data.get(section, []), list):
            raise DotAiError(f"Manifest '{section}' must be an array")
    for package in data.get("packages", []):
        if not isinstance(package, dict):
            raise DotAiError("Manifest 'packages' entries must be objects")
        if package.get("updateGroup", "core") not in {"core", "dependency"}:
            raise DotAiError("Manifest package 'updateGroup' must be 'core' or 'dependency'")
    extensions = data.get("ompExtensions", [])
    if not isinstance(extensions, list) or any(not isinstance(item, str) or not item for item in extensions):
        raise DotAiError("Manifest 'ompExtensions' must be an array of non-empty strings")
    if "ompRouting" in data:
        data["ompRouting"] = validate_omp_routing(
            data["ompRouting"], allow_legacy=allow_legacy_routing
        )
    mcp = data.get("mcp", {})
    if not isinstance(mcp, dict) or not isinstance(mcp.get("servers", {}), dict):
        raise DotAiError("Manifest 'mcp.servers' must be an object")
    for name in mcp.get("servers", {}):
        if not SERVER_NAME.fullmatch(name):
            raise DotAiError(f"Invalid MCP server name: {name}")
    return data


def selected(value: Any, platform_name: str) -> Any:
    if not isinstance(value, dict):
        return value
    for key in platform_keys(platform_name):
        if key in value:
            return value[key]
    return []


class Runner:
    def __init__(self, platform_name: str, dry_run: bool = False, verbose: bool = False):
        self.platform = platform_name
        self.dry_run = dry_run
        self.verbose = verbose
        self.failures: list[str] = []
        self.env = os.environ.copy()
        candidates = [
            home_dir() / ".local" / "bin",
            home_dir() / ".cargo" / "bin",
            home_dir() / ".bun" / "bin",
            Path(os.environ.get("SCOOP", home_dir() / "scoop")) / "shims",
            Path(os.environ.get("APPDATA", "")) / "npm",
        ]
        self.env["PATH"] = os.pathsep.join(str(path) for path in candidates if str(path) != ".") + os.pathsep + self.env.get("PATH", "")

    def _format(self, value: str) -> str:
        values = {"home": str(home_dir()), "repo": str(ROOT), "python": sys.executable}
        return PLACEHOLDER.sub(lambda match: values[match.group(1)], value)

    def argv(self, command: str | list[str]) -> list[str]:
        if isinstance(command, str):
            text = self._format(command)
            if self.platform == "windows":
                return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", text]
            return ["/bin/sh", "-c", text]
        return [self._format(str(part)) for part in command]

    def display_command(self, command: str | list[str]) -> str:
        return command if isinstance(command, str) else " ".join(str(part) for part in command)

    def run(self, command: str | list[str], label: str, *, capture: bool = False, required: bool = True) -> subprocess.CompletedProcess[str] | None:
        print(f"{badge('RUN')} {label}: {self.display_command(command)}")
        if self.dry_run:
            return None
        try:
            result = subprocess.run(
                self.argv(command),
                env=self.env,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.STDOUT if capture else None,
                check=False,
            )
        except OSError as exc:
            if required:
                self.failures.append(f"{label}: {exc}")
            print(f"{badge('FAIL')} {label}: {exc}")
            return None
        if result.returncode != 0:
            detail = f" (exit {result.returncode})"
            if capture and result.stdout:
                detail += f": {result.stdout.strip()}"
            if required:
                self.failures.append(label + detail)
            print(f"{badge('FAIL')} {label}{detail}")
        elif self.verbose and capture and result.stdout:
            print(result.stdout.rstrip())
        if self.platform == "windows":
            self._refresh_windows_path()
        return result

    def _refresh_windows_path(self) -> None:
        script = (
            "[Environment]::GetEnvironmentVariable('Path','Machine')+';'+"
            "[Environment]::GetEnvironmentVariable('Path','User')"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                self.env["PATH"] = result.stdout.strip() + os.pathsep + self.env["PATH"]
        except OSError:
            pass

    def succeeds(self, command: str | list[str]) -> bool:
        try:
            return subprocess.run(
                self.argv(command), env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            ).returncode == 0
        except OSError:
            return False

    def output(self, command: str | list[str]) -> str:
        try:
            result = subprocess.run(
                self.argv(command), env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except OSError:
            return ""




def run_steps(steps: Any, runner: Runner, label: str) -> None:
    for index, step in enumerate(steps or [], start=1):
        step_label = f"{label} ({index}/{len(steps)})"
        if isinstance(step, (str, list)):
            runner.run(step, step_label)
        else:
            runner.failures.append(f"{step_label}: invalid command entry")


def package_check(package: dict[str, Any], runner: Runner) -> bool:
    command = selected(package.get("check", []), runner.platform)
    return bool(command) and runner.succeeds(command)


def reconcile_packages(
    manifest: dict[str, Any],
    runner: Runner,
    mode: str,
    force: bool = False,
    include_dependencies: bool = False,
) -> None:
    for package in manifest["packages"]:
        name = package["name"]
        installed = package_check(package, runner)
        if (
            mode == "update"
            and installed
            and package.get("updateGroup", "core") == "dependency"
            and not include_dependencies
        ):
            print(f"{badge('OK')} {name}: dependency update skipped (use --include-dependencies)")
        elif mode == "install" and installed and not force:
            print(f"{badge('OK')} {name}: already installed")
        else:
            operation = "install" if mode == "install" or not installed else "update"
            steps = selected(package.get(operation, package.get("install", {})), runner.platform)
            if not steps and operation == "update" and installed:
                print(f"{badge('OK')} {name}: no managed update required")
            elif not steps:
                runner.failures.append(f"{name}: no {operation} commands for {runner.platform}")
                print(f"{badge('FAIL')} {name}: unsupported platform {runner.platform}")
                continue
            else:
                label = f"Check/update {name}" if operation == "update" else f"Install {name}"
                run_steps(steps, runner, label)
        if package.get("configure"):
            run_steps(selected(package["configure"], runner.platform), runner, f"Configure {name}")
        if not runner.dry_run and not package_check(package, runner):
            runner.failures.append(f"{name}: verification command failed after reconciliation")
            print(f"{badge('FAIL')} {name}: verification failed")


def skill_command(skill: dict[str, Any]) -> list[str]:
    command = [
        "npx",
        "--yes",
        "skills@latest",
        "add",
        skill["source"],
        "--global",
        "--agent",
        skill.get("agent", "universal"),
    ]
    wanted = skill.get("skills", ["*"])
    for name in wanted:
        command.extend(["--skill", name])
    command.extend(["--copy", "--yes"])
    return command


def reconcile_skills(manifest: dict[str, Any], runner: Runner) -> None:
    for skill in manifest["skills"]:
        runner.run(skill_command(skill), f"Reconcile skills from {skill['source']}")


def reconcile_plugins(manifest: dict[str, Any], runner: Runner, mode: str) -> None:
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


def extension_identity(value: str) -> str:
    if value.startswith(("~/", "~\\")) or Path(value).is_absolute():
        return os.path.normcase(str(expand_path(value).resolve()))
    return value


def selector_identity(selector: str) -> str:
    base, separator, suffix = selector.rpartition(":")
    return base if separator and suffix in THINKING_SUFFIXES else selector


def available_omp_models(runner: Runner) -> set[str] | None:
    raw = runner.output(["omp", "models", "--json"])
    try:
        catalog = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        return None
    models = catalog["models"]
    if any(not isinstance(model, dict) or not isinstance(model.get("selector"), str) or not model["selector"] for model in models):
        return None
    return {model["selector"] for model in models}


def detected_routing_providers(
    recommendations: dict[str, Any], available: set[str]
) -> list[str]:
    available_identities = {selector_identity(selector) for selector in available}
    supported = recommendations["providers"]
    providers = sorted(
        {selector.partition("/")[0] for selector in available} & set(supported)
    )
    for provider in providers:
        recommended = supported[provider]["roles"].values()
        if not any(
            selector_identity(selector) in available_identities
            for selectors in recommended
            for selector in selectors
        ):
            raise DotAiError(f"Provider {provider} has no recommended models available")
    return providers


def choose_primary_provider(
    providers: list[str], current: str | None, requested: str | None
) -> str:
    premium = [
        provider for provider in ("anthropic", "openai-codex") if provider in providers
    ]
    if requested is not None and requested not in premium:
        raise DotAiError(f"Requested --primary {requested} is not available")
    if not premium:
        if "github-copilot" in providers:
            return "github-copilot"
        raise DotAiError("No supported routing providers are available")
    if len(premium) == 1:
        return premium[0]
    if requested is not None:
        return requested
    if current in premium:
        return current
    if not sys.stdin.isatty():
        raise DotAiError(
            "Both Anthropic and OpenAI Codex are available; pass --primary"
        )
    try:
        response = input(
            "Choose interactive primary: [1] Anthropic [2] OpenAI Codex: "
        )
    except (EOFError, KeyboardInterrupt) as exc:
        raise DotAiError("Primary selection cancelled") from exc
    if response == "1":
        return "anthropic"
    if response == "2":
        return "openai-codex"
    raise DotAiError("Invalid primary selection; enter 1 or 2")


def resolve_omp_routing(
    recommendations: dict[str, Any],
    providers: list[str],
    primary: str,
    available: set[str],
) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
    interactive_order = [
        primary,
        *(
            provider
            for provider in ("anthropic", "openai-codex")
            if provider in providers and provider != primary
        ),
        *(
            provider
            for provider in ("github-copilot",)
            if provider in providers and provider != primary
        ),
    ]
    worker_order = [
        provider
        for provider in ("github-copilot", "anthropic", "openai-codex")
        if provider in providers
    ]
    available_identities = {selector_identity(selector) for selector in available}
    primaries: dict[str, str] = {}
    fallbacks: dict[str, list[str]] = {}
    unavailable: list[str] = []
    for role in ROUTING_ROLES:
        provider_order = (
            interactive_order if role in ("default", "slow") else worker_order
        )
        candidates = (
            selector
            for provider in provider_order
            for selector in recommendations["providers"][provider]["roles"][role]
        )
        retained: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if (
                candidate not in seen
                and selector_identity(candidate) in available_identities
            ):
                seen.add(candidate)
                retained.append(candidate)
        if retained:
            primaries[role] = retained[0]
            fallbacks[role] = retained
        else:
            unavailable.append(role)
    return primaries, fallbacks, unavailable


def configured_omp_value(runner: Runner, key: str) -> Any | None:
    raw = runner.output(["omp", "config", "get", key, "--json"])
    try:
        configured = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(configured, dict) or "value" not in configured:
        return None
    return configured["value"]


def build_omp_routing_intent(
    routing: dict[str, Any],
    recommendations: dict[str, Any],
    providers: list[str],
    primary: str,
) -> dict[str, Any]:
    return {
        "providers": sorted(providers),
        "primaryProvider": primary,
        "agentModelOverrides": routing.get(
            "agentModelOverrides", recommendations["agentModelOverrides"]
        ),
        "usageReservePct": routing.get("usageReservePct", 10),
        "usageReservePolicy": routing.get("usageReservePolicy", "auto"),
        "fallbackRevertPolicy": routing.get(
            "fallbackRevertPolicy", "cooldown-expiry"
        ),
    }


def omp_routing_scalar_values(routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "retry.modelFallback": True,
        "retry.usageAwareFallback": True,
        "retry.usageReservePct": routing["usageReservePct"],
        "retry.usageReservePolicy": routing["usageReservePolicy"],
        "retry.fallbackRevertPolicy": routing["fallbackRevertPolicy"],
    }


def configure_omp_routing(
    manifest: dict[str, Any],
    path: Path,
    runner: Runner,
    requested_primary: str | None = None,
) -> int:
    routing = manifest.get("ompRouting") or {}
    recommendations = load_routing_recommendations()
    available = available_omp_models(runner)
    if available is None:
        print(f"{badge('FAIL')} OMP routing: unable to read OMP model catalog")
        return 1

    providers = detected_routing_providers(recommendations, available)
    persisted_primary = routing.get("primaryProvider")
    primary = choose_primary_provider(providers, persisted_primary, requested_primary)
    primaries, fallbacks, unavailable = resolve_omp_routing(
        recommendations, providers, primary, available
    )
    if unavailable:
        print(f"{badge('FAIL')} OMP routing: unavailable managed roles: {', '.join(unavailable)}")
        return 1

    intent = build_omp_routing_intent(
        routing, recommendations, providers, primary
    )
    record_keys = (
        "modelRoles",
        "retry.fallbackChains",
        "task.agentModelOverrides",
    )
    scalar_values = omp_routing_scalar_values(intent)
    current = {
        key: configured_omp_value(runner, key)
        for key in (*record_keys, *scalar_values)
    }
    if any(
        not isinstance(current[key], dict)
        or any(not isinstance(name, str) for name in current[key])
        for key in record_keys
    ) or any(current[key] is None for key in scalar_values):
        print(f"{badge('FAIL')} OMP routing: unable to read required OMP configuration")
        return 1

    updated = dict(manifest)
    updated["ompRouting"] = intent
    desired_records = {
        "modelRoles": {**current["modelRoles"], **primaries},
        "retry.fallbackChains": {
            **current["retry.fallbackChains"],
            **fallbacks,
        },
        "task.agentModelOverrides": {
            **current["task.agentModelOverrides"],
            **intent["agentModelOverrides"],
        },
    }
    writes: list[tuple[str, str]] = []
    for key, value in desired_records.items():
        if current[key] != value:
            writes.append((key, json.dumps(value, separators=(",", ":"))))
    for key, value in scalar_values.items():
        if current[key] != value:
            serialized = str(value).lower() if isinstance(value, bool) else str(value)
            writes.append((key, serialized))

    print(f"{heading('OMP routing:')}")
    print(f"  discovered providers: {', '.join(providers)}")
    print(f"  interactive primary: {primary}")
    print(
        "  resolved role primaries: "
        + json.dumps(primaries, separators=(",", ":"))
    )
    print(
        "  fallback chains: "
        + json.dumps(fallbacks, separators=(",", ":"))
    )
    print("  manifest changes:")
    diff = manifest_diff(manifest, updated, path)
    print(diff or "    none")
    print("  pending OMP commands:")
    if writes:
        for key, value in writes:
            print(f"    omp config set {key} {value}")
    else:
        print("    none")

    if runner.dry_run:
        print(f"{badge('RUN')} Dry run: no manifest or OMP changes applied.")
        return 0

    manifest_changed = manifest != updated
    if manifest_changed:
        backup = backup_manifest(path)
        write_manifest(path, updated)
        print(f"{badge('OK')} Manifest backup written to {backup}")

    failures_before = len(runner.failures)
    for key, value in writes:
        runner.run(
            ["omp", "config", "set", key, value],
            f"Configure OMP routing {key}",
        )
    if not manifest_changed and not writes:
        print(f"{badge('OK')} OMP routing: already configured")
    return 1 if len(runner.failures) > failures_before else 0


def configured_omp_extensions(runner: Runner) -> list[str] | None:
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


def reconcile_omp_extensions(manifest: dict[str, Any], runner: Runner) -> None:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return
    current = configured_omp_extensions(runner)
    if current is None:
        if runner.dry_run:
            print(f"{badge('RUN')} OMP extensions: configure {', '.join(desired)}")
            return
        runner.failures.append("OMP extensions: unable to read global OMP configuration")
        print(f"{badge('FAIL')} OMP extensions: unable to read global OMP configuration")
        return
    known = {extension_identity(item) for item in current}
    additions = [item for item in desired if extension_identity(item) not in known]
    if not additions:
        print(f"{badge('OK')} OMP extensions: all managed extensions are configured")
        return
    merged = [*current, *additions]
    if runner.dry_run:
        print(f"{badge('RUN')} OMP extensions: add {', '.join(additions)}")
        return
    runner.run(
        ["omp", "config", "set", "extensions", json.dumps(merged, separators=(",", ":"))],
        "Configure OMP extensions",
    )


def omp_extension_status(manifest: dict[str, Any], runner: Runner) -> tuple[bool, str]:
    desired = manifest.get("ompExtensions", [])
    if not desired:
        return True, "no managed extensions"
    current = configured_omp_extensions(runner)
    if current is None:
        return False, "unable to read global OMP configuration"
    configured = {extension_identity(item) for item in current}
    missing_config = [item for item in desired if extension_identity(item) not in configured]
    missing_files = [item for item in desired if not expand_path(item).exists()]
    if missing_config:
        return False, f"not configured: {', '.join(missing_config)}"
    if missing_files:
        return False, f"source missing: {', '.join(missing_files)}"
    return True, f"{len(desired)} managed extension(s) configured and available"


def desired_mcp(manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    mcp = manifest["mcp"]
    target = expand_path(mcp.get("target", "~/.omp/agent/mcp.json"))
    return target, mcp


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DotAiError(f"Refusing to modify invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DotAiError(f"Refusing to modify non-object JSON at {path}")
    return value
def mcp_config_paths(target: Path) -> list[Path]:
    candidates = [
        target,
        home_dir() / ".omp" / "agent" / "mcp.json",
        ROOT / ".omp" / "mcp.json",
        ROOT / ".omp" / ".mcp.json",
        ROOT / "mcp.json",
        ROOT / ".mcp.json",
        home_dir() / ".config" / "opencode" / "opencode.json",
        ROOT / "opencode.json",
        home_dir() / ".cursor" / "mcp.json",
        ROOT / ".cursor" / "mcp.json",
        home_dir() / ".vscode" / "mcp.json",
        ROOT / ".vscode" / "mcp.json",
        home_dir() / ".claude" / "mcp.json",
        ROOT / ".claude" / "mcp.json",
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def discover_mcp_servers(target: Path) -> list[tuple[str, dict[str, Any], Path]]:
    discovered: list[tuple[str, dict[str, Any], Path]] = []
    for path in mcp_config_paths(target):
        if not path.is_file():
            continue
        try:
            config = load_json_object(path)
        except (OSError, DotAiError):
            continue
        disabled = set(config.get("disabledServers", []))
        for section in ("mcpServers", "servers", "mcp"):
            servers = config.get(section)
            if not isinstance(servers, dict):
                continue
            for name, server in servers.items():
                if name in disabled or not isinstance(server, dict) or server.get("enabled") is False:
                    continue
                discovered.append((name, server, path))
    return discovered


def server_satisfies(found: dict[str, Any], required: dict[str, Any]) -> bool:
    found_type = found.get("type", "http" if "url" in found else "stdio")
    required_type = required.get("type", "http" if "url" in required else "stdio")
    if found_type == "remote":
        found_type = "http"
    if required_type == "remote":
        required_type = "http"
    if found_type != required_type:
        return False
    if "url" in required:
        if str(found.get("url", "")).rstrip("/") != str(required["url"]).rstrip("/"):
            return False
    else:
        if found.get("command") != required.get("command"):
            return False
        if list(found.get("args", [])) != list(required.get("args", [])):
            return False
    for section in ("env", "headers"):
        expected = required.get(section, {})
        actual = found.get(section, {})
        if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
            return False
    return True




def sync_mcp(manifest: dict[str, Any], runner: Runner) -> bool:
    target, mcp = desired_mcp(manifest)
    existing = load_json_object(target)
    servers = dict(existing.get("mcpServers", {}))
    discovered = discover_mcp_servers(target)
    additions = 0
    for name, required in mcp["servers"].items():
        if any(server_satisfies(found, required) for _, found, _ in discovered):
            continue
        servers[name] = required
        additions += 1
    if additions == 0:
        print(f"{badge('OK')} MCP: all managed servers are available through discovered provider configs")
        return False
    merged = dict(existing)
    merged["$schema"] = mcp.get(
        "$schema", "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json"
    )
    merged["mcpServers"] = servers
    if merged == existing:
        print(f"{badge('OK')} MCP: already synchronized at {target}")
        return False
    if runner.dry_run:
        print(f"{badge('RUN')} MCP: add {additions} unavailable managed servers to {target}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target.with_name(f"{target.name}.bak.{stamp}")
        shutil.copy2(target, backup)
        print(f"{badge('OK')} MCP: backup written to {backup}")
    payload = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, target)
    print(f"{badge('OK')} MCP: synchronized {target}")
    return True


def legacy_skill_sources(manifest: dict[str, Any]) -> list[str]:
    return [skill["source"] for skill in manifest["skills"] if skill.get("agent") == "pi"]


def print_legacy_skill_notice(manifest: dict[str, Any]) -> bool:
    sources = legacy_skill_sources(manifest)
    if not sources:
        return False
    print(f"{badge('DRIFT')} Legacy Pi skill targets: {', '.join(sources)}")
    print("  Run 'dotai fix' to review and migrate them to the OMP universal target.")
    return True


def skill_status(skill: dict[str, Any]) -> tuple[bool, str]:
    agent = skill.get("agent", "universal")
    roots = {
        "pi": home_dir() / ".pi" / "agent" / "skills",
        "universal": home_dir() / ".agents" / "skills",
    }
    root = roots.get(agent, home_dir() / f".{agent}" / "skills")
    checks = skill.get("checkSkills", [])
    if checks and all((root / name / "SKILL.md").is_file() for name in checks):
        return True, f"installed for {agent}"
    plugin_cache = home_dir() / ".codex" / "plugins" / "cache"
    if checks and plugin_cache.is_dir():
        found_elsewhere = all(next(plugin_cache.glob(f"**/skills/{name}/SKILL.md"), None) for name in checks)
        if found_elsewhere:
            return False, f"installed as a Codex plugin, but inactive in OMP; not installed for {agent}"
    return False, f"not installed for {agent}"


def json_contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return needle in value or any(json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(json_contains(item, needle) for item in value)
    return value == needle


def registry_contains(path: Path, needle: str) -> bool:
    try:
        return json_contains(load_json_object(path), needle)
    except (OSError, DotAiError):
        return False


def mcp_status(manifest: dict[str, Any]) -> tuple[bool, str]:
    target, mcp = desired_mcp(manifest)
    try:
        load_json_object(target)
    except DotAiError as exc:
        return False, str(exc)
    discovered = discover_mcp_servers(target)
    missing: list[str] = []
    sources: set[Path] = set()
    for name, required in mcp["servers"].items():
        matches = [(found, path) for _, found, path in discovered if server_satisfies(found, required)]
        if not matches:
            missing.append(name)
        else:
            sources.add(matches[0][1])
    if missing:
        return False, f"unavailable: {', '.join(missing)}"
    return True, f"{len(mcp['servers'])} managed servers available across {len(sources)} discovered config(s)"


def omp_routing_status(manifest: dict[str, Any], runner: Runner) -> tuple[str, str]:
    routing = manifest.get("ompRouting")
    if not routing:
        return "OK", "not configured in manifest"

    try:
        recommendations = load_routing_recommendations()
    except DotAiError:
        return "FAIL", "unable to read routing recommendations"
    available = available_omp_models(runner)
    if available is None:
        return "FAIL", "unable to read OMP model catalog"
    try:
        providers = detected_routing_providers(recommendations, available)
    except DotAiError as exc:
        return "FAIL", str(exc)

    persisted_providers = set(routing["providers"])
    detected_providers = set(providers)
    if not persisted_providers & detected_providers:
        return "INACTIVE", "configured providers are unavailable"
    if persisted_providers != detected_providers:
        return (
            "DRIFT",
            "authenticated providers changed; run 'dotai configure omp-routing'",
        )

    primaries, fallbacks, unavailable = resolve_omp_routing(
        recommendations,
        routing["providers"],
        routing["primaryProvider"],
        available,
    )
    if unavailable:
        return "DRIFT", f"unavailable roles: {', '.join(unavailable)}"

    record_keys = ("modelRoles", "retry.fallbackChains", "task.agentModelOverrides")
    scalar_values = omp_routing_scalar_values(routing)
    current = {key: configured_omp_value(runner, key) for key in (*record_keys, *scalar_values)}
    if any(
        not isinstance(current[key], dict) or any(not isinstance(name, str) for name in current[key])
        for key in record_keys
    ) or any(current[key] is None for key in scalar_values):
        return "FAIL", "unable to read required OMP configuration"
    for role, primary in primaries.items():
        if current["modelRoles"].get(role) != primary:
            return "DRIFT", f"model role differs: {role}"
    for role, chain in fallbacks.items():
        if current["retry.fallbackChains"].get(role) != chain:
            return "DRIFT", f"fallback chain differs: {role}"
    for agent, model in routing["agentModelOverrides"].items():
        if current["task.agentModelOverrides"].get(agent) != model:
            return "DRIFT", f"agent override differs: {agent}"
    for key, value in scalar_values.items():
        if current[key] != value:
            return "DRIFT", f"{key} differs"
    return "OK", "configured roles match"


def print_status(manifest: dict[str, Any], runner: Runner) -> bool:
    healthy = True
    print(f"{heading('Platform:')} {runner.platform}")
    print(heading("Packages:"))
    for package in manifest["packages"]:
        installed = package_check(package, runner)
        healthy &= installed
        version = runner.output(selected(package.get("check", []), runner.platform)) if installed else "not found"
        label = "OK" if installed else "MISSING"
        print(f"  {badge(label)} {package['name']}: {version.splitlines()[0] if version else 'installed'}")
    print(heading("Skills:"))
    for skill in manifest["skills"]:
        installed, detail = skill_status(skill)
        legacy = skill.get("agent") == "pi"
        healthy &= installed and not legacy
        label = "DRIFT" if legacy and installed else "OK" if installed else "INACTIVE" if detail.startswith("installed as") else "MISSING"
        print(f"  {badge(label)} {skill['source']}: {detail}")
    if print_legacy_skill_notice(manifest):
        healthy = False
    if manifest["marketplaces"]:
        print(heading("Marketplaces:"))
        registry = home_dir() / ".omp" / "marketplaces.json"
        for marketplace in manifest["marketplaces"]:
            installed = registry_contains(registry, marketplace["name"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            print(f"  {badge(label)} {marketplace['name']}")
    if manifest["plugins"]:
        print(heading("Plugins:"))
        for plugin in manifest["plugins"]:
            registry = (
                ROOT / ".omp" / "plugins" / "installed_plugins.json"
                if plugin.get("scope") == "project"
                else home_dir() / ".omp" / "plugins" / "installed_plugins.json"
            )
            installed = registry_contains(registry, plugin["id"])
            healthy &= installed
            label = "OK" if installed else "MISSING"
            print(f"  {badge(label)} {plugin['id']}")
    if manifest.get("ompExtensions"):
        extensions_ok, detail = omp_extension_status(manifest, runner)
        healthy &= extensions_ok
        label = "OK" if extensions_ok else "MISSING"
        print(f"{heading('OMP extensions:')}\n  {badge(label)} {detail}")
    if manifest.get("ompRouting"):
        label, detail = omp_routing_status(manifest, runner)
        healthy &= label == "OK"
        print(f"{heading('OMP routing:')}\n  {badge(label)} {detail}")
    mcp_ok, detail = mcp_status(manifest)
    healthy &= mcp_ok
    label = "OK" if mcp_ok else "DRIFT"
    print(f"{heading('MCP:')}\n  {badge(label)} {detail}")
    return healthy


def writable_parent(path: Path) -> bool:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


def doctor(manifest: dict[str, Any], runner: Runner) -> bool:
    healthy = print_status(manifest, runner)
    target, _ = desired_mcp(manifest)
    writable = writable_parent(target)
    healthy &= writable
    print(f"{heading('Config target:')}\n  {badge('OK' if writable else 'FAIL')} writable parent for {target}")
    manager_checks = {
        "windows": ["scoop", "--version"],
        "macos": ["brew", "--version"],
        "ubuntu": ["apt-get", "--version"],
        "wsl": ["apt-get", "--version"],
        "arch": ["pacman", "--version"],
    }
    manager = manager_checks.get(runner.platform)
    if manager:
        available = runner.succeeds(manager)
        healthy &= available
        print(f"{heading('Package manager:')}\n  {badge('OK' if available else 'FAIL')} {manager[0]}")
    return healthy


def save_state(manifest_path: Path, runner: Runner, operation: str) -> None:
    if runner.dry_run or runner.failures:
        return
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    value = {
        "lastOperation": operation,
        "lastSuccess": dt.datetime.now(dt.timezone.utc).isoformat(),
        "manifest": str(manifest_path.resolve()),
        "platform": runner.platform,
    }
    target = directory / "state.json"
    target.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def reconcile(
    manifest: dict[str, Any],
    manifest_path: Path,
    runner: Runner,
    mode: str,
    force: bool = False,
    include_dependencies: bool = False,
) -> int:
    reconcile_packages(manifest, runner, mode, force, include_dependencies)
    reconcile_omp_extensions(manifest, runner)
    reconcile_skills(manifest, runner)
    reconcile_plugins(manifest, runner, mode)
    try:
        sync_mcp(manifest, runner)
    except (OSError, DotAiError) as exc:
        runner.failures.append(str(exc))
        print(f"{badge('FAIL')} MCP: {exc}")
    save_state(manifest_path, runner, mode)
    if runner.failures:
        print(f"\n{styled('Reconciliation failed:', 'red', 'bold')}")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1
    print(f"\n{styled(f'DotAi {mode} complete for {runner.platform}.', 'green', 'bold')}")
    return 0


def parse_platform_commands(values: list[str] | None, option: str) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}
    for value in values or []:
        platform_name, separator, command = value.partition("=")
        if not separator or platform_name not in {"windows", "wsl", "ubuntu", "arch", "macos", "linux", "default"} or not command:
            raise DotAiError(f"{option} must use PLATFORM=COMMAND")
        commands.setdefault(platform_name, []).append(command)
    return commands


def parse_assignments(
    values: list[str] | None,
    option: str,
    name_pattern: re.Pattern[str],
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for item in values or []:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            raise DotAiError(f"{option} must use NAME=VALUE")
        if not name_pattern.fullmatch(name):
            raise DotAiError(f"{option} has an invalid name: {name}")
        if name in assignments:
            raise DotAiError(f"{option} repeats name: {name}")
        assignments[name] = value
    return assignments


def upsert(items: list[dict[str, Any]], key: str, value: dict[str, Any]) -> None:
    for index, item in enumerate(items):
        if item.get(key) == value[key]:
            items[index] = value
            return
    items.append(value)



def legacy_skill_migration(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    updated = dict(manifest)
    migrated: list[str] = []
    skills = []
    for skill in manifest["skills"]:
        if skill.get("agent") == "pi":
            migrated.append(skill["source"])
            skills.append({**skill, "agent": "universal"})
        else:
            skills.append(skill)
    updated["skills"] = skills
    return updated, migrated


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
    shutil.copy2(path, backup)
    return backup


def fix_legacy_skills(manifest: dict[str, Any], path: Path, runner: Runner) -> int:
    updated, migrated = legacy_skill_migration(manifest)
    if not migrated:
        print(f"{badge('OK')} No legacy Pi-targeted skills found in {path}.")
        return 0

    print(f"{heading('Proposed skill migration:')}")
    print(manifest_diff(manifest, updated, path))
    print(f"\nMigrates {len(migrated)} skill source(s) from Pi to the OMP universal target.")
    if runner.dry_run:
        print(f"{badge('RUN')} Dry run: no manifest changes applied.")
        reconcile_skills(updated, runner)
        return 1 if runner.failures else 0
    try:
        answer = input("Apply these changes and install the migrated skills? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        print(f"{badge('OK')} No changes applied.")
        return 0

    backup = backup_manifest(path)
    write_manifest(path, updated)
    print(f"{badge('OK')} Manifest backup written to {backup}")
    reconcile_skills(updated, runner)
    if runner.failures:
        print(f"{styled('Skill migration failed:', 'red', 'bold')}")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1
    print(f"{styled('Skill migration complete.', 'green', 'bold')}")
    return 0

def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def add_integration(args: argparse.Namespace, manifest: dict[str, Any], path: Path) -> int:
    kind = args.kind
    if kind == "skill":
        value = {
            "source": args.source,
            "agent": args.agent,
            "skills": args.skills or ["*"],
            "checkSkills": args.check_skills or [],
        }
        upsert(manifest["skills"], "source", value)
    elif kind == "marketplace":
        upsert(manifest["marketplaces"], "name", {"name": args.name, "source": args.source})
    elif kind == "plugin":
        upsert(manifest["plugins"], "id", {"id": args.id, "scope": args.scope})
    elif kind == "mcp":
        if not SERVER_NAME.fullmatch(args.name):
            raise DotAiError(f"Invalid MCP server name: {args.name}")
        if args.url:
            if args.server_args:
                raise DotAiError("--arg requires --command")
            if args.server_env:
                raise DotAiError("--env requires --command")
            value = {"type": args.transport, "url": args.url}
            headers = parse_assignments(args.headers, "--header", HEADER_NAME)
            if headers:
                value["headers"] = headers
        else:
            if args.headers:
                raise DotAiError("--header requires --url")
            value = {"type": "stdio", "command": args.server_command}
            if args.server_args:
                value["args"] = args.server_args
            server_env = parse_assignments(args.server_env, "--env", ENV_NAME)
            if server_env:
                value["env"] = server_env
        manifest["mcp"]["servers"][args.name] = value
    elif kind == "tool":
        installs = parse_platform_commands(args.install_commands, "--install")
        if not installs:
            raise DotAiError("At least one --install PLATFORM=COMMAND is required")
        value = {"name": args.name, "check": args.check_command, "install": installs}
        if args.update_group != "core":
            value["updateGroup"] = args.update_group
        updates = parse_platform_commands(args.update_commands, "--update")
        if updates:
            value["update"] = updates
        upsert(manifest["packages"], "name", value)
    else:
        raise DotAiError(f"Unsupported integration kind: {kind}")
    load_check = dict(manifest)
    if load_check.get("version") != 1:
        raise DotAiError("Generated manifest is invalid")
    write_manifest(path, manifest)
    print(f"{badge('OK')} Added {kind} to {path}. Run 'dotai sync' or 'dotai install' to apply it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dotai", description="Install and reconcile a portable AI development stack.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Stack manifest (default: stack.json)")
    parser.add_argument("--platform", choices=["windows", "wsl", "ubuntu", "arch", "macos", "linux"], help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto for terminals, always, or never (default: auto)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="Install missing components and synchronize configuration")
    install.add_argument("--force", action="store_true", help="Reinstall components already present")
    install.add_argument("--dry-run", action="store_true", help="Print actions without changing the machine")
    update = sub.add_parser("update", help="Update core components and synchronize configuration")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Also update installed packages marked as dependencies",
    )
    sync = sub.add_parser("sync", help="Synchronize skills, plugins, and MCP configuration")
    sync.add_argument("--dry-run", action="store_true")
    configure = sub.add_parser("configure", help="Configure explicit post-authentication integrations")
    configure_sub = configure.add_subparsers(dest="configure_target", required=True)
    configure_routing = configure_sub.add_parser("omp-routing", help="Configure OMP multi-provider model routing")
    configure_routing.add_argument("--dry-run", action="store_true")
    configure_routing.add_argument(
        "--primary",
        choices=["anthropic", "openai-codex"],
        help="Interactive primary when both Anthropic and Codex are authenticated",
    )
    sub.add_parser("version", help="Print the current DotAi version")
    sub.add_parser("init", help="Generate a new manifest from stack.example.json")
    fix = sub.add_parser("fix", help="Review and migrate legacy Pi-targeted skills to OMP")
    fix.add_argument("--dry-run", action="store_true", help="Show the migration without changing files or machine state")
    add = sub.add_parser("add", help="Add a tool, skill, marketplace, plugin, or MCP server to the manifest")
    add_sub = add.add_subparsers(dest="kind", required=True)

    add_tool = add_sub.add_parser("tool", help="Add or replace a command-line tool")
    add_tool.add_argument("name")
    add_tool.add_argument("--check", dest="check_command", required=True, help="Command that exits zero when installed")
    add_tool.add_argument("--install", dest="install_commands", action="append", required=True, metavar="PLATFORM=COMMAND")
    add_tool.add_argument("--update", dest="update_commands", action="append", metavar="PLATFORM=COMMAND")
    add_tool.add_argument(
        "--update-group",
        choices=["core", "dependency"],
        default="core",
        help="Whether normal updates include this tool (default: core)",
    )

    add_skill = add_sub.add_parser("skill", help="Add or replace a skills.sh source")
    add_skill.add_argument("source")
    add_skill.add_argument("--agent", default="universal")
    add_skill.add_argument("--skill", dest="skills", action="append")
    add_skill.add_argument("--check-skill", dest="check_skills", action="append")

    add_marketplace = add_sub.add_parser("marketplace", help="Add or replace an OMP marketplace")
    add_marketplace.add_argument("name")
    add_marketplace.add_argument("source")

    add_plugin = add_sub.add_parser("plugin", help="Add or replace an OMP marketplace plugin")
    add_plugin.add_argument("id")
    add_plugin.add_argument("--scope", choices=["user", "project"], default="user")

    add_mcp = add_sub.add_parser("mcp", help="Add or replace an OMP MCP server")
    add_mcp.add_argument("name")
    mcp_transport = add_mcp.add_mutually_exclusive_group(required=True)
    mcp_transport.add_argument("--url")
    mcp_transport.add_argument("--command", dest="server_command")
    add_mcp.add_argument("--transport", choices=["http", "sse"], default="http")
    add_mcp.add_argument("--arg", dest="server_args", action="append")
    add_mcp.add_argument(
        "--header",
        dest="headers",
        action="append",
        metavar="NAME=VALUE",
        help="HTTP header NAME=VALUE; VALUE may name an environment variable; repeatable",
    )
    add_mcp.add_argument(
        "--env",
        dest="server_env",
        action="append",
        metavar="NAME=VALUE",
        help="stdio environment NAME=VALUE; repeatable",
    )
    sub.add_parser("status", help="Show installed and configured components")
    sub.add_parser("doctor", help="Check commands, configuration, and platform prerequisites")
    sub.add_parser("validate", help="Validate the manifest")
    sub.add_parser("platform", help="Print the detected platform")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_color(args.color)
    if args.platform:
        os.environ["DOTAI_PLATFORM"] = args.platform
    if args.command == "version":
        print(VERSION)
        print_release_notice()
        return 0
    platform_name = detect_platform()
    if args.command == "platform":
        print(platform_name)
        return 0
    if args.command in {"install", "status", "sync"}:
        print_release_notice()
    if args.command == "init":
        try:
            if not initialize_manifest(args.manifest, allow_custom=True):
                raise DotAiError(f"Manifest already exists: {args.manifest}")
        except (OSError, DotAiError) as exc:
            print(f"{styled('dotai:', 'red', 'bold')} {exc}", file=sys.stderr)
            return 2
        return 0
    allow_legacy_routing = (
        args.command == "configure" and args.configure_target == "omp-routing"
    )
    try:
        initialize_default_manifest(args.manifest)
        manifest = load_manifest(
            args.manifest, allow_legacy_routing=allow_legacy_routing
        )
    except (OSError, DotAiError) as exc:
        print(f"{styled('dotai:', 'red', 'bold')} {exc}", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(f"{badge('OK')} Valid manifest: {args.manifest}")
        return 0
    if args.command == "add":
        try:
            return add_integration(args, manifest, args.manifest)
        except (OSError, DotAiError) as exc:
            print(f"{styled('dotai:', 'red', 'bold')} {exc}", file=sys.stderr)
            return 2
    runner = Runner(platform_name, getattr(args, "dry_run", False), args.verbose)
    if args.command == "configure":
        try:
            return configure_omp_routing(
                manifest, args.manifest, runner, args.primary
            )
        except (OSError, DotAiError) as exc:
            print(f"{styled('dotai:', 'red', 'bold')} {exc}", file=sys.stderr)
            return 2
    if args.command == "fix":
        try:
            return fix_legacy_skills(manifest, args.manifest, runner)
        except OSError as exc:
            print(f"{styled('dotai:', 'red', 'bold')} {exc}", file=sys.stderr)
            return 2
    if args.command == "status":
        return 0 if print_status(manifest, runner) else 1
    if args.command == "doctor":
        return 0 if doctor(manifest, runner) else 1
    if args.command == "sync":
        reconcile_omp_extensions(manifest, runner)
        reconcile_skills(manifest, runner)
        reconcile_plugins(manifest, runner, "install")
        try:
            sync_mcp(manifest, runner)
        except (OSError, DotAiError) as exc:
            runner.failures.append(str(exc))
        save_state(args.manifest, runner, "sync")
        return 1 if runner.failures else 0
    if args.command == "update":
        print_legacy_skill_notice(manifest)
    return reconcile(
        manifest,
        args.manifest,
        runner,
        args.command,
        getattr(args, "force", False),
        getattr(args, "include_dependencies", False),
    )


if __name__ == "__main__":
    raise SystemExit(main())
