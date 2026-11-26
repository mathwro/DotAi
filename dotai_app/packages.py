"""Package presence, minimum versions, and install/update reconciliation."""

from __future__ import annotations

from typing import Any
import re
import subprocess
from . import manifest as manifests
from . import runtime
from . import terminal


PACKAGE_VERSION_PATTERN = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?(?![\w.-])")


def _run_probe(runner: runtime.Runner, command: Any) -> tuple[bool, str, str]:
    if not command:
        return False, "", ""
    runner.secrets.update(terminal.credential_values(runner.env))
    runner.secrets.update(terminal.credential_values(command))
    try:
        result = subprocess.run(
            runner.argv(command), env=runner.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
        stdout = (result.stdout or b"").decode("utf-8").strip()
        stderr = (result.stderr or b"").decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        if runner.verbose:
            runner.say(f"  probe: {runner.display_command(command)}: {terminal.diagnostic(str(exc), runner.secrets)}")
        return False, "", ""
    if runner.verbose:
        runner.say(f"  probe: {runner.display_command(command)} (exit {result.returncode})")
        for name, text in (("stdout", stdout), ("stderr", stderr)):
            detail = terminal.diagnostic(text, runner.secrets)
            if detail:
                runner.say(f"  {name}: {detail}")
    return result.returncode == 0, stdout, stderr

def _evaluate_minimum_version(
    minimum_value: Any, succeeded: bool, stdout: str, stderr: str
) -> tuple[bool, bool, str]:
    minimum = manifests.VERSION_MINIMUM_PATTERN.fullmatch(minimum_value) if isinstance(minimum_value, str) else None
    if not minimum:
        return False, False, "not found"
    version = PACKAGE_VERSION_PATTERN.search(stdout)
    if version:
        output = stdout
    else:
        version = PACKAGE_VERSION_PATTERN.search(stderr)
        output = stderr if version else stdout or stderr
    if not succeeded or not version:
        return False, succeeded, output or "not found"
    actual_parts = tuple(int(part or 0) for part in version.groups())
    minimum_parts = tuple(int(part or 0) for part in minimum.groups())
    return actual_parts >= minimum_parts, True, output


def probe_package(package: dict[str, Any], runner: runtime.Runner) -> tuple[bool, bool, str]:
    command = runtime.selected(package.get("check", []), runner.platform)
    if "minimumVersion" in package:
        if not (isinstance(package["minimumVersion"], str) and manifests.VERSION_MINIMUM_PATTERN.fullmatch(package["minimumVersion"])) or not command:
            return False, False, "not found"
        succeeded, stdout, stderr = _run_probe(runner, command)
        return _evaluate_minimum_version(package["minimumVersion"], succeeded, stdout, stderr)
    succeeded, stdout, _ = _run_probe(runner, command)
    if not succeeded:
        return False, False, "not found"
    return True, True, stdout


def package_version_check(package: dict[str, Any], runner: runtime.Runner, command: Any) -> tuple[bool, str]:
    minimum_value = package["minimumVersion"]
    if not (isinstance(minimum_value, str) and manifests.VERSION_MINIMUM_PATTERN.fullmatch(minimum_value)) or not command:
        return False, "not found"
    succeeded, stdout, stderr = _run_probe(runner, command)
    healthy, _, detail = _evaluate_minimum_version(minimum_value, succeeded, stdout, stderr)
    return healthy, detail


def package_check(package: dict[str, Any], runner: runtime.Runner) -> bool:
    command = runtime.selected(package.get("check", []), runner.platform)
    if "minimumVersion" in package:
        return package_version_check(package, runner, command)[0]
    return bool(command) and runner.succeeds(command)


_DEFAULT_PACKAGE_CHECK = package_check

def reconcile_packages(
    manifest: dict[str, Any],
    runner: runtime.Runner,
    mode: str,
    force: bool = False,
    include_dependencies: bool = False,
) -> None:
    for package in manifest["packages"]:
        name = package["name"]
        if package_check is _DEFAULT_PACKAGE_CHECK:
            installed, present, _ = probe_package(package, runner)
        else:
            installed = package_check(package, runner)
            check_command = runtime.selected(package.get("check", []), runner.platform)
            present = installed or (
                "minimumVersion" in package and bool(check_command) and runner.succeeds(check_command)
            )
        if (
            mode == "update"
            and present
            and package.get("updateGroup", "core") == "dependency"
            and not include_dependencies
        ):
            runner.say(f"{terminal.badge('OK')} {name}: dependency update skipped (use --include-dependencies)")
        elif mode == "install" and installed and not force:
            runner.say(f"{terminal.badge('OK')} {name}: already installed")
        else:
            operation = "update" if present and (mode == "update" or not installed) else "install"
            steps = runtime.selected(package.get(operation, package.get("install", {})), runner.platform)
            if not steps and operation == "update" and installed:
                runner.say(f"{terminal.badge('OK')} {name}: no managed update required")
            elif not steps:
                runner.failures.append(f"{name}: no {operation} commands for {runner.platform}")
                runner.say(f"{terminal.badge('FAIL')} {name}: unsupported platform {runner.platform}")
                continue
            else:
                label = f"Check/update {name}" if operation == "update" else f"Install {name}"
                runtime.run_steps(steps, runner, label)
        if package.get("configure"):
            runtime.run_steps(runtime.selected(package["configure"], runner.platform), runner, f"Configure {name}")
        if not runner.dry_run and not package_check(package, runner):
            _, stdout, stderr = _run_probe(runner, runtime.selected(package.get("check", []), runner.platform))
            cause = terminal.diagnostic(
                stderr or stdout or "no output captured, or the command could not start", runner.secrets, limit=400, lines=3
            )
            runner.failures.append(f"{name}: verification command failed after reconciliation: {cause}")
            runner.say(f"{terminal.badge('FAIL')} {name}: verification failed: {cause}")
            runner.say("  Next step: Run the check command directly or retry with --verbose for sanitized diagnostics.")
