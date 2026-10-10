from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from dotai_app import manifest as manifests, recommendations, reconcile, runtime, state


class ReconcileContractTests(unittest.TestCase):
    def test_legacy_direct_reconciliation_cannot_execute_installers(self) -> None:
        # Removing the legacy guard would run an obsolete environment installer.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "changed-environment"
            manifest = {
                "version": 1,
                "packages": [{
                    "name": "Legacy runtime",
                    "check": [sys.executable, "-c", "raise SystemExit(1)"],
                    "install": {"default": [[sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('changed')"]]},
                }],
                "skills": [], "marketplaces": [], "plugins": [],
                "mcp": {"target": str(root / "mcp.json"), "servers": {}},
            }
            path = root / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "DOTAI_HOME": str(root), "HOME": str(root),
                "DOTAI_STATE_DIR": str(root / "state"),
                "XDG_STATE_HOME": str(root / "xdg"),
            }), contextlib.redirect_stdout(io.StringIO()):
                result = reconcile.reconcile(manifest, path, runtime.Runner("windows" if os.name == "nt" else "linux"), "install")
            self.assertFalse(marker.exists(), "Legacy installer changed the environment before conversion")
            self.assertNotEqual(result, 0)
            self.assertFalse((root / "state").exists())

    def test_recommendation_prerequisite_failure_preserves_manifest_and_ownership(self) -> None:
        # Omitting candidate preflight would save accepted skills before discovering
        # their missing runtime and advance recommendation ownership on failure.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            data = {
                "version": 2, "packages": [], "skills": [], "plugins": [], "marketplaces": [],
                "prerequisites": [{"name": "external-runtime", "check": [sys.executable, "-c", "raise SystemExit(1)"]}],
                "integrationRequires": {"skills": ["external-runtime"]},
                "mcp": {"target": str(root / "mcp.json"), "servers": {}},
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            before = path.read_bytes()
            recommendation = root / "recommendations.json"
            recommendation.write_text(json.dumps({
                **data, "skills": [{"source": "owner/recommended", "agent": "universal",
                                    "skills": ["review"], "checkSkills": ["review"]}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "HOME": str(root), "DOTAI_HOME": str(root),
                "DOTAI_STATE_DIR": str(root / "state"), "XDG_STATE_HOME": str(root / "xdg"),
            }), mock.patch.object(manifests, "EXAMPLE_MANIFEST", recommendation), \
                    mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", recommendation), \
                    mock.patch("builtins.input", return_value="a"), \
                    contextlib.redirect_stdout(io.StringIO()):
                runner = runtime.Runner("windows" if os.name == "nt" else "linux")
                updated, managed = recommendations.review_recommended_skills(data, path, runner)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(updated["skills"], [])
            self.assertEqual(managed, [])
            self.assertTrue(runner.failures)
            self.assertEqual(list(root.glob("stack.json.bak.*")), [])
            self.assertFalse((root / "state").exists())

    def test_legacy_recommendation_ownership_uses_independent_recommendation_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            current = {"source": "owner/current", "skills": ["review"]}
            retired = {"source": "owner/retired", "skills": ["old"]}
            recommendation = root / "recommendations.json"
            recommendation.write_text(json.dumps({"skills": [current]}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(root / "state")}), \
                    mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", recommendation):
                state_root = root / "state"
                state_root.mkdir()
                (state_root / "recommended-skills.json").write_text(json.dumps({
                    os.path.normcase(str(path.resolve())): [current, retired],
                }), encoding="utf-8")
                self.assertEqual(state.managed_recommendations(path), [current])

    def test_unsupported_pin_preflights_all_packages_before_first_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "changed-component"
            path = root / "stack.json"
            data = {
                "version": 2, "prerequisites": [], "skills": [], "plugins": [], "marketplaces": [],
                "packages": [
                    {"name": "First", "managed": True,
                     "check": [sys.executable, "-c", "raise SystemExit(1)"],
                     "install": {"default": [[sys.executable, "-c",
                         f"from pathlib import Path; Path({str(marker)!r}).write_text('changed')"]]}},
                    {"name": "Unsupported pin", "managed": True,
                     "check": [sys.executable, "-c", "print('1.0.0')"],
                     "install": {"default": [[sys.executable, "-c", "print('must not run')"]]},
                     "version": "2.0.0", "updatePolicy": "pinned"},
                ],
                "mcp": {"target": str(root / "mcp.json"), "servers": {}},
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "DOTAI_HOME": str(root), "HOME": str(root),
                "DOTAI_STATE_DIR": str(root / "state"), "XDG_STATE_HOME": str(root / "xdg"),
            }), contextlib.redirect_stdout(io.StringIO()):
                result = reconcile.reconcile(data, path, runtime.Runner("linux"), "install")
            self.assertFalse(marker.exists(), "An earlier component changed before pin preflight")
            self.assertNotEqual(result, 0)
            self.assertFalse((root / "state").exists())

    def test_install_records_observed_version_without_expanding_personal_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "installed.version"
            path = root / "stack.json"
            binary = root / ("personal-tool.cmd" if os.name == "nt" else "personal-tool")
            if os.name == "nt":
                launcher = f'@echo off\r\nif not exist "{marker}" exit /b 1\r\ntype "{marker}"\r\n'.encode()
            else:
                quoted = shlex.quote(str(marker))
                launcher = f"#!/bin/sh\n[ -f {quoted} ] || exit 1\ncat {quoted}\n".encode()
            check = [str(binary), "--version"]
            install = [sys.executable, "-c",
                       f"from pathlib import Path; Path({str(marker)!r}).write_text('{{version}}'); "
                       f"p=Path({str(binary)!r}); p.write_bytes({launcher!r}); p.chmod(0o755)"]
            data = {
                "version": 2, "prerequisites": [], "skills": [], "plugins": [], "marketplaces": [],
                "packages": [{"name": "Personal tool", "managed": True, "check": check,
                              "install": {"default": [install]}, "pinInstall": {"default": [install]},
                              "version": "1.2.3", "updatePolicy": "pinned"}],
                "mcp": {"target": str(root / "mcp.json"), "servers": {}},
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            original = path.read_bytes()
            with mock.patch.dict(os.environ, {
                "DOTAI_HOME": str(root), "HOME": str(root),
                "DOTAI_STATE_DIR": str(root / "state"), "XDG_STATE_HOME": str(root / "xdg"),
            }), contextlib.redirect_stdout(io.StringIO()):
                result = reconcile.reconcile(data, path, runtime.Runner("windows" if os.name == "nt" else "linux"), "install")
            self.assertEqual(result, 0)
            self.assertEqual(marker.read_text(), "1.2.3")
            lock_path = root / "stack.lock.json"
            self.assertTrue(lock_path.exists(), "A successful real install must record resolved facts")
            facts = json.loads(lock_path.read_text())
            self.assertEqual(facts["packages"]["Personal tool"]["version"], "1.2.3")
            self.assertNotIn(str(root), lock_path.read_text())
            self.assertEqual(path.read_bytes(), original)

    def test_failed_install_step_does_not_run_later_mutation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "unexpected-change"
            runner = runtime.Runner("linux")
            with contextlib.redirect_stdout(io.StringIO()):
                runtime.run_steps([
                    [sys.executable, "-c", "import sys; print('Permission denied', file=sys.stderr); raise SystemExit(7)"],
                    [sys.executable, "-c",
                     f"from pathlib import Path; Path({str(marker)!r}).write_text('changed')"],
                ], runner, "Install personal component")
            self.assertFalse(marker.exists(), "A later installer ran after its prerequisite step failed")
            self.assertTrue(runner.failures)


if __name__ == "__main__":
    unittest.main()
