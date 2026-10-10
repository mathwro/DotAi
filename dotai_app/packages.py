"""Package presence, minimum versions, and install/update reconciliation."""

from __future__ import annotations

from typing import Any
import re
import subprocess
from . import manifest as manifests
from . import runtime
from . import terminal


PACKAGE_VERSION_PATTERN = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?(?![\w.-])")


def package_version_check(package: dict[str, Any], runner: runtime.Runner, command: Any) -> tuple[bool, str]:
    minimum_value = package["minimumVersion"]
    minimum = manifests.VERSION_MINIMUM_PATTERN.fullmatch(minimum_value) if isinstance(minimum_value, str) else None
    if not minimum or not command:
        return False, "not found"
    try:
        result = subprocess.run(
            runner.argv(command), env=runner.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except (OSError, UnicodeError):
        return False, "not found"
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    version = PACKAGE_VERSION_PATTERN.search(stdout)
    if version:
        output = stdout
    else:
        version = PACKAGE_VERSION_PATTERN.search(stderr)
        output = stderr if version else stdout or stderr
    if result.returncode != 0 or not version:
        return False, output or "not found"
    actual_parts = tuple(int(part or 0) for part in version.groups())
    minimum_parts = tuple(int(part or 0) for part in minimum.groups())
    return actual_parts >= minimum_parts, output


def package_check(package: dict[str, Any], runner: runtime.Runner) -> bool:
    command = runtime.selected(package.get("check", []), runner.platform)
    if "minimumVersion" in package:
        return package_version_check(package, runner, command)[0]
    return bool(command) and runner.succeeds(command)


def reconcile_packages(
    manifest: dict[str, Any],
    runner: runtime.Runner,
    mode: str,
    force: bool = False,
) -> None:
    manifests.validate_manifest(manifest)
    for package in manifest["packages"]:
        if not package.get("enabled", True):
            continue
        name = package["name"]
        installed = package_check(package, runner)
        check_command = runtime.selected(package.get("check", []), runner.platform)
        present = installed or (
            "minimumVersion" in package and bool(check_command) and runner.succeeds(check_command)
        )
        if mode == "install" and installed and not force:
            print(f"{terminal.badge('OK')} {name}: already installed")
        else:
            operation = "update" if present and (mode == "update" or not installed) else "install"
            steps = runtime.selected(package.get(operation, package.get("install", {})), runner.platform)
            if not steps and operation == "update" and installed:
                print(f"{terminal.badge('OK')} {name}: no managed update required")
            elif not steps:
                runner.failures.append(f"{name}: no {operation} commands for {runner.platform}")
                print(f"{terminal.badge('FAIL')} {name}: unsupported platform {runner.platform}")
                continue
            else:
                label = f"Check/update {name}" if operation == "update" else f"Install {name}"
                runtime.run_steps(steps, runner, label)
        if package.get("configure"):
            runtime.run_steps(runtime.selected(package["configure"], runner.platform), runner, f"Configure {name}")
        if not runner.dry_run and not package_check(package, runner):
            runner.failures.append(f"{name}: verification command failed after reconciliation")
            print(f"{terminal.badge('FAIL')} {name}: verification failed")
