"""Package presence, minimum versions, and install/update reconciliation."""

from __future__ import annotations

from typing import Any, Callable
import re
import subprocess
from . import manifest as manifests
from . import runtime
from . import terminal


PACKAGE_VERSION_PATTERN = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?(?![\w.-])")


def package_version_check(package: dict[str, Any], runner: runtime.Runner, command: Any) -> tuple[bool, str]:
    minimum_value = package.get("minimumVersion")
    minimum = manifests.VERSION_MINIMUM_PATTERN.fullmatch(minimum_value) if isinstance(minimum_value, str) else None
    desired_value = package.get("version", "latest")
    desired = manifests.VERSION_MINIMUM_PATTERN.fullmatch(desired_value) if isinstance(desired_value, str) and desired_value != "latest" else None
    if not command or ("minimumVersion" in package and not minimum) or (desired_value != "latest" and not desired):
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
    healthy = True
    if minimum:
        healthy &= actual_parts >= tuple(int(part or 0) for part in minimum.groups())
    if desired:
        healthy &= actual_parts == tuple(int(part or 0) for part in desired.groups())
    return healthy, output


def package_check(package: dict[str, Any], runner: runtime.Runner) -> bool:
    command = runtime.selected(package.get("check", []), runner.platform)
    if "minimumVersion" in package or package.get("version", "latest") != "latest":
        return package_version_check(package, runner, command)[0]
    return bool(command) and runner.succeeds(command)


def package_operation(package: dict[str, Any], runner: runtime.Runner, mode: str, force: bool = False) -> str | None:
    if not package.get("enabled", True) or mode == "sync":
        return None
    if mode not in {"install", "update"}:
        raise runtime.DotAiError(f"Unsupported package reconciliation mode: {mode}")
    if force:
        return "install"
    installed = package_check(package, runner)
    if installed and (mode == "install" or package.get("updatePolicy") == "pinned"):
        return None
    command = runtime.selected(package.get("check", []), runner.platform)
    present = installed or bool(command) and runner.succeeds(command)
    if present and runtime.selected(package.get("update", []), runner.platform):
        return "update"
    if not installed:
        return "install"
    return None


def selected_subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and selected_subset(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return all(any(selected_subset(item, desired) for item in actual
                       if not isinstance(item, dict) or item.get("enabled", True)) for desired in expected)
    if isinstance(actual, dict) and isinstance(expected, str):
        return actual.get("enabled", True) and isinstance(actual.get("path"), str) and selected_subset(actual["path"], expected)
    if isinstance(actual, str) and isinstance(expected, str):
        return manifests.extension_identity(actual) == manifests.extension_identity(expected)
    return type(actual) is type(expected) and actual == expected


def reconcile_packages(
    manifest: dict[str, Any],
    runner: runtime.Runner,
    mode: str,
    force: bool = False,
    *,
    on_installed: Callable[[dict[str, Any], dict[str, Any], runtime.Runner], None] | None = None,
) -> None:
    manifests.validate_manifest(manifest)
    for package in manifest["packages"]:
        if not package.get("enabled", True):
            continue
        name = package["name"]
        operation = package_operation(package, runner, mode, force)
        if operation is None:
            print(f"{terminal.badge('OK')} {terminal.redact(name)}: installed version retained; no package operation needed")
            runner.record_outcome(name, "unchanged", "Installed version retained")
        else:
            steps = runtime.selected(package.get(operation, []), runner.platform)
            if not steps:
                runner.fail(name, f"Unsupported {operation} on {runner.platform}: no reviewed commands")
                continue
            failures_before = len(runner.failures)
            runtime.run_steps(steps, runner, f"{operation.capitalize()} {name}")
            if len(runner.failures) != failures_before:
                continue
        if package.get("configure") and selected_subset(manifest, package.get("configureWhen", {})):
            failures_before = len(runner.failures)
            runtime.run_steps(runtime.selected(package["configure"], runner.platform), runner, f"Configure {name}")
            if len(runner.failures) != failures_before:
                continue
        if not runner.dry_run and not package_check(package, runner):
            runner.fail(name, "Verification command failed after reconciliation")
            continue
        if operation is not None and not runner.dry_run and on_installed is not None:
            on_installed(package, manifest, runner)
