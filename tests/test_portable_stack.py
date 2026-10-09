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


if __name__ == "__main__":
    unittest.main()
