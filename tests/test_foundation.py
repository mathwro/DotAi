"""Behavioral regressions for checks-only prerequisite and manifest boundaries."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotai_app import cli, health, manifest, packages, runtime


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
            path = Path(directory) / "missing.json"
            with mock.patch.object(manifest, "DEFAULT_MANIFEST", path), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(cli.releases, "print_release_notice"):
                result = cli.main(["validate"])
            self.assertEqual(result, 2)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
