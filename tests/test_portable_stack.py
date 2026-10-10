"""Portable intent and reproducible execution boundary regressions."""
from __future__ import annotations

import copy
import importlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def feature(test, name):
    try:
        return importlib.import_module(f"dotai_app.{name}")
    except ModuleNotFoundError as exc:
        test.fail(f"Required {name} behavior is not implemented: {exc}")


class PortableCatalogTests(unittest.TestCase):
    def test_unknown_recipe_and_unsupported_pin_fail_before_execution(self):
        catalog = feature(self, "catalog")
        with self.assertRaisesRegex(RuntimeError, "recipe"):
            catalog.resolve_package({"name": "Mystery", "recipe": "missing", "managed": True})
        with self.assertRaisesRegex(RuntimeError, "pin"):
            catalog.resolve_package({"name": "RTK", "recipe": "rtk", "managed": True, "version": "1.2.3"})

    def test_custom_pin_requires_explicit_reviewed_pin_command(self):
        catalog = feature(self, "catalog")
        intent = {"name": "custom", "managed": True, "version": "2.3.4", "check": ["custom", "--version"], "install": [["custom-installer", "latest"]]}
        with self.assertRaisesRegex(RuntimeError, "pin"):
            catalog.resolve_package(intent)

    def test_default_paths_do_not_create_configuration(self):
        portable = feature(self, "portable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"DOTAI_CONFIG_DIR": str(root / "config")}, clear=True):
                self.assertEqual(portable.default_manifest_path(), root / "config" / "stack.json")
                self.assertEqual(portable.lock_path(root / "other.json"), root / "other.lock.json")
            self.assertFalse((root / "config").exists())

    def test_windows_and_xdg_paths_are_platform_specific(self):
        portable = feature(self, "portable")
        with patch.dict(os.environ, {"APPDATA": "/roaming", "XDG_CONFIG_HOME": "/xdg"}, clear=True):
            self.assertEqual(portable.default_manifest_path("windows"), Path("/roaming/DotAi/stack.json"))
            self.assertEqual(portable.default_manifest_path("linux"), Path("/xdg/dotai/stack.json"))


class StackLockTests(unittest.TestCase):
    def setUp(self):
        self.locking = feature(self, "locking")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "stack.json"
        self.path.write_text("{}")
        from dotai_app.runtime import Runner
        self.runner = Runner("linux")

    def package(self, name="Sample", version="1.2.3"):
        observed = self.root / f"{name}.version"
        return {
            "name": name, "managed": True, "version": version,
            "check": [sys.executable, "-c", f"from pathlib import Path; print(Path({str(observed)!r}).read_text())"],
            "pinInstall": [[sys.executable, "-c", f"from pathlib import Path; Path({str(observed)!r}).write_text('{{version}}')"]],
            "install": [[sys.executable, "-c", f"from pathlib import Path; Path({str(observed)!r}).write_text('1.2.3')"]],
        }

    def install(self, manifest, mode="install"):
        effective = self.locking.prepare(manifest, self.path, self.runner, mode)
        from dotai_app.runtime import selected, run_steps
        for package in effective["packages"]:
            if package.get("managed") and package.get("enabled", True):
                run_steps(selected(package["install"], self.runner.platform), self.runner, "Isolated fixture")
        self.locking.record(effective, self.path, self.runner, mode)
        return json.loads((self.root / "stack.lock.json").read_text())

    def test_stderr_version_probe_records_existing_target_without_installer(self):
        package = self.package()
        package["check"] = [sys.executable, "-c", "import sys; print('fixture 1.2.3', file=sys.stderr)"]
        intent = {"packages": [package]}
        self.locking.prepare(intent, self.path, self.runner, "install")
        self.locking.record(intent, self.path, self.runner, "install")
        facts = json.loads((self.root / "stack.lock.json").read_text())
        self.assertEqual(facts["packages"]["Sample"]["version"], "1.2.3")
        self.assertFalse((self.root / "Sample.version").exists())

    def test_lock_records_actual_successful_observations_privately(self):
        intent = {"packages": [self.package()]}
        original = copy.deepcopy(intent)
        lock = self.install(intent)
        self.assertEqual(lock["packages"]["Sample"]["version"], "1.2.3")
        self.assertEqual(intent, original)
        if os.name != "nt":
            self.assertEqual((self.root / "stack.lock.json").stat().st_mode & 0o777, 0o600)

    def test_dry_run_and_failed_reconciliation_leave_lock_bytes_unchanged(self):
        intent = {"packages": [self.package()]}
        self.install(intent)
        before = (self.root / "stack.lock.json").read_bytes()
        for dry_run in (True, False):
            self.runner.dry_run = dry_run
            effective = self.locking.prepare(intent, self.path, self.runner, "sync")
            if not dry_run:
                self.runner.failures.append("Fixture failure")
            self.locking.record(effective, self.path, self.runner, "sync")
            self.assertEqual((self.root / "stack.lock.json").read_bytes(), before)

    def test_selective_update_preserves_unrelated_records(self):
        a, b = self.package("A"), self.package("B")
        initial = self.install({"packages": [a, b]})
        a["version"] = "2.3.4"
        updated = self.install({"packages": [a]}, "update")
        self.assertEqual(updated["packages"]["A"]["version"], "2.3.4")
        self.assertEqual(updated["packages"]["B"], initial["packages"]["B"])

    def test_sync_reuses_resolved_version_instead_of_mutable_latest(self):
        package = self.package(version="latest")
        self.install({"packages": [package]})
        (self.root / "Sample.version").unlink()
        effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "sync")
        self.assertEqual(effective["packages"][0]["version"], "1.2.3")
        self.assertIn("1.2.3", effective["packages"][0]["install"][0][-1])

    def test_absent_latest_lock_and_stale_intent_refuse_sync_before_commands(self):
        package = self.package(version="latest")
        with self.assertRaisesRegex(RuntimeError, "lock|install|update"):
            self.locking.prepare({"packages": [package]}, self.path, self.runner, "sync")
        self.assertFalse((self.root / "Sample.version").exists())
        self.install({"packages": [package]})
        package["source"] = "https://example.invalid/different"
        with self.assertRaisesRegex(RuntimeError, "intent|stale|provenance"):
            self.locking.prepare({"packages": [package]}, self.path, self.runner, "sync")

    def test_mismatched_or_unobservable_versions_never_become_lock_facts(self):
        intent = {"packages": [self.package()]}
        effective = self.locking.prepare(intent, self.path, self.runner, "install")
        for observed in ("9.9.9", "unparseable"):
            (self.root / "Sample.version").write_text(observed)
            with self.assertRaisesRegex(RuntimeError, "version|observ"):
                self.locking.record(effective, self.path, self.runner, "install")
            self.assertFalse((self.root / "stack.lock.json").exists())

    def test_unsupported_pin_preflights_entire_selection(self):
        intent = {"packages": [self.package(), {"name": "RTK", "recipe": "rtk", "managed": True, "version": "1.2.3"}]}
        with self.assertRaisesRegex(RuntimeError, "pin"):
            self.locking.prepare(intent, self.path, self.runner, "install")
        self.assertFalse((self.root / "Sample.version").exists())

    def test_unmanaged_packages_never_receive_lock_records(self):
        package = self.package()
        package["managed"] = False
        self.install({"packages": [package]})
        lock = json.loads((self.root / "stack.lock.json").read_text())
        self.assertEqual(lock["packages"], {})
        self.assertFalse((self.root / "Sample.version").exists())

    def test_lock_changed_since_prepare_is_not_overwritten(self):
        intent = {"packages": [self.package()]}
        self.install(intent)
        effective = self.locking.prepare(intent, self.path, self.runner, "sync")
        target = self.root / "stack.lock.json"
        concurrent = json.loads(target.read_text())
        concurrent["external"] = "retained"
        target.write_text(json.dumps(concurrent))
        before = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed|concurrent"):
            self.locking.record(effective, self.path, self.runner, "sync")
        self.assertEqual(target.read_bytes(), before)

    def test_install_of_missing_component_reuses_existing_lock_target(self):
        package = self.package(version="latest")
        self.install({"packages": [package]})
        (self.root / "Sample.version").unlink()
        effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
        self.assertEqual(effective["packages"][0]["version"], "1.2.3")
        self.assertIn("1.2.3", effective["packages"][0]["install"][0][-1])

    def omp_release(self, version="18.8.7", digest="a" * 64):
        return {"tag_name": "v" + version, "draft": False, "prerelease": False, "assets": [
            {"name": "omp-linux-x64", "state": "uploaded", "digest": "sha256:" + digest,
             "browser_download_url": f"https://github.com/can1357/oh-my-pi/releases/download/v{version}/omp-linux-x64"}]}

    def native_intent(self, version="latest"):
        intent = json.loads((Path(__file__).resolve().parents[1] / "stack.example.json").read_text())
        intent["packages"] = [{"name": "OMP", "recipe": "omp", "managed": True, "version": version,
                               "check": [str(self.root.resolve() / ".local/bin/omp"), "--version"]}]
        self.path.write_text(json.dumps(intent))
        return intent

    def native_environment(self):
        home = str(self.root.resolve())
        return {"DOTAI_HOME": home, "HOME": home, "USERPROFILE": home,
                "DOTAI_STATE_DIR": home + "/state", "DOTAI_CONFIG_DIR": home + "/config",
                "PATH": home + "/.local/bin" + os.pathsep + os.environ.get("PATH", "")}

    @unittest.skipIf(os.name == "nt", "POSIX native release fixture")
    def test_omp_missing_lock_replay_installs_verified_frozen_binary(self):
        from dotai_app import catalog, packages, runtime
        payload = b"#!/bin/sh\nprintf 'omp 18.8.7\\n'\n"
        release = self.omp_release(digest=hashlib.sha256(payload).hexdigest())
        intent = self.native_intent()
        target = self.root / ".local/bin/omp"
        with patch.dict(os.environ, self.native_environment()), patch.object(catalog, "native_architecture", return_value="x64"):
            self.runner = runtime.Runner("linux")
            with patch.object(self.locking, "_fetch_json", return_value=release):
                effective = self.locking.prepare(intent, self.path, self.runner, "install")
            def apply(candidate):
                command = runtime.selected(candidate["packages"][0]["install"], "linux")[0]
                argv = self.runner.argv(command)
                with patch.object(sys, "argv", ["-c", *argv[3:]]), patch.object(packages.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(payload)):
                    exec(argv[2], {"__name__": "__main__"})
                self.locking.record(candidate, self.path, self.runner, "install")
            apply(effective)
            target.unlink()
            with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Replay must not follow mutable releases")):
                apply(self.locking.prepare(intent, self.path, self.runner, "install"))
        self.assertEqual(target.read_bytes(), payload)
        lock = json.loads((self.root / "stack.lock.json").read_text())["packages"]["OMP"]
        self.assertEqual(lock["version"], "18.8.7")
        self.assertEqual(lock["sourceMetadata"]["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "Native fixture requires a C compiler")
    def test_omp_native_update_records_actual_release_when_vendor_latest_advances(self):
        from dotai_app import catalog, lifecycle, reconcile, runtime
        intent = self.native_intent()
        target = self.root / ".local/bin/omp"
        target.parent.mkdir(parents=True)
        for binary, version in ((target, "18.8.6"), (Path(str(target) + ".next"), "18.8.8")):
            source = ('#include <stdio.h>\n#include <string.h>\n'
                      'int main(int argc,char **argv){if(argc>1&&!strcmp(argv[1],"update")){'
                      f'return rename({json.dumps(str(target.resolve()) + ".next")},{json.dumps(str(target.resolve()))})!=0;}}'
                      f'puts("omp {version}");return 0;}}')
            compiled = subprocess.run(["cc", "-x", "c", "-", "-o", str(binary)], input=source, text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
        actual_digest = hashlib.sha256(Path(str(target) + ".next").read_bytes()).hexdigest()
        def release(url):
            return self.omp_release("18.8.8", actual_digest) if "/tags/" in url else self.omp_release()
        with patch.dict(os.environ, self.native_environment()), patch.object(catalog, "native_architecture", return_value="x64"), patch.object(self.locking, "_fetch_json", side_effect=release):
            self.runner = runtime.Runner("linux")
            lifecycle.record_install("tool", "OMP", intent["packages"][0], intent, self.runner)
            self.assertEqual(reconcile.reconcile(intent, self.path, self.runner, "update"), 0)
        lock = json.loads((self.root / "stack.lock.json").read_text())["packages"]["OMP"]
        self.assertEqual(lock["version"], "18.8.8")
        self.assertEqual(lock["sourceMetadata"]["version"], "18.8.8")
        self.assertEqual(lock["sourceMetadata"]["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

    @unittest.skipIf(os.name == "nt", "POSIX version fixture")
    def test_native_lock_refuses_version_matching_binary_with_wrong_artifact_digest(self):
        from dotai_app import catalog, runtime
        intent = self.native_intent()
        target = self.root / ".local/bin/omp"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\nprintf 'omp 18.8.7\\n'\n")
        target.chmod(0o755)
        with patch.dict(os.environ, self.native_environment()), patch.object(catalog, "native_architecture", return_value="x64"), patch.object(self.locking, "_fetch_json", return_value=self.omp_release()):
            self.runner = runtime.Runner("linux")
            effective = self.locking.prepare(intent, self.path, self.runner, "update")
            with self.assertRaisesRegex(RuntimeError, "differs from the release artifact"):
                self.locking.record(effective, self.path, self.runner, "update")
        self.assertFalse((self.root / "stack.lock.json").exists())

    @unittest.skipIf(os.name == "nt", "POSIX native update fixture")
    def test_omp_native_update_refuses_different_exact_pin_before_changes(self):
        from dotai_app import runtime
        intent = self.native_intent("18.8.7")
        target = self.root / ".local/bin/omp"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\nprintf 'omp 18.8.6\\n'\n")
        target.chmod(0o755)
        before = target.read_bytes()
        with patch.dict(os.environ, self.native_environment()), patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Refusal precedes source lookup")):
            self.runner = runtime.Runner("linux")
            with self.assertRaisesRegex(RuntimeError, "install --force"):
                self.locking.prepare(intent, self.path, self.runner, "update")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.root / "stack.lock.json").exists())

    def test_omp_exact_missing_artifact_and_digest_refuse_before_mutation(self):
        from dotai_app import catalog
        intent = {"packages": [{"name": "OMP", "recipe": "omp", "managed": True, "version": "18.8.7"}]}
        for metadata in (self.omp_release(digest=""), {**self.omp_release(), "assets": []}, self.omp_release(version="18.8.8")):
            with patch.object(catalog, "native_architecture", return_value="x64"), patch.object(self.locking, "_package_version", return_value=None), patch.object(self.locking, "_fetch_json", return_value=metadata):
                with self.assertRaisesRegex(RuntimeError, "artifact|digest|version"):
                    self.locking.prepare(intent, self.path, self.runner, "install")
        self.assertFalse((self.root / "stack.lock.json").exists())

    def test_custom_recipe_pin_runs_declared_installer_without_unrelated_catalog_lookup(self):
        package = self.package()
        package.update({"recipe": "omp", "minimumVersion": "1.0.0"})
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Custom installer has no catalog-source resolution")):
            lock = self.install({"packages": [package]})
        self.assertEqual((self.root / "Sample.version").read_text(), "1.2.3")
        self.assertEqual(lock["packages"]["Sample"]["version"], "1.2.3")

    def source_recipe(self, version="latest"):
        package = self.package(version=version)
        package["recipe"] = "graphify"
        recipes = self.locking.catalog.load_catalog()
        recipes["recipes"]["graphify"]["versionMetadata"] = {
            "latest": "https://pypi.org/pypi/graphifyy/json",
            "exact": "https://pypi.org/pypi/graphifyy/{version}/json",
            "versionPath": ["info", "version"],
            "pythonRequirementPath": ["info", "requires_python"],
        }
        package["versionMetadata"] = copy.deepcopy(recipes["recipes"]["graphify"]["versionMetadata"])
        return package, recipes

    def test_mutable_package_metadata_resolves_exact_supported_install(self):
        package, recipes = self.source_recipe()
        metadata = {"info": {"version": "2.3.4", "requires_python": ">=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            lock = self.install({"packages": [package]})
        self.assertEqual(lock["packages"]["Sample"]["version"], "2.3.4")
        self.assertEqual((self.root / "Sample.version").read_text(), "2.3.4")

    def test_exact_source_version_mismatch_refuses_before_any_install(self):
        package, recipes = self.source_recipe("1.2.3")
        metadata = {"info": {"version": "9.9.9", "requires_python": ">=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, "source|version"):
                self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
        self.assertFalse((self.root / "Sample.version").exists())

    def test_incompatible_source_python_refuses_without_downloading_interpreter(self):
        package, recipes = self.source_recipe()
        metadata = {"info": {"version": "2.3.4", "requires_python": ">=99.0"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, "Python|python"):
                self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
        self.assertFalse((self.root / "Sample.version").exists())

    def test_unsupported_python_requirement_is_not_treated_as_compatible(self):
        package, recipes = self.source_recipe()
        metadata = {"info": {"version": "2.3.4", "requires_python": "~=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, "unsupported|requirement"):
                self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")

    def test_healthy_install_reuses_observed_source_without_network(self):
        package, recipes = self.source_recipe()
        (self.root / "Sample.version").write_text("1.2.3")
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Healthy install must not resolve latest")):
            effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
            self.assertEqual(effective["packages"][0]["version"], "1.2.3")
            self.locking.record(effective, self.path, self.runner, "install")

    def test_force_install_preflights_remote_source_before_exact_replacement(self):
        package, recipes = self.source_recipe()
        (self.root / "Sample.version").write_text("1.2.3")
        metadata = {"info": {"version": "2.3.4", "requires_python": ">=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "install", force=True)
        self.assertEqual(effective["packages"][0]["version"], "2.3.4")

    def test_frozen_reviewed_plan_does_not_resolve_a_moving_source_again(self):
        package, recipes = self.source_recipe()
        intent = {"packages": [package]}
        metadata = {"info": {"version": "2.3.4", "requires_python": ">=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            self.locking.prepare(intent, self.path, self.runner, "install")
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Reviewed source moved; must use frozen target")):
            effective = self.locking.prepared(intent, self.path, self.runner, "install")
        self.assertEqual(effective["packages"][0]["version"], "2.3.4")
        changed = copy.deepcopy(intent)
        changed["packages"][0]["source"] = "https://example.invalid/changed"
        self.assertIsNone(self.locking.prepared(changed, self.path, self.runner, "install"))

    def test_exact_current_update_checks_python_before_updater_mutation(self):
        package, recipes = self.source_recipe("1.2.3")
        observed = self.root / "Sample.version"
        observed.write_text("1.2.3")
        marker = self.root / "updated"
        package["pinInstall"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(observed)!r}).write_text('{{version}}'); Path({str(marker)!r}).write_text('updated')"]]
        metadata = {"info": {"version": "1.2.3", "requires_python": ">=99.0"}}
        from dotai_app.runtime import run_steps, selected
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, "Python|python"):
                effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "update")
                run_steps(selected(effective["packages"][0]["update"], self.runner.platform), self.runner, "Update fixture")
        self.assertFalse(marker.exists())
        self.assertEqual(observed.read_text(), "1.2.3")

    def test_below_minimum_install_resolves_supported_target_not_unhealthy_current(self):
        package, recipes = self.source_recipe()
        package["minimumVersion"] = "2.0"
        (self.root / "Sample.version").write_text("1.2.3")
        metadata = {"info": {"version": "2.3.4", "requires_python": ">=3.10"}}
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", return_value=metadata):
            effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
            from dotai_app.runtime import run_steps, selected
            run_steps(selected(effective["packages"][0]["install"], self.runner.platform), self.runner, "Upgrade fixture")
            self.locking.record(effective, self.path, self.runner, "install")
        self.assertEqual((self.root / "Sample.version").read_text(), "2.3.4")
        self.assertEqual(self.locking.load(self.path)["packages"]["Sample"]["version"], "2.3.4")

    def test_healthy_install_preserves_newer_current_than_existing_lock(self):
        package = self.package(version="latest")
        self.install({"packages": [package]})
        (self.root / "Sample.version").write_text("2.3.4")
        effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, "install")
        self.locking.record(effective, self.path, self.runner, "install")
        self.assertEqual((self.root / "Sample.version").read_text(), "2.3.4")
        self.assertEqual(self.locking.load(self.path)["packages"]["Sample"]["version"], "2.3.4")

    def test_exact_current_sync_and_pinned_update_need_no_remote_resolution(self):
        package, recipes = self.source_recipe("1.2.3")
        package["updatePolicy"] = "pinned"
        (self.root / "Sample.version").write_text("1.2.3")
        with patch.object(self.locking.catalog, "load_catalog", return_value=recipes), patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Unchanged pinned source must not resolve remotely")):
            for mode in ("sync", "update"):
                effective = self.locking.prepare({"packages": [package]}, self.path, self.runner, mode)
                self.locking.record(effective, self.path, self.runner, mode)
        self.assertEqual((self.root / "Sample.version").read_text(), "1.2.3")

    def test_frozen_plan_rejects_changed_sidecar_before_installer(self):
        intent = {"packages": [self.package()]}
        self.locking.prepare(intent, self.path, self.runner, "install")
        target = self.root / "stack.lock.json"
        concurrent = {"version": 1, "packages": {}, "skills": {}, "plugins": {}, "external": "retained"}
        target.write_text(json.dumps(concurrent))
        before = target.read_bytes()
        from dotai_app.runtime import run_steps, selected
        with self.assertRaisesRegex(RuntimeError, "changed|concurrent"):
            effective = self.locking.prepared(intent, self.path, self.runner, "install")
            for package in effective["packages"]:
                run_steps(selected(package["install"], self.runner.platform), self.runner, "Install fixture")
        self.assertFalse((self.root / "Sample.version").exists())
        self.assertEqual(target.read_bytes(), before)

    def test_execution_rechecks_sidecar_after_prepared_before_installer(self):
        intent = {"packages": [self.package()]}
        self.locking.prepare(intent, self.path, self.runner, "install")
        effective = self.locking.prepared(intent, self.path, self.runner, "install")
        target = self.root / "stack.lock.json"
        target.write_text(json.dumps({"version": 1, "packages": {}, "skills": {}, "plugins": {}, "external": "retained"}))
        from dotai_app.runtime import run_steps, selected
        with self.assertRaisesRegex(RuntimeError, "changed|concurrent"):
            with self.locking.execution(self.path, self.runner):
                run_steps(selected(effective["packages"][0]["install"], self.runner.platform), self.runner, "Install fixture")
        self.assertFalse((self.root / "Sample.version").exists())
        self.assertFalse((self.root / "stack.lock.json.write").exists())

    def test_overlapping_execution_refuses_second_mutation_and_retains_first_guard(self):
        from dotai_app.runtime import Runner, run_steps, selected
        intent = {"packages": [self.package()]}
        second = Runner("linux")
        self.locking.prepare(intent, self.path, self.runner, "install")
        effective = self.locking.prepare(intent, self.path, second, "install")
        guard = self.root / "stack.lock.json.write"
        with self.locking.execution(self.path, self.runner):
            if os.name != "nt":
                self.assertEqual(guard.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, "writer|active|exclusiv"):
                with self.locking.execution(self.path, second):
                    run_steps(selected(effective["packages"][0]["install"], second.platform), second, "Install fixture")
            self.assertTrue(guard.exists())
            self.assertFalse((self.root / "Sample.version").exists())
        self.assertFalse(guard.exists())

    def test_execution_releases_own_guard_after_action_exception(self):
        self.locking.prepare({"packages": []}, self.path, self.runner, "install")
        guard = self.root / "stack.lock.json.write"
        with self.assertRaisesRegex(ValueError, "fixture"):
            with self.locking.execution(self.path, self.runner):
                raise ValueError("fixture action failed")
        self.assertFalse(guard.exists())
        with self.locking.execution(self.path, self.runner):
            self.assertTrue(guard.exists())
        self.assertFalse(guard.exists())

    def test_execution_cleanup_preserves_replaced_guard(self):
        self.locking.prepare({"packages": []}, self.path, self.runner, "install")
        guard = self.root / "stack.lock.json.write"
        replacement = self.root / "replacement"
        replacement.write_text("another owner")
        with self.locking.execution(self.path, self.runner):
            os.replace(replacement, guard)
        self.assertEqual(guard.read_text(), "another owner")

    def test_execution_requires_preflight_and_creates_nothing_on_dry_run(self):
        missing = self.root / "missing" / "stack.json"
        with self.assertRaisesRegex(RuntimeError, "prepare|preflight"):
            with self.locking.execution(missing, self.runner):
                self.fail("Execution must not start without preflight")
        self.assertFalse(missing.parent.exists())
        self.runner.dry_run = True
        effective = self.locking.prepare({"packages": []}, missing, self.runner, "install")
        with self.locking.execution(missing, self.runner):
            self.assertFalse(self.locking.record(effective, missing, self.runner, "install"))
            self.assertFalse(missing.parent.exists())
        self.assertFalse(missing.parent.exists())

    def test_record_with_execution_lease_does_not_double_acquire_and_preserves_noop_bytes(self):
        intent = {"packages": [self.package()]}
        effective = self.locking.prepare(intent, self.path, self.runner, "install")
        from dotai_app.runtime import run_steps, selected
        guard = self.root / "stack.lock.json.write"
        target = self.root / "stack.lock.json"
        with self.locking.execution(self.path, self.runner):
            run_steps(selected(effective["packages"][0]["install"], self.runner.platform), self.runner, "Install fixture")
            self.assertTrue(self.locking.record(effective, self.path, self.runner, "install"))
            self.assertTrue(guard.exists())
        parsed = json.loads(target.read_text())
        target.write_text(json.dumps(parsed, separators=(",", ":")))
        before, modified = target.read_bytes(), target.stat().st_mtime_ns
        effective = self.locking.prepare(intent, self.path, self.runner, "sync")
        with self.locking.execution(self.path, self.runner):
            self.assertFalse(self.locking.record(effective, self.path, self.runner, "sync"))
            self.assertTrue(guard.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertFalse(guard.exists())

    def test_independent_record_refuses_existing_guard_without_removing_it(self):
        intent = {"packages": [self.package()]}
        self.install(intent)
        effective = self.locking.prepare(intent, self.path, self.runner, "sync")
        guard = self.root / "stack.lock.json.write"
        guard.write_text("another writer")
        with self.assertRaisesRegex(RuntimeError, "writer|active|exclusiv"):
            self.locking.record(effective, self.path, self.runner, "sync")
        self.assertEqual(guard.read_text(), "another writer")


class SkillLockTests(unittest.TestCase):
    def setUp(self):
        self.locking = feature(self, "locking")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "stack.json"
        self.path.write_text("{}")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"DOTAI_HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state")}).start()
        from dotai_app.runtime import Runner
        self.runner = Runner("linux")
        self.source = {"source": "example/reviewed", "agent": "universal", "skills": ["alpha"], "checkSkills": ["alpha"]}
        self.revision = "a" * 40
        import hashlib
        blob = hashlib.sha1(b"blob 8\0# Alpha\n").digest()
        tree = b"100644 SKILL.md\0" + blob
        self.tree = hashlib.sha1(b"tree " + str(len(tree)).encode() + b"\0" + tree).hexdigest()
        self.responses = {
            "https://registry.npmjs.org/skills/latest": {"name": "skills", "version": "1.7.1", "engines": {"node": ">=22.20.0"}},
            "https://api.github.com/repos/example/reviewed/commits/HEAD": {"sha": self.revision, "commit": {"tree": {"sha": "b" * 40}}},
            "https://api.github.com/repos/example/reviewed/git/trees/" + "b" * 40 + "?recursive=1": {"truncated": False, "tree": [{"path": "skills/alpha", "type": "tree", "sha": self.tree}]},
        }

    def fetch(self, url):
        if url not in self.responses:
            raise RuntimeError(f"Unplanned external retrieval: {url}")
        return copy.deepcopy(self.responses[url])

    def observed(self, command):
        if command == ["node", "--version"]:
            return "v22.20.0"
        if command == ["npx", "--yes", "skills@1.7.1", "--version"]:
            return "1.7.1"
        raise RuntimeError(f"Unplanned external probe: {command}")

    def install_copy(self):
        from dotai_app import skills
        folder = skills.skill_root(self.source) / "alpha"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_bytes(b"# Alpha\n")
        upstream = self.root / "state" / "skills" / ".skill-lock.json"
        upstream.parent.mkdir(parents=True)
        upstream.write_text(json.dumps({"version": 3, "skills": {"alpha": {
            "source": "example/reviewed", "sourceType": "github",
            "sourceUrl": "https://github.com/example/reviewed", "ref": self.revision,
            "skillPath": "skills/alpha/SKILL.md", "skillFolderHash": self.tree,
            "installedAt": "2026-10-08T00:00:00Z", "updatedAt": "2026-10-08T00:00:00Z",
        }}}))

    def test_skill_ref_and_installer_resolve_immutably_then_sync_needs_no_network(self):
        intent = {"packages": [], "skills": [self.source]}
        with patch.object(self.locking, "_fetch_json", side_effect=self.fetch), patch.object(self.runner, "output", side_effect=self.observed):
            effective = self.locking.prepare(intent, self.path, self.runner, "install")
            self.assertEqual(effective["skills"][0]["revision"], self.revision)
            self.assertEqual(effective["skills"][0]["installerVersion"], "1.7.1")
            self.install_copy()
            self.locking.record(effective, self.path, self.runner, "install")
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Unchanged sync must not retrieve mutable HEAD")):
            effective = self.locking.prepare(intent, self.path, self.runner, "sync")
            self.assertEqual(effective["skills"][0]["revision"], self.revision)
            self.assertEqual(effective["skills"][0]["installerVersion"], "1.7.1")

    def test_skill_retrieval_failure_has_no_latest_fallback_or_lock_write(self):
        with patch.object(self.locking, "_fetch_json", side_effect=RuntimeError("network unavailable")):
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                self.locking.prepare({"packages": [], "skills": [self.source]}, self.path, self.runner, "install")
        self.assertFalse((self.root / "stack.lock.json").exists())

    def test_modified_skill_content_cannot_be_recorded_as_resolved_revision(self):
        intent = {"packages": [], "skills": [self.source]}
        with patch.object(self.locking, "_fetch_json", side_effect=self.fetch), patch.object(self.runner, "output", side_effect=self.observed):
            effective = self.locking.prepare(intent, self.path, self.runner, "install")
            self.install_copy()
            from dotai_app import skills
            (skills.skill_root(self.source) / "alpha" / "SKILL.md").write_text("locally modified")
            with self.assertRaisesRegex(RuntimeError, "tree|provenance|modified"):
                self.locking.record(effective, self.path, self.runner, "install")
        self.assertFalse((self.root / "stack.lock.json").exists())

    def establish_lock(self):
        intent = {"packages": [], "skills": [self.source]}
        with patch.object(self.locking, "_fetch_json", side_effect=self.fetch), patch.object(self.runner, "output", side_effect=self.observed):
            effective = self.locking.prepare(intent, self.path, self.runner, "install")
            self.install_copy()
            self.locking.record(effective, self.path, self.runner, "install")
        return intent

    def test_external_lock_tree_keys_cannot_traverse_before_hashing(self):
        intent = self.establish_lock()
        target = self.root / "stack.lock.json"
        value = json.loads(target.read_text())
        value["skills"]["example/reviewed|universal"]["trees"]["../../outside"] = "d" * 40
        target.write_text(json.dumps(value))
        from dotai_app import skills
        with patch.object(skills, "installed_skill_tree_hash", side_effect=AssertionError("Unsafe lock reached hashing")):
            with self.assertRaisesRegex(RuntimeError, "unsafe|selection|provenance"):
                self.locking.prepare(intent, self.path, self.runner, "sync")

    def test_redirected_skill_root_is_not_immutable_install_proof(self):
        intent = self.establish_lock()
        from dotai_app import skills
        root = skills.skill_root(self.source)
        redirected = root.with_name("redirected")
        root.rename(redirected)
        try:
            root.symlink_to(redirected, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable")
        with self.assertRaisesRegex(RuntimeError, "linked|redirect|symlink"):
            self.locking.prepare(intent, self.path, self.runner, "sync")

    def test_adoption_receipt_can_prove_scope_but_still_requires_immutable_tree(self):
        intent = {"packages": [], "skills": [self.source]}
        self.install_copy()
        (self.root / "state" / "skills" / ".skill-lock.json").unlink()
        with patch.object(self.locking, "_fetch_json", side_effect=self.fetch), patch.object(self.runner, "output", side_effect=self.observed):
            effective = self.locking.prepare(intent, self.path, self.runner, "install", skill_receipt_owned=lambda skill: True)
            self.locking.record(effective, self.path, self.runner, "install")
        lock = json.loads((self.root / "stack.lock.json").read_text())
        self.assertEqual(lock["skills"]["example/reviewed|universal"]["revision"], "a" * 40)

    def test_explicit_selected_sync_refresh_advances_only_selected_skill_lock(self):
        intent = self.establish_lock()
        target = self.root / "stack.lock.json"
        before = json.loads(target.read_text())
        retained = copy.deepcopy(before["skills"]["example/reviewed|universal"])
        retained["source"] = "example/unrelated"
        before["skills"]["example/unrelated|universal"] = retained
        target.write_text(json.dumps(before))
        import hashlib
        content = b"# Alpha updated\n"
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).digest()
        raw_tree = b"100644 SKILL.md\0" + blob
        updated_tree = hashlib.sha1(b"tree " + str(len(raw_tree)).encode() + b"\0" + raw_tree).hexdigest()
        self.responses["https://api.github.com/repos/example/reviewed/commits/HEAD"]["sha"] = "c" * 40
        self.responses["https://api.github.com/repos/example/reviewed/git/trees/" + "b" * 40 + "?recursive=1"]["tree"][0]["sha"] = updated_tree
        with patch.object(self.locking, "_fetch_json", side_effect=self.fetch), patch.object(self.runner, "output", side_effect=self.observed):
            effective = self.locking.prepare(intent, self.path, self.runner, "sync", refresh_sources={"example/reviewed"}, skill_receipt_owned=lambda skill: True)
            self.assertEqual(effective["skills"][0]["revision"], "c" * 40)
            from dotai_app import skills
            folder = skills.skill_root(self.source) / "alpha"
            self.assertEqual((folder / "SKILL.md").read_text(), "# Alpha\n")
            (folder / "SKILL.md").write_bytes(content)
            upstream = self.root / "state" / "skills" / ".skill-lock.json"
            value = json.loads(upstream.read_text())
            value["skills"]["alpha"].update({"ref": "c" * 40, "skillFolderHash": updated_tree})
            upstream.write_text(json.dumps(value))
            self.locking.record(effective, self.path, self.runner, "sync")
        after = json.loads(target.read_text())
        self.assertEqual(after["skills"]["example/reviewed|universal"]["revision"], "c" * 40)
        self.assertEqual(after["skills"]["example/unrelated|universal"], retained)

    def test_explicit_skill_refresh_rejects_current_modified_unowned_copy(self):
        intent = self.establish_lock()
        from dotai_app import skills
        (skills.skill_root(self.source) / "alpha" / "SKILL.md").write_text("locally modified")
        before = (self.root / "stack.lock.json").read_bytes()
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Unowned copy must reject before target lookup")):
            with self.assertRaisesRegex(RuntimeError, "owned|provenance|modified"):
                self.locking.prepare(intent, self.path, self.runner, "sync", update_skills=True, skill_receipt_owned=lambda skill: False)
        self.assertEqual((self.root / "stack.lock.json").read_bytes(), before)

    def test_normal_install_repairs_owned_current_tree_to_existing_locked_target(self):
        intent = self.establish_lock()
        from dotai_app import skills
        folder = skills.skill_root(self.source) / "alpha"
        (folder / "SKILL.md").write_text("# Alpha updated\n")
        upstream = self.root / "state" / "skills" / ".skill-lock.json"
        value = json.loads(upstream.read_text())
        value["skills"]["alpha"].update({
            "ref": "c" * 40,
            "skillFolderHash": skills.installed_skill_tree_hash(folder),
        })
        upstream.write_text(json.dumps(value))
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Locked repair must not resolve latest")):
            effective = self.locking.prepare(intent, self.path, self.runner, "install", skill_receipt_owned=lambda skill: True)
        self.assertEqual(effective["skills"][0]["revision"], self.revision)
        self.assertTrue(self.runner._dotai_lock_plan["skills"]["example/reviewed|universal"]["refresh"])
        self.assertEqual((folder / "SKILL.md").read_text(), "# Alpha updated\n")

    def test_matching_existing_locked_skill_does_not_request_refresh(self):
        intent = self.establish_lock()
        with patch.object(self.locking, "_fetch_json", side_effect=AssertionError("Locked matching tree must not resolve latest")):
            effective = self.locking.prepare(intent, self.path, self.runner, "install")
        self.assertEqual(effective["skills"][0]["revision"], self.revision)
        self.assertFalse(self.runner._dotai_lock_plan["skills"]["example/reviewed|universal"]["refresh"])


class PluginLockTests(unittest.TestCase):
    def setUp(self):
        self.locking = feature(self, "locking")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"DOTAI_HOME": str(self.root)}).start()
        self.path = self.root / "stack.json"
        self.path.write_text("{}")
        from dotai_app.runtime import Runner
        self.runner = Runner("linux")
        self.plugin = {"id": "sample@reviewed", "scope": "user"}
        self.folder = self.root / ".omp" / "plugins" / "cache" / "plugins" / "sample"
        self.folder.mkdir(parents=True)
        (self.folder / "plugin.json").write_text('{"name":"sample","version":"1.2.3"}')
        self.registry = self.root / ".omp" / "plugins" / "installed_plugins.json"
        self.registry.write_text(json.dumps({"version": 2, "plugins": {"sample@reviewed": [{
            "scope": "user", "installPath": str(self.folder), "version": "1.2.3",
            "gitCommitSha": "c" * 40, "installedAt": "2026-10-08T00:00:00Z",
            "lastUpdated": "2026-10-08T00:00:00Z", "enabled": True,
        }]}}))

    def test_plugin_lock_records_observed_registry_version_and_revision(self):
        intent = {"packages": [], "plugins": [self.plugin]}
        effective = self.locking.prepare(intent, self.path, self.runner, "install")
        self.locking.record(effective, self.path, self.runner, "install")
        lock = json.loads((self.root / "stack.lock.json").read_text())
        self.assertEqual(lock["plugins"]["sample@reviewed|user"]["version"], "1.2.3")
        self.assertEqual(lock["plugins"]["sample@reviewed|user"]["revision"], "c" * 40)

    def test_missing_locked_plugin_refuses_unsupported_exact_reinstall(self):
        intent = {"packages": [], "plugins": [self.plugin]}
        effective = self.locking.prepare(intent, self.path, self.runner, "install")
        self.locking.record(effective, self.path, self.runner, "install")
        self.registry.unlink()
        with self.assertRaisesRegex(RuntimeError, "plugin|pin|exact"):
            self.locking.prepare(intent, self.path, self.runner, "sync")

    def test_changed_plugin_tree_is_drift_not_new_lock_provenance(self):
        intent = {"packages": [], "plugins": [self.plugin]}
        effective = self.locking.prepare(intent, self.path, self.runner, "install")
        self.locking.record(effective, self.path, self.runner, "install")
        before = (self.root / "stack.lock.json").read_bytes()
        (self.folder / "plugin.json").write_text("locally modified")
        with self.assertRaisesRegex(RuntimeError, "DRIFT|tree|modified"):
            self.locking.prepare(intent, self.path, self.runner, "sync")
        self.assertEqual((self.root / "stack.lock.json").read_bytes(), before)

    def test_redirected_plugin_cache_is_rejected_before_hashing(self):
        redirected = self.root / "redirected-cache"
        cache = self.folder.parent
        cache.rename(redirected)
        try:
            cache.symlink_to(redirected, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable")
        from dotai_app import skills
        with patch.object(skills, "installed_skill_tree_hash", side_effect=AssertionError("Redirected cache reached hashing")):
            with self.assertRaisesRegex(RuntimeError, "linked|redirect|symlink"):
                self.locking.prepare({"packages": [], "plugins": [self.plugin]}, self.path, self.runner, "install")

    def test_plugin_registry_cannot_claim_an_outside_directory(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "private.txt").write_text("not plugin data")
        registry = json.loads(self.registry.read_text())
        registry["plugins"]["sample@reviewed"][0]["installPath"] = str(unrelated)
        self.registry.write_text(json.dumps(registry))
        from dotai_app import skills
        with patch.object(skills, "installed_skill_tree_hash", side_effect=AssertionError("Unrelated data reached hashing")):
            with self.assertRaisesRegex(RuntimeError, "outside|cache|scope"):
                self.locking.prepare({"packages": [], "plugins": [self.plugin]}, self.path, self.runner, "install")

    def test_shared_plugin_directory_does_not_mint_scoped_lock_provenance(self):
        registry = json.loads(self.registry.read_text())
        shared = copy.deepcopy(registry["plugins"]["sample@reviewed"][0])
        shared["scope"] = "project"
        registry["plugins"]["another@reviewed"] = [shared]
        self.registry.write_text(json.dumps(registry))
        with self.assertRaisesRegex(RuntimeError, "shared|ambiguous|scope"):
            self.locking.prepare({"packages": [], "plugins": [self.plugin]}, self.path, self.runner, "install")

    def test_full_marketplace_provenance_survives_selected_execution_filter(self):
        selected = {"packages": [], "plugins": [self.plugin]}
        full = {**selected, "marketplaces": [{"name": "reviewed", "source": "example/original"}]}
        effective = self.locking.prepare(selected, self.path, self.runner, "install", provenance_manifest=full)
        self.locking.record(effective, self.path, self.runner, "install")
        effective = self.locking.prepare(selected, self.path, self.runner, "sync", provenance_manifest=full)
        self.assertEqual(effective["plugins"][0]["version"], "1.2.3")

    def test_changed_marketplace_source_invalidates_selected_plugin_lock(self):
        selected = {"packages": [], "plugins": [self.plugin]}
        full = {**selected, "marketplaces": [{"name": "reviewed", "source": "example/original"}]}
        effective = self.locking.prepare(selected, self.path, self.runner, "install", provenance_manifest=full)
        self.locking.record(effective, self.path, self.runner, "install")
        before = (self.root / "stack.lock.json").read_bytes()
        full["marketplaces"][0]["source"] = "example/replacement"
        with self.assertRaisesRegex(RuntimeError, "stale|intent|provenance"):
            self.locking.prepare(selected, self.path, self.runner, "sync", provenance_manifest=full)
        self.assertEqual((self.root / "stack.lock.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
