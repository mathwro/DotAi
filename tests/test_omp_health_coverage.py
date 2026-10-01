from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import health, omp, runtime, terminal


# Only the external OMP boundary is synthetic. Its registrations persist across
# processes, so an incorrect operation or scope changes observable user state.
OMP_COMMAND = r'''
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["DOTAI_HOME"])
project = Path(os.environ["OMP_TEST_PROJECT"])
args = sys.argv[1:]

def load(path):
    return json.loads(path.read_text()) if path.exists() else {}

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))

if args == ["config", "get", "extensions", "--json"]:
    if "OMP_TEST_CONFIG_OUTPUT" in os.environ:
        print(os.environ["OMP_TEST_CONFIG_OUTPUT"])
    else:
        config = load(home / ".omp" / "test-global.json")
        print(json.dumps({"key": "extensions", "value": config.get("extensions", [])}))
    sys.exit(0)
if args[:3] == ["config", "set", "extensions"] and len(args) == 4:
    if os.environ.get("OMP_TEST_FAIL") == "config-set":
        sys.exit(7)
    path = home / ".omp" / "test-global.json"
    config = load(path)
    config["extensions"] = json.loads(args[3])
    config["sets"] = config.get("sets", 0) + 1
    save(path, config)
    sys.exit(0)
if args[:2] == ["plugin", "marketplace"] and len(args) == 4:
    action, value = args[2:]
    if os.environ.get("OMP_TEST_FAIL") == "marketplace-" + action:
        sys.exit(7)
    path = home / ".omp" / "marketplaces.json"
    registry = load(path)
    records = registry.setdefault("marketplaces", {})
    if action == "add":
        records.setdefault(value.rsplit("/", 1)[-1], {"source": value, "revision": 1})
    elif action == "update" and value in records:
        records[value]["revision"] += 1
    else:
        sys.exit(64)
    save(path, registry)
    sys.exit(0)
if args[:1] == ["plugin"] and len(args) >= 5:
    action = args[1]
    if os.environ.get("OMP_TEST_FAIL") == "plugin-" + action:
        sys.exit(7)
    scope = args[args.index("--scope") + 1]
    if scope not in ("user", "project"):
        sys.exit(64)
    path = (home if scope == "user" else project) / ".omp" / "plugins" / "installed_plugins.json"
    registry = load(path)
    records = registry.setdefault("plugins", {})
    plugin_id = args[-1]
    if action == "install" and "--force" in args:
        records.setdefault(plugin_id, {"revision": 1})
    elif action == "upgrade" and plugin_id in records:
        records[plugin_id]["revision"] += 1
    else:
        sys.exit(64)
    save(path, registry)
    sys.exit(0)
sys.exit(64)
'''


def route_external_commands(real_run, fixture_bin: Path):
    """Run only synthetic OMP/managers as Python processes on every OS."""
    def run(command, *args, **kwargs):
        if isinstance(command, list) and command[0] in ("omp", "scoop", "brew", "apt-get", "pacman"):
            fixture = fixture_bin / (command[0] + ".py")
            if not fixture.is_file():
                raise FileNotFoundError(command[0])
            command = [sys.executable, str(fixture), *command[1:]]
        return real_run(command, *args, **kwargs)
    return run


class OmpHealthCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "home"
        self.project = self.directory / "project"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.project.mkdir()
        environment = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "DOTAI_HOME": str(self.home),
            "DOTAI_STATE_DIR": str(self.directory / "state"),
            "XDG_STATE_HOME": str(self.directory / "xdg-state"),
            "GH_HOST": "example.invalid",
            "DOTAI_PLATFORM": "linux",
            "SCOOP": str(self.home / "scoop"),
            "APPDATA": str(self.home / "appdata"),
            "PATH": str(self.bin),
            "PYTHONPATH": str(ROOT),
            "OMP_TEST_PROJECT": str(self.project),
        }
        if "SYSTEMROOT" in os.environ:
            environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        root_patcher = mock.patch.object(runtime, "ROOT", self.project)
        root_patcher.start()
        self.addCleanup(root_patcher.stop)
        color_patcher = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color_patcher.start()
        self.addCleanup(color_patcher.stop)
        command_patcher = mock.patch.object(
            subprocess, "run", new=route_external_commands(subprocess.run, self.bin)
        )
        command_patcher.start()
        self.addCleanup(command_patcher.stop)
        self.write_command("omp", OMP_COMMAND)
        self.manifest_path = self.project / "fixture-stack.json"
        self.manifest = {
            "version": 1,
            "packages": [],
            "skills": [],
            "marketplaces": [],
            "plugins": [],
            "ompExtensions": [],
            "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}},
        }
        self.config_path = self.home / ".omp" / "test-global.json"
        self.marketplace_path = self.home / ".omp" / "marketplaces.json"
        self.user_plugins = self.home / ".omp" / "plugins" / "installed_plugins.json"
        self.project_plugins = self.project / ".omp" / "plugins" / "installed_plugins.json"

    def write_command(self, name: str, body: str) -> None:
        path = self.bin / (name + ".py")
        path.write_text(body, encoding="utf-8")

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def run_cli(self, *arguments: str, platform: str = "linux") -> subprocess.CompletedProcess[str]:
        self.write_json(self.manifest_path, self.manifest)
        # Execute the real entry point after isolating project discovery and the
        # unrelated release-network boundary. Parser and dispatch remain real.
        bootstrap = (
            "import runpy, subprocess, sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(ROOT / 'tests')!r}); "
            "from test_omp_health_coverage import route_external_commands; "
            "from dotai_app import runtime, releases; "
            f"runtime.ROOT = Path({str(self.project)!r}); "
            f"subprocess.run = route_external_commands(subprocess.run, Path({str(self.bin)!r})); "
            "releases.latest_release_version = lambda: None; "
            f"runpy.run_path({str(ROOT / 'dotai.py')!r}, run_name='__main__')"
        )
        return subprocess.run(
            [sys.executable, "-c", bootstrap, "--manifest", str(self.manifest_path),
             "--platform", platform, "--color", "never", *arguments],
            cwd=self.project, env=os.environ.copy(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def declare_plugins(self) -> None:
        self.manifest["marketplaces"] = [{"name": "team", "source": "owner/team"}]
        self.manifest["plugins"] = [
            {"id": "review@team", "scope": "user"},
            {"id": "local@team", "scope": "project"},
            {"id": "default@team"},
        ]
        self.write_json(self.marketplace_path, {"marketplaces": {"private": {"source": "other/private", "revision": 9}}})
        self.write_json(self.user_plugins, {"plugins": {"private@private": {"revision": 9}}})
        self.write_json(self.project_plugins, {"plugins": {"local@private": {"revision": 8}}})

    def test_install_and_sync_preserve_registrations_and_plugin_scope(self) -> None:
        for mode in ("install", "sync"):
            with self.subTest(mode=mode):
                self.declare_plugins()
                result = self.run_cli(mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.read_json(self.marketplace_path), {"marketplaces": {
                    "private": {"source": "other/private", "revision": 9},
                    "team": {"source": "owner/team", "revision": 1},
                }})
                self.assertEqual(self.read_json(self.user_plugins), {"plugins": {
                    "private@private": {"revision": 9},
                    "review@team": {"revision": 1}, "default@team": {"revision": 1},
                }})
                self.assertEqual(self.read_json(self.project_plugins), {"plugins": {
                    "local@private": {"revision": 8}, "local@team": {"revision": 1},
                }})

    def test_update_refreshes_existing_records_without_reinstalling(self) -> None:
        self.declare_plugins()
        installed = self.run_cli("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        updated = self.run_cli("update")
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        self.assertEqual(self.read_json(self.marketplace_path), {"marketplaces": {
            "private": {"source": "other/private", "revision": 9},
            "team": {"source": "owner/team", "revision": 2},
        }})
        self.assertEqual(self.read_json(self.user_plugins), {"plugins": {
            "private@private": {"revision": 9},
            "review@team": {"revision": 2}, "default@team": {"revision": 2},
        }})
        self.assertEqual(self.read_json(self.project_plugins), {"plugins": {
            "local@private": {"revision": 8}, "local@team": {"revision": 2},
        }})

    def test_dry_run_leaves_registries_extensions_and_success_state_unchanged(self) -> None:
        self.declare_plugins()
        installed = self.run_cli("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.manifest["ompExtensions"] = ["~/preview.ts"]
        self.write_json(self.config_path, {"extensions": ["~/private.ts"], "sets": 0})
        paths = (self.marketplace_path, self.user_plugins, self.project_plugins,
                 self.config_path, Path(os.environ["DOTAI_STATE_DIR"]) / "state.json")
        before = {path: path.read_bytes() for path in paths}
        for mode in ("install", "sync", "update"):
            with self.subTest(mode=mode):
                result = self.run_cli(mode, "--dry-run")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[RUN]", result.stdout)
                self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_nonzero_marketplace_or_plugin_operation_fails_reconciliation(self) -> None:
        for operation, mode in (("marketplace-add", "sync"), ("plugin-install", "install"),
                                ("marketplace-update", "update"), ("plugin-upgrade", "update")):
            with self.subTest(operation=operation):
                self.declare_plugins()
                installed = self.run_cli("install")
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                prior_registry = (self.marketplace_path if operation.startswith("marketplace") else self.user_plugins).read_bytes()
                state_path = Path(os.environ["DOTAI_STATE_DIR"]) / "state.json"
                prior_state = state_path.read_bytes()
                with mock.patch.dict(os.environ, {"OMP_TEST_FAIL": operation}):
                    result = self.run_cli(mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("[FAIL]", result.stdout)
                self.assertEqual(state_path.read_bytes(), prior_state)
                registry = self.marketplace_path if operation.startswith("marketplace") else self.user_plugins
                self.assertEqual(registry.read_bytes(), prior_registry)

    def test_registry_matches_nested_exact_ids_not_substrings(self) -> None:
        path = self.directory / "registry.json"
        self.write_json(path, {"plugins": [{"records": {"review@team": {"metadata": ["local@team"]}}}]})
        for needle, expected in (("review@team", True), ("local@team", True),
                                 ("review", False), ("team", False), ("view@team", False)):
            with self.subTest(needle=needle):
                self.assertEqual(omp.registry_contains(path, needle), expected)

    def test_registry_missing_malformed_wrong_shape_and_unreadable_are_unhealthy(self) -> None:
        path = self.directory / "registry.json"
        self.assertFalse(omp.registry_contains(path, "review@team"))
        for payload in (b"{", b"[]", b"null", b'"review@team"', b"\xff"):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                self.assertFalse(omp.registry_contains(path, "review@team"))
        self.write_json(path, {"review@team": {}})
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("fixture denied")):
            self.assertFalse(omp.registry_contains(path, "review@team"))
        path.unlink()
        path.mkdir()
        self.assertFalse(omp.registry_contains(path, "review@team"))

    def test_extensions_keep_unrelated_entries_and_semantic_paths_without_repeat_mutation(self) -> None:
        existing = self.home / "extensions" / "present.ts"
        existing.parent.mkdir()
        existing.write_text("// fixture\n", encoding="utf-8")
        self.manifest["ompExtensions"] = ["~/extensions/present.ts", "~/extensions/new.ts"]
        self.write_json(self.config_path, {
            "extensions": ["~/private.ts", str(existing)], "unrelated": {"theme": "private"}, "sets": 0,
        })
        runner = runtime.Runner("linux")
        with contextlib.redirect_stdout(io.StringIO()):
            omp.reconcile_omp_extensions(self.manifest, runner)
        self.assertFalse(runner.failures)
        self.assertEqual(self.read_json(self.config_path), {
            "extensions": ["~/private.ts", str(existing), "~/extensions/new.ts"],
            "unrelated": {"theme": "private"}, "sets": 1,
        })
        snapshot = self.config_path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            omp.reconcile_omp_extensions(self.manifest, runner)
        self.assertFalse(runner.failures)
        self.assertEqual(self.config_path.read_bytes(), snapshot)

    def test_invalid_real_config_output_fails_without_destructive_set(self) -> None:
        self.manifest["ompExtensions"] = ["~/extensions/managed.ts"]
        self.write_json(self.config_path, {"extensions": ["~/private.ts"], "sets": 0})
        snapshot = self.config_path.read_bytes()
        for raw in ("", "{broken", "[]", "null", "3", "{}", '{"value":null}',
                    '{"value":"~/private.ts"}', '{"value":[42]}'):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"OMP_TEST_CONFIG_OUTPUT": raw}):
                runner = runtime.Runner("linux")
                with contextlib.redirect_stdout(io.StringIO()):
                    omp.reconcile_omp_extensions(self.manifest, runner)
                self.assertTrue(runner.failures)
                self.assertEqual(self.config_path.read_bytes(), snapshot)
                self.assertFalse(omp.omp_extension_status(self.manifest, runner)[0])

    def test_extension_registration_and_source_are_independently_required(self) -> None:
        source = self.home / "extensions" / "managed.ts"
        self.manifest["ompExtensions"] = ["~/extensions/managed.ts"]
        for registered, available, expected in ((False, False, False), (False, True, False),
                                                (True, False, False), (True, True, True)):
            with self.subTest(registered=registered, available=available):
                self.write_json(self.config_path, {"extensions": [str(source)] if registered else []})
                source.parent.mkdir(exist_ok=True)
                if available:
                    source.write_text("// fixture\n", encoding="utf-8")
                else:
                    source.unlink(missing_ok=True)
                healthy, detail = omp.omp_extension_status(self.manifest, runtime.Runner("linux"))
                self.assertEqual(healthy, expected, detail)
                if not expected:
                    self.assertIn("not configured" if not registered else "source missing", detail)

    def test_status_reports_package_marketplace_scopes_and_extension_health(self) -> None:
        self.declare_plugins()
        installed = self.run_cli("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.manifest["packages"] = [{"name": "Fixture tool", "check": [sys.executable, "-c", "print('ready')"], "install": {}}]
        healthy = self.run_cli("status")
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        for label in ("[OK] Fixture tool:", "[OK] team", "[OK] review@team", "[OK] local@team", "[INACTIVE]"):
            self.assertIn(label, healthy.stdout)
        # A project registration cannot satisfy a user plugin, or vice versa.
        self.write_json(self.user_plugins, {"plugins": {"local@team": {}, "default@team": {}}})
        self.write_json(self.project_plugins, {"plugins": {"review@team": {}}})
        wrong_scopes = self.run_cli("status")
        self.assertEqual(wrong_scopes.returncode, 1, wrong_scopes.stdout + wrong_scopes.stderr)
        self.assertIn("[MISSING] review@team", wrong_scopes.stdout)
        self.assertIn("[MISSING] local@team", wrong_scopes.stdout)
        self.manifest["packages"][0]["check"] = [sys.executable, "-c", "raise SystemExit(4)"]
        self.manifest["ompExtensions"] = ["~/missing.ts"]
        self.write_json(self.config_path, {"extensions": []})
        self.write_json(self.marketplace_path, {"marketplaces": {"team-extra": {}}})
        unhealthy = self.run_cli("status")
        self.assertEqual(unhealthy.returncode, 1, unhealthy.stdout + unhealthy.stderr)
        for label in ("[MISSING] Fixture tool:", "[MISSING] team", "OMP extensions:", "not configured", "[INACTIVE]"):
            self.assertIn(label, unhealthy.stdout)

    def test_doctor_uses_platform_manager_and_missing_manager_is_nonzero(self) -> None:
        for platform, manager in (("windows", "scoop"), ("macos", "brew"),
                                  ("ubuntu", "apt-get"), ("wsl", "apt-get"), ("arch", "pacman")):
            with self.subTest(platform=platform):
                self.write_command(manager, "import sys\nsys.exit(0 if sys.argv[1:] == ['--version'] else 64)\n")
                available = self.run_cli("doctor", platform=platform)
                self.assertEqual(available.returncode, 0, available.stdout + available.stderr)
                self.assertIn(f"[OK] {manager}", available.stdout)
                (self.bin / (manager + ".py")).unlink()
                missing = self.run_cli("doctor", platform=platform)
                self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
                self.assertIn(f"[FAIL] {manager}", missing.stdout)

    def test_generic_linux_doctor_needs_no_manager_and_retains_stack_failures(self) -> None:
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Package manager:", result.stdout)
        self.assertIn("[INACTIVE]", result.stdout)
        self.manifest["plugins"] = [{"id": "missing@team"}]
        unhealthy = self.run_cli("doctor")
        self.assertEqual(unhealthy.returncode, 1, unhealthy.stdout + unhealthy.stderr)
        self.assertIn("[MISSING] missing@team", unhealthy.stdout)
        self.assertIn("Config target:", unhealthy.stdout)

    def test_doctor_checks_permissions_of_nearest_existing_config_ancestor(self) -> None:
        self.manifest["mcp"]["target"] = str(self.home / "absent" / "nested" / "mcp.json")
        report = io.StringIO()
        # ACL behavior is an OS boundary; deny the existing ancestor only. A
        # check of either nonexistent directory would incorrectly report OK.
        with mock.patch.object(health.os, "access", side_effect=lambda path, mode: path != self.home), contextlib.redirect_stdout(report):
            healthy = health.doctor(self.manifest, runtime.Runner("linux"))
        self.assertFalse(healthy)
        self.assertIn("Config target:", report.getvalue())
        self.assertIn("[FAIL]", report.getvalue())
        self.assertFalse((self.home / "absent").exists())

    @unittest.skipUnless(sys.platform.startswith("linux") and Path("/sys").is_dir(), "Uses Linux read-only sysfs for a real unwritable ancestor")
    def test_doctor_rejects_unwritable_existing_ancestor_for_absent_config_path(self) -> None:
        if os.access("/sys", os.W_OK):
            self.skipTest("sysfs is writable on this host")
        target = Path("/sys") / f"dotai-fixture-{self.directory.name}" / "nested" / "mcp.json"
        self.manifest["mcp"]["target"] = str(target)
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Config target:", result.stdout)
        self.assertIn("[FAIL]", result.stdout)
        self.assertFalse(target.parent.exists())

    def test_platform_dispatch_does_not_require_or_initialize_manifest(self) -> None:
        for platform in ("windows", "wsl", "ubuntu", "arch", "macos", "linux"):
            with self.subTest(platform=platform):
                missing = self.project / "never-create.json"
                result = self.run_cli("--manifest", str(missing), "platform", platform=platform)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), platform)
                self.assertFalse(missing.exists())

    def test_enforce_without_recommended_skills_rejects_before_mutation(self) -> None:
        self.write_json(self.manifest_path, self.manifest)
        manifest_before = self.manifest_path.read_bytes()
        result = self.run_cli("sync", "--enforce")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--recommended-skills", result.stderr)
        self.assertEqual(self.manifest_path.read_bytes(), manifest_before)
        self.assertFalse(Path(os.environ["DOTAI_STATE_DIR"]).exists())
        self.assertFalse(self.marketplace_path.exists())
        self.assertFalse(self.user_plugins.exists())
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
