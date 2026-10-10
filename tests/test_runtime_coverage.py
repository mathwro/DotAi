from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import lifecycle, packages, reconcile, runtime, terminal


class RuntimeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = os.environ.copy()
        for key in ("DOTAI_PLATFORM", "LOCALAPPDATA", "APPDATA", "SCOOP"):
            environment.pop(key, None)
        environment.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state"),
            "DOTAI_CONFIG_DIR": str(self.home / "config"), "XDG_CONFIG_HOME": str(self.home / "xdg-config"),
            "XDG_STATE_HOME": str(self.home / "xdg"), "GH_HOST": "github.com",
            "NO_COLOR": "1",
        })
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)
        self.platform = "windows" if os.name == "nt" else "linux"

    def python(self, code: str) -> list[str]:
        return [sys.executable, "-c", code]

    def marker_command(self, path: Path, value: str) -> list[str]:
        return self.python(f"from pathlib import Path; Path({str(path)!r}).write_text({value!r}, encoding='utf-8')")

    def manifest(self, package: dict) -> dict:
        return {"version": 2, "prerequisites": [], "packages": [package], "skills": [], "marketplaces": [],
        "plugins": [], "ompExtensions": [],
        "mcp": {"target": str(self.home / "mcp.json"), "servers": {}},}

    def test_nonzero_capture_reports_both_streams_and_only_required_failures(self) -> None:
        command = self.python("import sys; print('stdout-detail'); print('stderr-detail', file=sys.stderr); sys.exit(7)")
        for required in (False, True):
            with self.subTest(required=required):
                runner = runtime.Runner(self.platform)
                report = io.StringIO()
                with contextlib.redirect_stdout(report):
                    result = runner.run(command, "failing tool", capture=True, required=required)
                self.assertEqual(result.returncode, 7)
                self.assertIn("stdout-detail", result.stdout)
                self.assertIn("stderr-detail", result.stderr)
                self.assertNotIn("stderr-detail", result.stdout)
                self.assertNotIn("stdout-detail", result.stderr)
                self.assertIn("failing tool", report.getvalue())
                self.assertIn("stderr-detail", report.getvalue())
                self.assertEqual(len(runner.failures), int(required))
                if required:
                    self.assertIn("exit 7", runner.failures[0])
                    self.assertIn("stderr-detail", runner.failures[0])

    def test_nonexistent_executable_records_only_required_failure(self) -> None:
        command = [str(self.home / "nonexistent-executable")]
        for required in (False, True):
            with self.subTest(required=required):
                runner = runtime.Runner(self.platform)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(runner.run(command, "missing tool", required=required))
                self.assertEqual(len(runner.failures), int(required))
                self.assertFalse(runner.succeeds(command))
                self.assertEqual(runner.output(command), "")

    def test_probes_reject_nonzero_stdout_and_output_excludes_stderr(self) -> None:
        runner = runtime.Runner(self.platform)
        failed = self.python("import sys; print('not-valid-data'); sys.exit(3)")
        self.assertFalse(runner.succeeds(failed))
        self.assertEqual(runner.output(failed), "")
        self.assertEqual(runner.output(self.python("import sys; print('valid-data'); print('warning', file=sys.stderr)")), "valid-data")
        self.assertTrue(runner.succeeds(self.python("pass")))
        self.assertEqual(runner.failures, [])

    def test_verbose_capture_reveals_output_without_failure(self) -> None:
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                runner = runtime.Runner(self.platform, verbose=verbose)
                report = io.StringIO()
                # Keep the payload out of the displayed command so its presence proves output rendering.
                script = self.home / "output.py"
                script.write_text("print('captured-' + 'payload')\n", encoding="utf-8")
                with contextlib.redirect_stdout(report):
                    result = runner.run([sys.executable, str(script)], "output tool", capture=True)
                self.assertEqual(result.stdout.strip(), "captured-payload")
                self.assertEqual("captured-payload" in report.getvalue(), verbose)
                self.assertEqual(runner.failures, [])

    def test_dry_run_does_not_execute_or_record_nonexistent_command(self) -> None:
        marker = self.home / "dry-marker"
        runner = runtime.Runner(self.platform, dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(runner.run(self.marker_command(marker, "changed"), "mutation"))
            self.assertIsNone(runner.run([str(self.home / "missing")], "missing"))
        self.assertFalse(marker.exists())
        self.assertEqual(runner.failures, [])

    @unittest.skipIf(os.name == "nt", "POSIX /bin/sh execution requires a POSIX host")
    def test_posix_shell_string_expands_environment_and_sequences_commands(self) -> None:
        marker = self.home / "shell marker"
        runner = runtime.Runner("linux")
        runner.env["DOTAI_MARKER_VALUE"] = "value with spaces"
        command = f'printf "%s" "$DOTAI_MARKER_VALUE" > {shlex.quote(str(marker))}; printf shell-output'
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(command, "shell marker", capture=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "shell-output")
        self.assertEqual(marker.read_text(encoding="utf-8"), "value with spaces")
        self.assertEqual(runner.failures, [])

    @unittest.skipUnless(os.name == "nt", "Real PowerShell and registry PATH require native Windows")
    def test_native_windows_shell_marker_preserves_inherited_path_after_refresh(self) -> None:
        marker = self.home / "powershell marker"
        runner = runtime.Runner("windows")
        inherited = str(self.home / "inherited-path-entry")
        runner.env["PATH"] = inherited + os.pathsep + runner.env["PATH"]
        runner.env["DOTAI_MARKER_VALUE"] = "native-value"
        escaped = str(marker).replace("'", "''")
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(f"[IO.File]::WriteAllText('{escaped}', $env:DOTAI_MARKER_VALUE); Write-Output 'native-output'", "native shell", capture=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "native-value")
        self.assertIn("native-output", result.stdout)
        self.assertIn(inherited, runner.env["PATH"].split(os.pathsep))
        registry = subprocess.run([
            "powershell.exe", "-NoProfile", "-Command",
            "[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')",
        ], text=True, capture_output=True, check=False)
        self.assertEqual(registry.returncode, 0)
        self.assertTrue(runner.env["PATH"].startswith(registry.stdout.strip() + os.pathsep))
        child_path = runner.output(self.python("import os; print(os.environ['PATH'])"))
        self.assertEqual(child_path, runner.env["PATH"])
        self.assertEqual(runner.failures, [])

    def test_windows_path_refresh_reaches_real_child_and_preserves_inherited_path(self) -> None:
        runner = runtime.Runner("windows")
        original_path = runner.env["PATH"]
        new_path = str(self.home / "new-shims")
        real_run = subprocess.run

        def windows_boundary(command, **kwargs):
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, new_path + "\n")
            return real_run(command, **kwargs)

        with mock.patch.object(runtime.subprocess, "run", side_effect=windows_boundary), contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(self.python("print('installed')"), "synthetic installer", capture=True)
            child_path = runner.output(self.python("import os; print(os.environ['PATH'])"))
        self.assertEqual(result.stdout.strip(), "installed")
        self.assertEqual(child_path, new_path + os.pathsep + original_path)
        self.assertEqual(runner.failures, [])

    def test_repeated_windows_refresh_keeps_registry_paths_once(self) -> None:
        runner = runtime.Runner("windows")
        inherited = str(self.home / "inherited")
        shared = str(self.home / "shared")
        installed = str(self.home / "new-shims")
        runner.env["PATH"] = os.pathsep.join([inherited, shared])
        registry_path = os.pathsep.join([shared, installed])
        expected = os.pathsep.join([shared, installed, inherited])
        real_run = subprocess.run

        def windows_boundary(command, **kwargs):
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, registry_path + "\n")
            return real_run(command, **kwargs)

        with mock.patch.object(runtime.subprocess, "run", side_effect=windows_boundary), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                result = runner.run(self.python("print('refreshed')"), "refresh", capture=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(runner.output(self.python("import os; print(os.environ['PATH'])")), expected)
        self.assertEqual(runner.failures, [])

    def test_windows_refresh_failure_does_not_break_successful_command(self) -> None:
        for outcome in (OSError("PowerShell unavailable"), (1, "bad-path"), (0, " \n")):
            with self.subTest(outcome=outcome):
                runner = runtime.Runner("windows")
                original_path = runner.env["PATH"]
                real_run = subprocess.run

                def windows_boundary(command, **kwargs):
                    if command[0] == "powershell.exe":
                        if isinstance(outcome, OSError):
                            raise outcome
                        return subprocess.CompletedProcess(command, *outcome)
                    return real_run(command, **kwargs)

                with mock.patch.object(runtime.subprocess, "run", side_effect=windows_boundary), contextlib.redirect_stdout(io.StringIO()):
                    result = runner.run(self.python("print('success')"), "tool", capture=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(runner.env["PATH"], original_path)
                self.assertEqual(runner.failures, [])

    def test_platform_detection_precedence_uses_controlled_os_sources(self) -> None:
        # Simultaneous lower-priority sources make ordering errors observable.
        cases = [
            ("custom", "nt", "darwin", "Microsoft", True, "Ubuntu", "custom"),
            (None, "nt", "darwin", "Microsoft", True, "Ubuntu", "windows"),
            (None, "posix", "darwin", "Microsoft", True, "Ubuntu", "macos"),
            (None, "posix", "linux", "MICROSOFT WSL", True, "Ubuntu", "wsl"),
            (None, "posix", "linux", "Linux", True, "Ubuntu", "arch"),
            (None, "posix", "linux", "Linux", False, 'ID="Ubuntu"', "ubuntu"),
            (None, "posix", "linux", None, False, "ID=debian", "ubuntu"),
            (None, "posix", "linux", "Linux", False, "ID=fedora", "linux"),
            (None, "posix", "linux", None, False, None, "linux"),
        ]
        for override, os_name, system, proc, arch, release, expected in cases:
            with self.subTest(expected=expected, release=release):
                sources = self.home / "sources"
                sources.mkdir(exist_ok=True)
                mapping = {name: sources / filename for name, filename in (
                    ("/proc/version", "version"), ("/etc/arch-release", "arch"), ("/etc/os-release", "release"),
                )}
                for path, content in ((mapping["/proc/version"], proc), (mapping["/etc/arch-release"], "" if arch else None), (mapping["/etc/os-release"], release)):
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(content, encoding="utf-8")
                environment = dict(os.environ)
                if override:
                    environment["DOTAI_PLATFORM"] = override
                with mock.patch.object(runtime, "os", SimpleNamespace(name=os_name, environ=environment)), mock.patch.object(runtime.sys, "platform", system), mock.patch.object(runtime, "Path", side_effect=lambda value: mapping[value]):
                    self.assertEqual(runtime.detect_platform(), expected)

    def test_state_directory_override_and_native_family_fallbacks(self) -> None:
        cases = [
            ("nt", {"DOTAI_STATE_DIR": str(self.home / "override"), "LOCALAPPDATA": str(self.home / "local")}, self.home / "override"),
            ("posix", {"DOTAI_STATE_DIR": str(self.home / "override"), "XDG_STATE_HOME": str(self.home / "xdg")}, self.home / "override"),
            ("nt", {"LOCALAPPDATA": str(self.home / "local")}, self.home / "local" / "DotAi"),
            ("nt", {}, self.home / "AppData" / "Local" / "DotAi"),
            ("posix", {"XDG_STATE_HOME": str(self.home / "xdg")}, self.home / "xdg" / "dotai"),
            ("posix", {}, self.home / ".local" / "state" / "dotai"),
        ]
        for os_name, values, expected in cases:
            with self.subTest(os_name=os_name, values=values):
                environment = {"DOTAI_HOME": str(self.home), **values}
                with mock.patch.object(runtime, "os", SimpleNamespace(name=os_name, environ=environment)):
                    self.assertEqual(runtime.state_dir(), expected)

    def test_platform_selection_executes_most_specific_available_command(self) -> None:
        marker = self.home / "selected"
        commands = {key: self.marker_command(marker, key) for key in ("wsl", "ubuntu", "arch", "macos", "unix", "linux", "windows", "custom", "default")}
        cases = [
            ("wsl", ("wsl", "ubuntu", "linux", "default")),
            ("ubuntu", ("ubuntu", "linux", "default")),
            ("arch", ("arch", "linux", "default")),
            ("macos", ("macos", "unix", "default")),
            ("windows", ("windows", "default")),
            ("linux", ("linux", "default")),
            ("custom", ("custom", "default")),
        ]
        runner = runtime.Runner(self.platform)
        for platform, precedence in cases:
            available = dict(commands)
            for expected in precedence:
                with self.subTest(platform=platform, expected=expected):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = runner.run(runtime.selected(available, platform), "selected", capture=True)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(marker.read_text(encoding="utf-8"), expected)
                    available.pop(expected)
            self.assertEqual(runtime.selected(available, platform), [])
        self.assertEqual(runtime.selected({"linux": [], "default": ["not-used"]}, "linux"), [])


    def test_package_version_failures_are_unhealthy_with_useful_reports(self) -> None:
        cases = [
            (self.python("print('tool 1.9.9')"), "tool 1.9.9"),
            (self.python("print('version unknown')"), "version unknown"),
            (self.python("import sys; print('tool 9.0.0'); sys.exit(4)"), "tool 9.0.0"),
            ([str(self.home / "missing-version-tool")], "not found"),
            ([], "not found"),
        ]
        runner = runtime.Runner(self.platform)
        for command, report in cases:
            with self.subTest(report=report, command=command):
                package = {"name": "tool", "check": command, "minimumVersion": "2.0"}
                self.assertEqual(packages.package_version_check(package, runner, command), (False, report))
                self.assertFalse(packages.package_check(package, runner))
        self.assertEqual(runner.failures, [])

    def test_package_version_unicode_read_error_is_unhealthy(self) -> None:
        command = self.python("import sys; sys.stdout.buffer.write(b'\\xff')")
        runner = runtime.Runner(self.platform)
        # Pin the decoding boundary only; the child and invalid bytes are real.
        real_run = subprocess.run

        def decode_as_utf8(*args, **kwargs):
            return real_run(*args, **kwargs, encoding="utf-8")

        with mock.patch.object(packages.subprocess, "run", side_effect=decode_as_utf8):
            self.assertEqual(packages.package_version_check({"minimumVersion": "2.0"}, runner, command), (False, "not found"))

    def test_reconciliation_failure_never_overwrites_previous_success_state(self) -> None:
        for failure in ("nonzero", "missing", "verification", "unsupported"):
            with self.subTest(failure=failure):
                state_dir = self.home / "state"
                state_dir.mkdir(exist_ok=True)
                saved = state_dir / "state.json"
                saved.write_text('{"lastSuccess":"previous"}\n', encoding="utf-8")
                marker = self.home / failure
                package = {"name": "synthetic", "managed": True, "check": self.python("import sys; sys.exit(1)"), "install": {"default": [self.marker_command(marker, "installed")]}}
                if failure == "nonzero":
                    package["install"]["default"] = [self.python("import sys; sys.exit(8)")]
                elif failure == "missing":
                    package["install"]["default"] = [[str(self.home / "missing-installer")]]
                elif failure == "unsupported":
                    package["install"] = {"linux" if self.platform == "windows" else "windows": [self.marker_command(marker, "wrong")]}
                manifest = self.manifest(package)
                path = self.home / "synthetic.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                runner = runtime.Runner(self.platform)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = reconcile.reconcile(manifest, path, runner, "update")
                self.assertEqual(result, 1)
                self.assertTrue(any("synthetic" in item for item in runner.failures))
                self.assertEqual(saved.read_text(encoding="utf-8"), '{"lastSuccess":"previous"}\n')
                self.assertFalse((state_dir / "recommended-skills.json").exists())
                self.assertEqual(marker.exists(), failure == "verification")

    def test_install_skip_and_package_without_update_still_configure_and_verify(self) -> None:
        for mode in ("install", "update"):
            with self.subTest(mode=mode):
                install_marker = self.home / (mode + "-install")
                configured = self.home / (mode + "-configured")
                package = {"name": "present", "managed": True, "check": self.python("print('present 1.0.0')"), "install": {"default": [self.marker_command(install_marker, "unexpected")]}, "update": {}, "configure": {"default": [self.marker_command(configured, "configured")]}}
                runner = runtime.Runner(self.platform)
                with contextlib.redirect_stdout(io.StringIO()):
                    packages.reconcile_packages(self.manifest(package), runner, mode)
                self.assertFalse(install_marker.exists())
                self.assertEqual(configured.read_text(encoding="utf-8"), "configured")
                self.assertEqual(runner.failures, [])

    def test_outdated_or_unparseable_present_package_uses_update_then_verifies(self) -> None:
        for initial in ("tool 1.0.0", "unknown"):
            with self.subTest(initial=initial):
                version_file = self.home / "version.txt"
                version_file.write_text(initial, encoding="utf-8")
                unexpected = self.home / "install"
                binary = self.home / "independent-tool"
                if os.name == "nt":
                    self.skipTest("Independent executable fixture requires a POSIX launcher")
                binary.write_text(f"#!/bin/sh\ncat {shlex.quote(str(version_file))}\n", encoding="utf-8")
                binary.chmod(0o755)
                check = [str(binary), "--version"]
                package = {"name": "tool", "managed": True, "minimumVersion": "2.0", "check": check, "install": {"default": [self.marker_command(unexpected, "wrong")]}, "update": {"default": [self.marker_command(version_file, "tool 2.0.0")]}}
                runner = runtime.Runner(self.platform)
                lifecycle.record_install("tool", "tool", package, self.manifest(package), runner)
                with contextlib.redirect_stdout(io.StringIO()):
                    packages.reconcile_packages(
                        self.manifest(package), runner, "install",
                        ownership_check=lambda value, current, operation_runner, operation: lifecycle.check_update_ownership(
                            "tool", value["name"], value, current, operation_runner, operation=operation),
                    )
                self.assertEqual(version_file.read_text(encoding="utf-8"), "tool 2.0.0")
                self.assertFalse(unexpected.exists())
                self.assertEqual(runner.failures, [])

    def test_missing_package_install_makes_verification_healthy(self) -> None:
        marker = self.home / "installed"
        check = self.python(f"from pathlib import Path; import sys; sys.exit(0 if Path({str(marker)!r}).exists() else 1)")
        package = {"name": "missing", "managed": True, "check": check, "install": {"default": [self.marker_command(marker, "installed")]}}
        runner = runtime.Runner(self.platform)
        with contextlib.redirect_stdout(io.StringIO()):
            packages.reconcile_packages(self.manifest(package), runner, "update")
        self.assertEqual(marker.read_text(encoding="utf-8"), "installed")
        self.assertTrue(packages.package_check(package, runner))
        self.assertEqual(runner.failures, [])

    def test_dry_reconciliation_preserves_files_and_success_state(self) -> None:
        marker = self.home / "not-installed"
        package = {"name": "missing", "managed": True, "check": self.python("import sys; sys.exit(1)"), "install": {"default": [self.marker_command(marker, "wrong")]}}
        manifest = self.manifest(package)
        path = self.home / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        runner = runtime.Runner(self.platform, dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()):
            result = reconcile.reconcile(manifest, path, runner, "update")
        self.assertEqual(result, 0)
        self.assertEqual(runner.failures, [])
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / "state").exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)


    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_missing_appdata_does_not_run_program_from_relative_npm_directory(self) -> None:
        folder = self.home / "npm"
        folder.mkdir()
        marker = self.home / "unmanaged-program-ran"
        program = folder / "dotai-relative-path-probe"
        program.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'unmanaged\\n'\n")
        program.chmod(0o755)
        previous = Path.cwd()
        try:
            os.chdir(self.home)
            with mock.patch.dict(os.environ):
                os.environ.pop("APPDATA", None)
                runner = runtime.Runner("linux")
                self.assertEqual(runner.output(["dotai-relative-path-probe"]), "")
        finally:
            os.chdir(previous)
        self.assertFalse(marker.exists())

if __name__ == "__main__":
    unittest.main()
