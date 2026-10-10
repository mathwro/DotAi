"""Package presence, minimum versions, and install/update reconciliation."""

from __future__ import annotations

from typing import Any, Callable
import re
import os
from pathlib import Path
import shlex
import shutil
import sys
import subprocess
import hashlib
import tempfile
import urllib.request
from . import catalog
from . import manifest as manifests
from . import runtime
from . import terminal


PACKAGE_VERSION_PATTERN = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?(?![\w.-])")


def checked_package_path(package: dict[str, Any], runner: runtime.Runner) -> Path | None:
    """Find an independent checked executable, including an existing broken one."""
    check = runtime.selected(package.get("check", []), runner.platform)
    try:
        words = shlex.split(check, posix=runner.platform != "windows") if isinstance(check, str) else check
    except ValueError:
        return None
    if not words or any(token in {";", "&&", "||", "|"} for token in words):
        return None
    command = runner._format(words[0]).strip('"')
    name = Path(command).name.lower().removesuffix(".exe")
    if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", name) or name in {
        "sh", "bash", "zsh", "dash", "fish", "cmd", "powershell", "pwsh",
        "node", "ruby", "perl", "env",
    }:
        return None
    executable = shutil.which(command, path=runner.env.get("PATH"))
    path = Path(executable).absolute() if executable else None
    if path is None:
        candidates = [Path(command).absolute()] if Path(command).is_absolute() or "/" in command or "\\" in command else [
            Path(directory).absolute() / command for directory in runner.env.get("PATH", "").split(os.pathsep) if directory
        ]
        path = next((candidate for candidate in candidates if candidate.exists() or candidate.is_symlink()), None)
    if path is not None and path.resolve() == Path(sys.executable).resolve():
        return None
    return path


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
    ownership_check: Callable[[dict[str, Any], dict[str, Any], runtime.Runner, str], None] | None = None,
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
                return
            check = runtime.selected(package.get("check", []), runner.platform)
            if ownership_check is not None or checked_package_path(package, runner) is not None or check and runner.succeeds(check):
                failures_before = len(runner.failures)
                try:
                    if ownership_check is None:
                        raise runtime.DotAiError("Existing tool mutation requires proven ownership; adopt matching content explicitly")
                    ownership_check(package, manifest, runner, operation)
                except (OSError, ValueError, runtime.DotAiError) as exc:
                    if len(runner.failures) == failures_before:
                        runner.fail(name, str(exc))
                    return
                if len(runner.failures) != failures_before:
                    return
            failures_before = len(runner.failures)
            runtime.run_steps(steps, runner, f"{operation.capitalize()} {name}")
            if len(runner.failures) != failures_before:
                return
        if package.get("configure") and selected_subset(manifest, package.get("configureWhen", {})):
            failures_before = len(runner.failures)
            runtime.run_steps(runtime.selected(package["configure"], runner.platform), runner, f"Configure {name}")
            if len(runner.failures) != failures_before:
                return
        if not runner.dry_run and not package_check(package, runner):
            runner.fail(name, "Verification command failed after reconciliation")
            return
        if operation is not None and not runner.dry_run and on_installed is not None:
            on_installed(package, manifest, runner)


def install_native_release(facts: dict[str, Any], relative_target: str, recipe_id: str) -> None:
    """Verify a frozen release in a sibling staging directory before replacement."""
    recipe = catalog.load_catalog()["recipes"].get(recipe_id)
    if recipe is None:
        raise runtime.DotAiError("Unknown native installation recipe")
    descriptor = recipe.get("versionMetadata", {})
    catalog.validate_native_metadata(descriptor, facts, facts.get("version", ""))
    allowed = {value["launcher"] for value in recipe.get("footprint", {}).values()
               if isinstance(value, dict) and value.get("kind") == "standalone"}
    if relative_target not in allowed:
        raise runtime.DotAiError("Standalone release target does not match the reviewed recipe")
    relative = Path(relative_target)
    if relative.is_absolute() or ".." in relative.parts:
        raise runtime.DotAiError("Unsafe standalone release destination")
    home = runtime.home_dir()
    target = home / relative
    for parent in (target, *target.parents):
        if parent == home:
            break
        if parent.is_symlink() or (parent == target and parent.is_dir()):
            raise runtime.DotAiError("Refusing redirected standalone release destination")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dotai-native.", dir=target.parent) as directory:
        staged = Path(directory) / target.name
        digest = hashlib.sha256()
        request = urllib.request.Request(facts["url"], headers={"User-Agent": "DotAi-native-release"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, staged.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
        except OSError as exc:
            raise runtime.DotAiError(f"Unable to download frozen native release: {exc}") from exc
        if digest.hexdigest() != facts["sha256"]:
            raise runtime.DotAiError("Native release checksum mismatch; previous binary preserved")
        staged.chmod(0o755)
        try:
            probe_env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
            result = subprocess.run([str(staged), "--version"], env=probe_env, cwd=directory, stdin=subprocess.DEVNULL,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise runtime.DotAiError(f"Verified native release cannot start; previous binary preserved: {exc}") from exc
        versions = set(re.findall(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)(?![\w.+-])", result.stdout + "\n" + result.stderr))
        if result.returncode != 0 or versions != {facts["version"]}:
            raise runtime.DotAiError("Verified native release cannot prove its exact version; previous binary preserved")
        os.replace(staged, target)
