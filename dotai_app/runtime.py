"""Repository paths, platform selection, and external command execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import re
import subprocess
import sys
from . import terminal


ROOT = Path(__file__).resolve().parents[1]


PLACEHOLDER = re.compile(r"\{(home|repo|python)\}")


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
        print(f"{terminal.badge('RUN')} {label}: {self.display_command(command)}")
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
            print(f"{terminal.badge('FAIL')} {label}: {exc}")
            return None
        if result.returncode != 0:
            detail = f" (exit {result.returncode})"
            if capture and result.stdout:
                detail += f": {result.stdout.strip()}"
            if required:
                self.failures.append(label + detail)
            print(f"{terminal.badge('FAIL')} {label}{detail}")
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
                registered = result.stdout.strip().split(os.pathsep)
                inherited = self.env["PATH"].split(os.pathsep)
                self.env["PATH"] = os.pathsep.join(registered + [path for path in inherited if path not in registered])
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
                self.argv(command), env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False
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
