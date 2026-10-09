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
        self.outcomes: list[dict[str, str]] = []
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

    def _secrets(self, command: str | list[str]) -> list[str]:
        values = terminal.credential_values(self.env)
        values.extend(terminal.credential_values(command))
        if isinstance(command, list):
            for index, part in enumerate(command[:-1]):
                if terminal._SECRET_KEY.search(str(part)) and str(part).startswith("--") and "=" not in str(part):
                    values.append(str(command[index + 1]))
                elif part in {"--header", "-H", "--env", "-e"}:
                    value = str(command[index + 1])
                    separator = ":" if part in {"--header", "-H"} else "="
                    if separator in value:
                        values.append(value.split(separator, 1)[1].strip())
        return values

    def display_command(self, command: str | list[str]) -> str:
        secrets = self._secrets(command)
        if isinstance(command, str):
            return terminal.redact(terminal.omit_protocol(command, "<structured configuration>"), secrets)
        parts = []
        for part in command:
            text = terminal.omit_protocol(str(part), "<structured configuration>")
            parts.append(terminal.redact(text, secrets))
        return " ".join(parts)

    def record_outcome(self, label: str, status: str, detail: str = "") -> None:
        if status not in {"changed", "unchanged", "skipped", "failed", "planned"}:
            raise ValueError(f"Unknown outcome status: {status}")
        self.outcomes.append({
            "label": terminal.redact(label, terminal.credential_values(self.env)),
            "status": status,
            "detail": terminal.redact(detail, terminal.credential_values(self.env)),
        })

    def summary(self) -> str:
        counts = {status: sum(item["status"] == status for item in self.outcomes)
                  for status in ("changed", "unchanged", "skipped", "failed", "planned")}
        prefix = "Dry-run summary" if self.dry_run else (
            "Partial completion" if counts["failed"] and any(counts[key] for key in ("changed", "unchanged"))
            else "Summary"
        )
        text = f"{prefix}: " + ", ".join(f"{counts[key]} {key}" for key in counts if key != "planned" or counts[key])
        if self.dry_run:
            text += "; no changes applied."
        print(text)
        return text

    def _diagnostics(self, command: str | list[str], stdout: str | None, stderr: str | None) -> None:
        if not self.verbose:
            return
        secrets = self._secrets(command)
        for stream, text in (("stdout", stdout), ("stderr", stderr)):
            if text:
                lines = terminal.diagnostic_lines(text, secrets)
                for line in lines[:30]:
                    print(f"  {stream}: {line}")
                if len(lines) > 30:
                    print(f"  {stream}: further diagnostics omitted.")

    def fail(self, label: str, cause: str, *, required: bool = True, command: str | list[str] | None = None) -> None:
        command = command or []
        detail = terminal.redact(cause, self._secrets(command))[:300]
        message = terminal.redact(f"{label}: {detail}", self._secrets(command))
        if required:
            self.failures.append(message)
        self.record_outcome(label, "failed", detail)
        print(f"{terminal.badge('FAIL')} {message}", flush=True)
        if "permission" in detail.lower() or "access denied" in detail.lower():
            print("  Next step: check access to the component's target and rerun with --verbose.")
        elif "no such file" in detail.lower() or "not found" in detail.lower():
            print("  Next step: install the required executable, check PATH, and rerun with --verbose.")
        else:
            print("  Next step: rerun with --verbose, resolve the reported cause, then retry this component.")

    def run(
        self, command: str | list[str], label: str, *, capture: bool = False,
        required: bool = True, interactive: bool = False,
    ) -> subprocess.CompletedProcess[str] | None:
        if interactive and capture:
            raise ValueError("Interactive execution cannot capture its terminal streams.")
        safe_label = terminal.redact(label, self._secrets(command))
        action = "Would run" if self.dry_run else "Starting"
        suffix = " (interactive; input and output use your terminal)" if interactive else ""
        print(f"{terminal.badge('RUN')} {action} {safe_label}{suffix}", flush=True)
        if self.verbose:
            print(f"  Command: {self.display_command(command)}", flush=True)
        if self.dry_run:
            self.record_outcome(safe_label, "planned")
            return None
        try:
            result = subprocess.run(
                self.argv(command), env=self.env, text=True, encoding="utf-8", errors="replace",
                stdin=None if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.PIPE,
                stderr=None if interactive else subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            self.fail(safe_label, str(exc), required=required, command=command)
            return None
        if result.returncode != 0:
            secrets = self._secrets(command)
            cause = terminal.failure_cause(result.stderr or "", secrets) or terminal.failure_cause(result.stdout or "", secrets)
            if not cause:
                cause = "The tool did not provide a plain-language error."
            self.fail(safe_label, f"exit {result.returncode}: {cause}", required=required, command=command)
        else:
            self.record_outcome(safe_label, "changed")
            print(f"{terminal.badge('OK')} {safe_label}: completed.", flush=True)
        self._diagnostics(command, result.stdout, result.stderr)
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
            result = subprocess.run(
                self.argv(command), env=self.env, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE if self.verbose else subprocess.DEVNULL,
                stderr=subprocess.PIPE if self.verbose else subprocess.DEVNULL, check=False,
            )
        except OSError as exc:
            if self.verbose:
                print(f"  Probe could not start: {terminal.redact(exc, self._secrets(command))}")
            return False
        if self.verbose:
            print(f"  Probe command: {self.display_command(command)} (exit {result.returncode})")
            self._diagnostics(command, result.stdout, result.stderr)
        return result.returncode == 0

    def output(self, command: str | list[str]) -> str:
        try:
            result = subprocess.run(
                self.argv(command), env=self.env, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
        except OSError as exc:
            if self.verbose:
                print(f"  Probe could not start: {terminal.redact(exc, self._secrets(command))}")
            return ""
        if self.verbose:
            print(f"  Probe command: {self.display_command(command)} (exit {result.returncode})")
            self._diagnostics(command, result.stdout, result.stderr)
        return result.stdout.strip() if result.returncode == 0 else ""


def run_steps(steps: Any, runner: Runner, label: str) -> None:
    for index, step in enumerate(steps or [], start=1):
        step_label = f"{label} ({index}/{len(steps)})"
        previous_failures = len(runner.failures)
        if isinstance(step, (str, list)):
            runner.run(step, step_label)
        else:
            runner.fail(step_label, "invalid command entry")
        if len(runner.failures) > previous_failures:
            for pending in range(index + 1, len(steps) + 1):
                runner.record_outcome(f"{label} ({pending}/{len(steps)})", "skipped", "Earlier step failed")
            if index < len(steps):
                print(terminal.redact(
                    f"{terminal.badge('INACTIVE')} {label}: {len(steps) - index} later step(s) not run after failure."
                ))
            break
