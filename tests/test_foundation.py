"""Behavioral regressions for checks-only prerequisite and manifest boundaries."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotai_app import catalog, cli, health, manifest, packages, prerequisites, runtime


def stack() -> dict:
    return {"version": 2, "prerequisites": [], "packages": [], "skills": [],
            "marketplaces": [], "plugins": [], "ompExtensions": [], "ompRouting": None,
            "mcp": {"target": "~/unused-mcp.json", "servers": {}}}


def command(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class PrerequisiteBoundaryTests(unittest.TestCase):
    def test_v1_cli_refuses_before_running_declared_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "mutated"
            data = stack()
            data["version"] = 1
            data["packages"] = [{"name": "legacy", "check": command("raise SystemExit(1)"),
                                 "install": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
            path = root / "stack.json"
            path.write_text(json.dumps(data))
            error = io.StringIO()
            with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli.releases, "print_release_notice"):
                result = cli.main(["--manifest", str(path), "install"])
            self.assertEqual(result, 2)
            self.assertIn("convert", error.getvalue())
            self.assertFalse(marker.exists())

    def test_prerequisite_mutation_fields_rejected_before_manifest_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for field in ("install", "update", "configure", "uninstall"):
                data = stack()
                data["prerequisites"] = [{"name": "node", "check": command("print('20.0.0')"),
                                          field: [command("raise SystemExit(0)")]}]
                path = root / field / "stack.json"
                with self.subTest(field=field), self.assertRaises(runtime.DotAiError):
                    manifest.write_manifest(path, data)
                self.assertFalse(path.parent.exists())

    def test_renaming_a_prerequisite_does_not_grant_package_management(self):
        for check in (["node", "--version"], {"default": ["uv", "--version"]}, "git --version"):
            data = stack()
            data["packages"] = [{"name": "my-ai-helper", "managed": True, "check": check, "install": []}]
            with self.subTest(check=check), self.assertRaises(runtime.DotAiError):
                manifest.validate_manifest(data)

    def test_packages_require_explicit_management_permission(self):
        data = stack()
        data["packages"] = [{"name": "custom", "check": command("raise SystemExit(1)"), "install": []}]
        with self.assertRaisesRegex(runtime.DotAiError, "managed"):
            manifest.validate_manifest(data)

    def test_general_dependencies_cannot_be_managed_packages(self):
        for name in ("Node.js", "node", "npm", "uv", "Python", "Git", "curl", "brew", "scoop"):
            data = stack()
            data["packages"] = [{"name": name, "managed": True, "check": command("print('1.0.0')"), "install": []}]
            with self.subTest(name=name), self.assertRaisesRegex(runtime.DotAiError, "prerequisite"):
                manifest.validate_manifest(data)

    def test_missing_selected_prerequisite_blocks_all_package_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "mutated"
            data = stack()
            data["prerequisites"] = [{"name": "external", "check": command("raise SystemExit(1)"), "hint": "Install externally."}]
            data["packages"] = [{"name": "dependent", "managed": True, "requires": ["external"],
                                 "check": command("raise SystemExit(1)"),
                                 "install": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
            path = root / "stack.json"
            path.write_text(json.dumps(data))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(cli.releases, "print_release_notice"):
                result = cli.main(["--manifest", str(path), "install"])
            self.assertEqual(result, 1)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(path.read_text()), data)

    def test_unselected_missing_prerequisite_does_not_make_status_unhealthy(self):
        data = stack()
        data["prerequisites"] = [{"name": "unused", "check": command("raise SystemExit(1)")}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(health.doctor(data, runtime.Runner("macos")))

    def test_disabled_packages_do_not_run_configure(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "mutated"
            data = stack()
            data["packages"] = [{"name": "disabled", "managed": True, "enabled": False,
                                 "check": command("print('1.0.0')"), "install": [],
                                 "configure": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
            with contextlib.redirect_stdout(io.StringIO()):
                packages.reconcile_packages(data, runtime.Runner("macos"), "install")
            self.assertFalse(marker.exists())

    def test_inspection_missing_default_does_not_initialize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "stack.json"
            with mock.patch.dict(os.environ, {"DOTAI_CONFIG_DIR": str(path.parent)}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(cli.releases, "print_release_notice"):
                result = cli.main(["validate"])
            self.assertEqual(result, 2)
            self.assertFalse(path.exists())


class ManifestConversionTests(unittest.TestCase):
    def legacy(self, marker: Path) -> dict:
        data = stack()
        data["version"] = 1
        mutation = command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        data["packages"] = [
            {"name": "Node.js", "updateGroup": "dependency", "check": ["node", "--version"],
             "install": [mutation], "update": [mutation], "configure": [mutation], "note": "Keep this external runtime"},
            {"name": "custom-ai", "check": command("print('1.2.3')"), "install": [mutation],
             "customMetadata": {"keep": True}},
        ]
        data["skills"] = [{"source": "owner/skills", "agent": "codex", "skills": ["review"], "checkSkills": ["review"]}]
        data["mcp"]["servers"] = {"private": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Authorization": "SECRET_REFERENCE"}}}
        data["userMetadata"] = {"preserve": "yes"}
        return data

    def invoke(self, path: Path, *options: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return cli.main(["--manifest", str(path), "convert", *options])
            except SystemExit as exc:
                self.fail(f"Explicit conversion command must be available, exited {exc.code}")

    def test_preview_preserves_source_without_backups_or_environment_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "environment", root / "legacy.json"
            path.write_text(json.dumps(self.legacy(marker)))
            before = path.read_bytes()
            self.assertEqual(self.invoke(path, "--dry-run"), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(root.iterdir()), {path})

    def test_reviewed_conversion_preserves_entries_and_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "environment", root / "legacy.json"
            original = self.legacy(marker)
            path.write_text(json.dumps(original))
            before = path.read_bytes()
            self.assertEqual(self.invoke(path, "--manage", "custom-ai", "--yes"), 0)
            converted = json.loads(path.read_text())
            self.assertEqual(converted["version"], 2)
            self.assertEqual(converted["skills"], original["skills"])
            self.assertEqual(converted["mcp"], original["mcp"])
            self.assertEqual(converted["userMetadata"], original["userMetadata"])
            self.assertEqual(converted["packages"], [{**original["packages"][1], "managed": True}])
            self.assertEqual(converted["prerequisites"][0]["name"], "node")
            self.assertNotIn("install", converted["prerequisites"][0])
            self.assertNotIn("configure", converted["prerequisites"][0])
            self.assertEqual(converted["prerequisites"][0]["note"], "Keep this external runtime")
            backups = list(root.glob("legacy.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            if sys.platform != "win32":
                self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(root.iterdir()), {path, backups[0]})

    def test_unreviewed_custom_ownership_refuses_noninteractive_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "environment", root / "legacy.json"
            path.write_text(json.dumps(self.legacy(marker)))
            before = path.read_bytes()
            self.assertEqual(self.invoke(path, "--yes"), 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(root.iterdir()), {path})

    def test_optional_destination_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "environment", root / "legacy.json"
            destination = root / "portable" / "stack.json"
            path.write_text(json.dumps(self.legacy(marker)))
            before = path.read_bytes()
            self.assertEqual(self.invoke(path, "--destination", str(destination), "--manage", "custom-ai", "--yes"), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(destination.read_text())["version"], 2)
            self.assertEqual(len(list(root.glob("legacy.json.bak.*"))), 1)
            self.assertFalse(marker.exists())

    def test_existing_destination_refused_before_backup_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "environment", root / "legacy.json"
            destination = root / "destination.json"
            path.write_text(json.dumps(self.legacy(marker)))
            destination.write_text("keep me")
            self.assertEqual(self.invoke(path, "--destination", str(destination), "--manage", "custom-ai", "--yes"), 2)
            self.assertEqual(destination.read_text(), "keep me")
            self.assertEqual(set(root.iterdir()), {path, destination})

    def test_static_routing_preserved_without_provider_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.json"
            data = self.legacy(root / "environment")
            data["ompRouting"] = {"roles": {"default": ["synthetic/model"]}}
            path.write_text(json.dumps(data))
            self.assertEqual(self.invoke(path, "--manage", "custom-ai", "--yes"), 0)
            self.assertEqual(json.loads(path.read_text())["ompRouting"], data["ompRouting"])
            self.assertFalse((root / "environment").exists())

    def test_conversion_requires_explicit_source_not_default_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            path.write_text(json.dumps(self.legacy(root / "environment")))
            before = path.read_bytes()
            with mock.patch.dict(os.environ, {"DOTAI_CONFIG_DIR": str(root)}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    result = cli.main(["convert", "--manage", "custom-ai", "--yes"])
                except SystemExit as exc:
                    result = exc.code
            self.assertEqual(result, 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(root.iterdir()), {path})

    def test_known_recipe_match_keeps_custom_commands_as_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.json"
            data = stack()
            data["version"] = 1
            data["packages"] = [{"name": "chosen-harness", "check": ["omp", "--version"],
                                 "install": [command("raise SystemExit(7)")], "customNote": "preserve"}]
            path.write_text(json.dumps(data))
            self.assertEqual(self.invoke(path, "--manage", "chosen-harness", "--yes"), 0)
            converted = json.loads(path.read_text())
            self.assertEqual(converted["packages"][0]["recipe"], "omp")
            self.assertEqual(converted["packages"][0]["install"], data["packages"][0]["install"])
            self.assertEqual(converted["packages"][0]["customNote"], "preserve")

    def test_destination_created_during_write_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "legacy.json", root / "destination.json"
            source.write_text(json.dumps(self.legacy(root / "environment")))
            real_write = manifest.write_manifest
            def competing_write(path, data, **options):
                destination.write_text("independent user file")
                return real_write(path, data, **options)
            with mock.patch.object(manifest, "write_manifest", side_effect=competing_write):
                result = self.invoke(source, "--destination", str(destination), "--manage", "custom-ai", "--yes")
            self.assertEqual(result, 2)
            self.assertEqual(destination.read_text(), "independent user file")
            self.assertEqual(json.loads(source.read_text())["version"], 1)


class OptionalComponentTests(unittest.TestCase):
    def invoke(self, path: Path, *arguments: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(cli.releases, "print_release_notice"):
            try:
                return cli.main(["--manifest", str(path), *arguments])
            except SystemExit as exc:
                self.fail(f"Optional component command unavailable: {exc.code}")

    def test_explicit_empty_init_is_healthy_without_any_external_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            self.assertEqual(self.invoke(path, "init"), 0)
            data = json.loads(path.read_text())
            for section in ("packages", "skills", "marketplaces", "plugins", "ompExtensions"):
                self.assertEqual(data[section], [])
            self.assertEqual(data["mcp"]["servers"], {})
            with mock.patch.object(runtime.Runner, "succeeds", side_effect=AssertionError("Unused component probe")):
                self.assertEqual(self.invoke(path, "status"), 0)
                self.assertEqual(self.invoke(path, "doctor"), 0)

    def test_recipe_selection_saves_intent_not_expanded_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            self.assertEqual(self.invoke(path, "init", "--component", "graphify"), 0)
            data = json.loads(path.read_text())
            self.assertEqual(data["packages"], [{"name": "graphify", "recipe": "graphify", "managed": True}])
            self.assertEqual(data["skills"], [])
            self.assertEqual(self.invoke(path, "add", "component", "rtk"), 0)
            after = json.loads(path.read_text())
            self.assertEqual([entry["recipe"] for entry in after["packages"]], ["graphify", "rtk"])
            self.assertFalse(any("install" in entry or "check" in entry for entry in after["packages"]))
            self.assertEqual(after["ompExtensions"], [])

    def test_unknown_recipe_refused_without_creating_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-created" / "stack.json"
            self.assertEqual(self.invoke(path, "init", "--component", "does-not-exist"), 2)
            self.assertFalse(path.parent.exists())

    def test_default_manifest_location_resolved_at_command_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desired = root / "portable" / "stack.json"
            obsolete = root / "old-repository-location.json"
            with mock.patch.dict(os.environ, {"DOTAI_CONFIG_DIR": str(desired.parent)}), mock.patch.object(manifest, "DEFAULT_MANIFEST", obsolete, create=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["init"]), 0)
            self.assertTrue(desired.exists())
            self.assertFalse(obsolete.exists())

    @unittest.skipIf(os.name == "nt", "POSIX independent executable fixture")
    def test_exact_version_mismatch_runs_reviewed_pin_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            version = root / "version"
            version.write_text("2.0.0")
            binary = root / "ai-helper"
            binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(version)!r}).read_text())\n")
            binary.chmod(0o755)
            check = [str(binary), "--version"]
            install = command(f"from pathlib import Path; Path({str(version)!r}).write_text('1.2.0')")
            data = stack()
            data["packages"] = [{"name": "ai-helper", "managed": True, "version": "1.2.0", "updatePolicy": "pinned",
                                 "check": check, "install": [install], "update": [install], "pinInstall": [install]}]
            from dotai_app import lifecycle
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(root / "state")}):
                runner = runtime.Runner("macos")
                self.assertFalse(packages.package_check(data["packages"][0], runner))
                lifecycle.record_install("tool", "ai-helper", data["packages"][0], data, runner)
                with contextlib.redirect_stdout(io.StringIO()):
                    packages.reconcile_packages(data, runner, "install", ownership_check=lambda value, current, actor, operation:
                                                lifecycle.check_update_ownership("tool", value["name"], value, current, actor, operation=operation))
                self.assertEqual(version.read_text(), "1.2.0")
                self.assertEqual(runner.failures, [])

    def test_missing_updater_requirement_blocks_update_before_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, path = root / "updated", root / "stack.json"
            data = stack()
            data["prerequisites"] = [{"name": "safe-updater", "check": command("raise SystemExit(1)")}]
            data["packages"] = [{"name": "ai-helper", "managed": True, "check": command("print('1.0.0')"),
                                 "install": [], "updateRequires": ["safe-updater"],
                                 "update": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
            path.write_text(json.dumps(data))
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(root / "home"), "DOTAI_STATE_DIR": str(root / "state")}):
                self.assertEqual(self.invoke(path, "update"), 1)
            self.assertFalse(marker.exists())
            self.assertFalse((root / "state").exists())

    @unittest.skipIf(os.name == "nt", "POSIX independent executable fixture")
    def test_force_install_uses_installer_not_unsafe_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            version, marker, path = root / "version", root / "unsafe-updater", root / "stack.json"
            version.write_text("1.0.0")
            binary = root / "ai-helper"
            binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(version)!r}).read_text())\n")
            binary.chmod(0o755)
            data = stack()
            data["prerequisites"] = [{"name": "safe-updater", "check": command("raise SystemExit(1)")}]
            data["packages"] = [{"name": "ai-helper", "managed": True, "minimumVersion": "2.0.0",
                                 "check": [str(binary), "--version"],
                                 "install": [command(f"from pathlib import Path; Path({str(version)!r}).write_text('2.0.0')")],
                                 "updateRequires": ["safe-updater"],
                                 "update": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
            path.write_text(json.dumps(data))
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(root / "home"), "DOTAI_STATE_DIR": str(root / "state")}):
                self.assertEqual(self.invoke(path, "adopt", "tool:ai-helper"), 0)
                self.assertEqual(self.invoke(path, "install", "--force"), 0)
            self.assertEqual(version.read_text(), "2.0.0")
            self.assertFalse(marker.exists())

    def test_configuration_condition_requires_enabled_matching_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for selected in ([], [{"path": "~/selected-extension.ts", "enabled": False}], ["~/selected-extension.ts"]):
                marker = root / "configured"
                marker.unlink(missing_ok=True)
                data = stack()
                data["ompExtensions"] = selected
                data["packages"] = [{"name": "ai-helper", "managed": True, "check": command("print('1.0.0')"), "install": [],
                                     "configureWhen": {"ompExtensions": ["~/selected-extension.ts"]},
                                     "configure": [command(f"from pathlib import Path; Path({str(marker)!r}).touch()")]}]
                with self.subTest(selected=selected), contextlib.redirect_stdout(io.StringIO()):
                    packages.reconcile_packages(data, runtime.Runner("macos"), "install")
                    self.assertEqual(marker.exists(), selected == ["~/selected-extension.ts"])

    def test_duplicate_component_identities_are_rejected(self):
        cases = [
            ("packages", [{"name": "same", "managed": True, "check": command("print('1.0.0')"), "install": []},
                          {"name": "same", "managed": True, "check": command("print('2.0.0')"), "install": []}]),
            ("skills", [{"source": "owner/repo", "skills": ["one"]}, {"source": "owner/repo", "agent": "universal", "skills": ["two"]}]),
            ("marketplaces", [{"name": "same", "source": "owner/one"}, {"name": "same", "source": "owner/two"}]),
            ("plugins", [{"id": "same@market"}, {"id": "same@market", "scope": "user"}]),
        ]
        for section, entries in cases:
            data = stack()
            data[section] = entries
            with self.subTest(section=section), self.assertRaises(runtime.DotAiError):
                manifest.validate_manifest(data)

    def test_semantically_duplicate_extension_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"DOTAI_HOME": directory}):
            data = stack()
            data["ompExtensions"] = ["~/extensions/check.ts", str(Path(directory) / "extensions" / "check.ts")]
            with self.assertRaises(runtime.DotAiError):
                manifest.validate_manifest(data)

    def test_updater_checks_selected_only_for_actual_update_operation(self):
        data = stack()
        data["prerequisites"] = [{"name": "safe-updater", "check": command("raise SystemExit(1)")}]
        data["packages"] = [{"name": "ai-helper", "managed": True, "check": command("print('1.0.0')"),
                             "install": [command("print('1.0.0')")],
                             "update": [command("print('1.0.0')")], "updateRequires": ["safe-updater"]}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(prerequisites.preflight(data, runtime.Runner("macos"), "update"))
            self.assertTrue(prerequisites.preflight(data, runtime.Runner("macos"), "install"))
            self.assertTrue(prerequisites.preflight(data, runtime.Runner("macos"), "install", force=True))


if __name__ == "__main__":
    unittest.main()
