"""Isolated CLI contracts for selected plans, lifecycle, and quiet failures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliStackContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "stack.json"
        self.env = {**os.environ, "HOME": str(self.root), "DOTAI_HOME": str(self.root),
                    "DOTAI_CONFIG_DIR": str(self.root / "config"),
                    "DOTAI_STATE_DIR": str(self.root / "state"),
                    "XDG_STATE_HOME": str(self.root / "xdg"), "NO_COLOR": "1"}
        self.manifest = {"version": 2, "prerequisites": [], "packages": [], "skills": [],
                         "marketplaces": [], "plugins": [], "ompExtensions": [],
                         "ompRouting": None, "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}}}

    def cli(self, *args):
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        return self.invoke(*args)

    def invoke(self, *args):
        bootstrap = ("from dotai_app import cli, releases; "
                     "releases.latest_release_version = lambda: None; "
                     "raise SystemExit(cli.main())")
        return subprocess.run([sys.executable, "-c", bootstrap, "--manifest", str(self.path),
                               "--platform", "linux", *args], cwd=ROOT, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def tool(self, name="sample", *, fail=False):
        version = self.root / (name + ".version")
        later = self.root / (name + ".updated")
        script = ("import sys; print('CHILD NOISE'); print('{\"protocol\":true}'); "
                  "print('fixture installer refused', file=sys.stderr); sys.exit(7)") if fail else (
                  f"from pathlib import Path; Path({str(version)!r}).write_text('1.2.3'); "
                  "print('CHILD NOISE'); print('{\"protocol\":true}')")
        return {"name": name, "managed": True,
                "check": [sys.executable, "-c", f"from pathlib import Path; print(Path({str(version)!r}).read_text())"],
                "install": [[sys.executable, "-c", script]],
                "update": [[sys.executable, "-c", f"from pathlib import Path; Path({str(later)!r}).write_text('updated')"]]}

    def assert_no_machine_state(self):
        self.assertFalse((self.root / "stack.lock.json").exists())
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "stack.lock.json.write").exists())

    def test_selected_install_ignores_unselected_requirements_and_unsupported_pin(self):
        self.manifest["packages"] = [self.tool(), {"name": "Unselected", "recipe": "omp", "managed": True,
                                                       "version": "1.2.3", "requires": ["absent"]}]
        result = self.cli("install", "--only", "tool:sample")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("1.2.3", (self.root / "sample.version").read_text())
        self.assertNotIn("CHILD NOISE", result.stdout)
        self.assertNotIn('"protocol"', result.stdout)
        self.assertIn("UNVERIFIED", result.stdout)
        self.assertFalse((self.root / "state/component-receipts.json").exists())
        self.assertNotIn("install", json.loads(self.path.read_text())["packages"][1])

    def test_sync_retains_healthy_tool_without_updater_or_configuration(self):
        self.manifest["packages"] = [self.tool()]
        (self.root / "sample.version").write_text("1.2.3")
        configured = self.root / "configured"
        self.manifest["packages"][0]["configure"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(configured)!r}).touch()"]]
        result = self.cli("sync", "--only", "tool:sample")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.root / "sample.updated").exists())
        self.assertFalse(configured.exists())

    def test_enable_missing_tool_reuses_reviewed_lock_instead_of_latest_installer(self):
        package = self.tool()
        available = self.root / "available.version"
        available.write_text("1.2.3")
        package["install"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(self.root / 'sample.version')!r}).write_text(Path({str(available)!r}).read_text())"]]
        package["pinInstall"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(self.root / 'sample.version')!r}).write_text('{{version}}')"]]
        self.manifest["packages"] = [package]
        installed = self.cli("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        (self.root / "sample.version").unlink()
        available.write_text("3.0.0")
        package["enabled"] = False
        enabled = self.cli("enable", "tool:sample")
        self.assertEqual(enabled.returncode, 0, enabled.stdout + enabled.stderr)
        self.assertEqual((self.root / "sample.version").read_text(), "1.2.3")
        self.assertEqual(json.loads((self.root / "stack.lock.json").read_text())["packages"]["sample"]["version"], "1.2.3")
        self.assertTrue(json.loads(self.path.read_text())["packages"][0]["enabled"])
        self.assertEqual(enabled.stdout.lower().count("summary:"), 1)

    def test_unknown_selector_and_narrow_recommendation_refuse_before_writes(self):
        self.manifest["packages"] = [self.tool()]
        for args in (("install", "--only", "tool:missing"),
                     ("sync", "--only", "tool:sample", "--recommended-skills")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse((self.root / "sample.version").exists())
                self.assert_no_machine_state()

    def test_required_failure_stops_remaining_tools_and_configuration_with_one_summary(self):
        self.manifest["packages"] = [self.tool("broken", fail=True), self.tool("later")]
        self.manifest["mcp"]["servers"] = {"example": {"type": "http", "url": "https://example.invalid/mcp"}}
        self.manifest["integrationRequires"] = {"mcp": []}
        result = self.cli("install")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.root / "later.version").exists())
        self.assertFalse((self.root / ".omp/agent/mcp.json").exists())
        self.assert_no_machine_state()
        self.assertNotIn("CHILD NOISE", result.stdout)
        self.assertNotIn('"protocol"', result.stdout)
        self.assertIn("exit 7", result.stdout)
        self.assertNotIn("complete for", result.stdout)
        self.assertEqual(1, result.stdout.lower().count("summary:"))

    def test_fix_prerequisite_failure_reports_one_summary_without_mutation(self):
        self.manifest["skills"] = [{"source": "fixture/legacy", "agent": "pi", "skills": ["alpha"], "checkSkills": ["alpha"]}]
        self.manifest["prerequisites"] = [{"name": "node", "check": [sys.executable, "-c", "raise SystemExit(1)"], "hint": "Provide Node externally"}]
        result = self.cli("fix")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.manifest, json.loads(self.path.read_text()))
        self.assertEqual(result.stdout.lower().count("summary:"), 1)
        self.assertIn("1 failed", result.stdout)
        self.assert_no_machine_state()
        self.assertFalse(list(self.root.glob("stack.json.bak.*")))

    def test_dry_run_creates_no_receipts_state_lock_or_installed_files(self):
        self.manifest["packages"] = [self.tool()]
        result = self.cli("install", "--only", "tool:sample", "--dry-run")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.root / "sample.version").exists())
        self.assert_no_machine_state()
        self.assertIn("no changes applied", result.stdout)

    def test_repeatable_selection_installs_both_tools_once(self):
        self.manifest["packages"] = [self.tool("first"), self.tool("second"), self.tool("ignored")]
        result = self.cli("install", "--only", "tool:first", "--only", "tool:second", "--only", "tool:first")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("1.2.3", (self.root / "first.version").read_text())
        self.assertEqual("1.2.3", (self.root / "second.version").read_text())
        self.assertFalse((self.root / "ignored.version").exists())

    def test_lifecycle_dry_run_does_not_change_declaration_or_receipts(self):
        self.manifest["mcp"]["servers"] = {"example": {"type": "http", "url": "https://example.invalid/mcp"}}
        target = self.root / ".omp/agent/mcp.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"mcpServers": {"example": dict(self.manifest["mcp"]["servers"]["example"])}}))
        before = target.read_bytes()
        result = self.cli("adopt", "mcp:example", "--dry-run")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(self.manifest, json.loads(self.path.read_text()))
        self.assert_no_machine_state()

    def test_actual_mcp_adopt_disable_and_forget_preserve_provider_extras(self):
        self.manifest["mcp"]["servers"] = {"example": {"type": "http", "url": "https://example.invalid/mcp"}}
        self.manifest["packages"] = [{"name": "Unselected", "recipe": "omp", "managed": True, "version": "1.2.3"}]
        target = self.root / ".omp/agent/mcp.json"
        target.parent.mkdir(parents=True)
        extra = {"type": "http", "url": "https://other.invalid/mcp", "notes": "untouched"}
        target.write_text(json.dumps({"theme": "personal", "mcpServers": {
            "alias": {"type": "http", "url": "https://example.invalid/mcp", "timeout": 42}, "other": extra}}))
        result = self.cli("adopt", "mcp:example")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        result = self.invoke("disable", "mcp:example")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        config = json.loads(target.read_text())
        self.assertFalse(config["mcpServers"]["alias"]["enabled"])
        self.assertEqual(42, config["mcpServers"]["alias"]["timeout"])
        self.assertEqual(extra, config["mcpServers"]["other"])
        self.assertEqual("personal", config["theme"])
        result = self.invoke("remove", "mcp:example")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(config, json.loads(target.read_text()))
        self.assertEqual({}, json.loads(self.path.read_text())["mcp"]["servers"])

    def test_read_only_inventory_does_not_resolve_unselected_pin(self):
        self.manifest["mcp"]["servers"] = {"example": {"type": "http", "url": "https://example.invalid/mcp"}}
        self.manifest["packages"] = [{"name": "badpin", "recipe": "omp", "managed": True, "version": "1.2.3"}]
        for args in (("show", "mcp:example"), ("list", "mcp:example")):
            result = self.cli(*args)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("mcp:example", result.stdout)
            self.assert_no_machine_state()

    def test_direct_owned_tool_install_records_matching_binary_not_interpreter(self):
        binary = self.root / ("fixture-tool.cmd" if os.name == "nt" else "fixture-tool")
        source = (b"@echo off\r\necho 1.2.3\r\n" if os.name == "nt"
                  else ("#!" + sys.executable + "\nprint('1.2.3')\n").encode("utf-8"))
        self.manifest["packages"] = [{
            "name": "fixture-tool", "managed": True, "ownershipPath": str(binary),
            "check": [str(binary), "--version"],
            "install": [[sys.executable, "-c", f"from pathlib import Path; p=Path({str(binary)!r}); p.write_bytes({source!r}); p.chmod(0o755)"]],
        }]
        result = self.cli("install", "--only", "tool:fixture-tool")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        receipts = json.loads((self.root / "state/component-receipts.json").read_text())["components"]
        self.assertEqual(1, len(receipts))
        self.assertEqual([str(binary.resolve())], list(next(iter(receipts.values()))["snapshot"]["paths"]))
        self.assertNotIn(str(Path(sys.executable).resolve()), list(next(iter(receipts.values()))["snapshot"]["paths"]))
        result = self.invoke("show", "tool:fixture-tool")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("1.2.3", result.stdout)
        self.assertNotIn("observed latest", result.stdout)


if __name__ == "__main__":
    unittest.main()
