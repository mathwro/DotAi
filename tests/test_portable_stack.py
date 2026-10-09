"""Portable intent and reproducible execution boundary regressions."""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sys
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
    def test_materialization_preserves_portable_intent_and_explicit_overrides(self):
        catalog = feature(self, "catalog")
        intent = {"version": 2, "packages": [{"name": "Graphify", "recipe": "graphify", "managed": True, "check": ["custom", "--version"]}]}
        original = copy.deepcopy(intent)
        effective = catalog.materialize(intent)
        self.assertEqual(intent, original)
        self.assertEqual(effective["packages"][0]["check"], ["custom", "--version"])
        self.assertIn("install", effective["packages"][0])
        self.assertEqual(effective["packages"][0]["requires"], ["uv"])
        effective["packages"][0]["requires"].append("changed")
        self.assertEqual(catalog.resolve_package(intent["packages"][0])["requires"], ["uv"])

    def test_prerequisite_definitions_cannot_install_environment_tools(self):
        catalog = feature(self, "catalog").load_catalog()
        for name in ("node", "npx", "uv", "curl", "git", "brew", "scoop"):
            definition = catalog["prerequisites"][name]
            self.assertTrue(definition["check"])
            self.assertFalse(set(definition) & {"install", "update", "configure"})

    def test_unknown_recipe_and_unsupported_pin_fail_before_execution(self):
        catalog = feature(self, "catalog")
        with self.assertRaisesRegex(RuntimeError, "recipe"):
            catalog.resolve_package({"name": "Mystery", "recipe": "missing", "managed": True})
        with self.assertRaisesRegex(RuntimeError, "pin"):
            catalog.resolve_package({"name": "OMP", "recipe": "omp", "managed": True, "version": "1.2.3"})

    def test_graphify_exact_version_uses_uv_requirement_not_latest_upgrade(self):
        catalog = feature(self, "catalog")
        resolved = catalog.resolve_package({"name": "Graphify", "recipe": "graphify", "managed": True, "version": "0.4.2", "updatePolicy": "pinned"})
        from dotai_app.runtime import selected
        self.assertEqual(selected(resolved["install"], "macos"), [["uv", "tool", "install", "graphifyy==0.4.2"]])
        self.assertEqual(selected(resolved["update"], "windows"), [["uv", "tool", "install", "graphifyy==0.4.2"]])

    def test_custom_pin_requires_explicit_reviewed_pin_command(self):
        catalog = feature(self, "catalog")
        intent = {"name": "custom", "managed": True, "version": "2.3.4", "check": ["custom", "--version"], "install": [["custom-installer", "latest"]]}
        with self.assertRaisesRegex(RuntimeError, "pin"):
            catalog.resolve_package(intent)
        intent["pinInstall"] = [["custom-installer", "{version}"]]
        self.assertEqual(catalog.resolve_package(intent)["install"], [["custom-installer", "2.3.4"]])

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
            self.assertEqual(str(portable.default_manifest_path("windows")), "/roaming/DotAi/stack.json")
            self.assertEqual(str(portable.default_manifest_path("linux")), "/xdg/dotai/stack.json")


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
        intent = {"packages": [self.package(), {"name": "OMP", "recipe": "omp", "managed": True, "version": "1.2.3"}]}
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
        (folder / "SKILL.md").write_text("# Alpha\n")
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
        self.folder = self.root / ".omp" / "plugins" / "cache" / "sample"
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


if __name__ == "__main__":
    unittest.main()
