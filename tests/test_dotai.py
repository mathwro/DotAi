from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dotai_module", ROOT / "dotai.py")
assert SPEC and SPEC.loader
DOTAI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOTAI)


class DotAiTests(unittest.TestCase):
    def minimal_manifest(self, target: str) -> dict:
        return {
            "version": 1,
            "packages": [],
            "skills": [],
            "marketplaces": [],
            "plugins": [],
            "mcp": {
                "target": target,
                "servers": {
                    "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                    "microsoft-learn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
                },
            },
        }

    def test_mcp_merge_preserves_unmanaged_values_backs_up_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "customTopLevel": {"preserve": True},
                        "mcpServers": {
                            "context7": {"type": "http", "url": "https://old.example/mcp"},
                            "private": {"type": "http", "url": "https://private.example/mcp"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                runner = DOTAI.Runner("ubuntu")
                self.assertTrue(DOTAI.sync_mcp(manifest, runner))
                merged = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(merged["customTopLevel"], {"preserve": True})
                self.assertIn("private", merged["mcpServers"])
                self.assertEqual(merged["mcpServers"]["context7"], manifest["mcp"]["servers"]["context7"])
                backups = list(target.parent.glob("mcp.json.bak.*"))
                self.assertEqual(len(backups), 1)
                self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                self.assertEqual(len(list(target.parent.glob("mcp.json.bak.*"))), 1)

    def test_mcp_status_accepts_alias_headers_and_external_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "Ctx7": {
                                "type": "http",
                                "url": "https://mcp.context7.com/mcp",
                                "headers": {"CONTEXT7_API_KEY": "test-key"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            opencode = home / ".config" / "opencode" / "opencode.json"
            opencode.parent.mkdir(parents=True)
            opencode.write_text(
                json.dumps(
                    {
                        "mcp": {
                            "microsoft-learn": {
                                "type": "remote",
                                "url": "https://learn.microsoft.com/api/mcp",
                                "enabled": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            original = target.read_text(encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                healthy, detail = DOTAI.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_skill_status_distinguishes_codex_plugin_from_pi_install(self) -> None:
        skill = {"source": "owner/skills", "agent": "pi", "checkSkills": ["alpha", "beta"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cache = home / ".codex" / "plugins" / "cache" / "owner" / "plugin" / "1.0.0" / "skills"
            for name in skill["checkSkills"]:
                path = cache / name / "SKILL.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {name}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                installed, detail = DOTAI.skill_status(skill)
                self.assertFalse(installed)
                self.assertIn("Codex plugin", detail)
                self.assertIn("inactive in OMP", detail)
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                manifest["skills"] = [skill]
                manifest["mcp"]["servers"] = {}
                output = io.StringIO()
                DOTAI.configure_color("never")
                with contextlib.redirect_stdout(output):
                    self.assertFalse(DOTAI.print_status(manifest, DOTAI.Runner("ubuntu")))
                self.assertIn("[INACTIVE]", output.getvalue())
                for name in skill["checkSkills"]:
                    path = home / ".pi" / "agent" / "skills" / name / "SKILL.md"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"# {name}\n", encoding="utf-8")
                installed, detail = DOTAI.skill_status(skill)
                self.assertTrue(installed)
                self.assertEqual(detail, "installed for pi")

    def test_status_color_can_be_forced_or_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {}
            path.write_text(json.dumps(manifest), encoding="utf-8")

            colored = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}, clear=False):
                with contextlib.redirect_stdout(colored):
                    self.assertEqual(
                        DOTAI.main(["--manifest", str(path), "--color", "always", "status"]),
                        0,
                    )
            self.assertIn("\033[", colored.getvalue())
            self.assertIn("[OK]", colored.getvalue())

            plain = io.StringIO()
            with contextlib.redirect_stdout(plain):
                self.assertEqual(
                    DOTAI.main(["--manifest", str(path), "--color", "never", "status"]),
                    0,
                )
            self.assertNotIn("\033[", plain.getvalue())
            self.assertIn("[OK]", plain.getvalue())

    def test_add_commands_extend_every_supported_integration_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                ["add", "skill", "owner/skills", "--skill", "review", "--check-skill", "review"],
                ["add", "marketplace", "team", "owner/marketplace"],
                ["add", "plugin", "review@team"],
                ["add", "mcp", "local", "--command", "npx", "--arg=-y", "--arg", "server-package"],
                [
                    "add",
                    "tool",
                    "Example",
                    "--check",
                    "example --version",
                    "--install",
                    "windows=scoop install example",
                    "--install",
                    "linux=curl https://example.test/install | sh",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["skills"][0]["source"], "owner/skills")
            self.assertEqual(value["marketplaces"][0]["name"], "team")
            self.assertEqual(value["plugins"][0]["id"], "review@team")
            self.assertEqual(value["mcp"]["servers"]["local"]["args"], ["-y", "server-package"])
            self.assertEqual(value["packages"][0]["install"]["windows"], ["scoop install example"])

    def test_add_mcp_supports_remote_headers_and_stdio_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                [
                    "add",
                    "mcp",
                    "authenticated",
                    "--url",
                    "https://example.test/mcp",
                    "--header",
                    "Authorization=API_TOKEN",
                    "--header",
                    "X-Signed=signature=with=padding",
                ],
                [
                    "add",
                    "mcp",
                    "local",
                    "--command",
                    "npx",
                    "--arg=-y",
                    "--arg=@scope/server",
                    "--env",
                    "API_TOKEN=LOCAL_API_TOKEN",
                    "--env",
                    "LOG_LEVEL=warning",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 0)

            servers = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]
            self.assertEqual(
                servers["authenticated"]["headers"],
                {"Authorization": "API_TOKEN", "X-Signed": "signature=with=padding"},
            )
            self.assertEqual(
                servers["local"]["env"],
                {"API_TOKEN": "LOCAL_API_TOKEN", "LOG_LEVEL": "warning"},
            )

    def test_add_mcp_rejects_credentials_for_wrong_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                ["add", "mcp", "remote", "--url", "https://example.test/mcp", "--env", "TOKEN=TOKEN"],
                ["add", "mcp", "local", "--command", "npx", "--header", "Authorization=TOKEN"],
                [
                    "add",
                    "mcp",
                    "duplicate",
                    "--url",
                    "https://example.test/mcp",
                    "--header",
                    "Authorization=ONE",
                    "--header",
                    "Authorization=TWO",
                ],
                ["add", "mcp", "invalid", "--command", "npx", "--env", "INVALID-NAME=TOKEN"],
            ]
            for command in commands:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 2)

    def test_windows_plan_uses_scoop(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["packages"] = [
            {
                "name": "Example",
                "check": ["example", "--version"],
                "install": {"windows": [["scoop", "install", "example"]]},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = DOTAI.main(
                    ["--manifest", str(path), "--platform", "windows", "install", "--force", "--dry-run"]
                )
            self.assertEqual(result, 0)
            self.assertIn("scoop install example", output.getvalue())
            self.assertNotIn("winget", output.getvalue().lower())

    def test_missing_default_manifest_is_initialized_once_from_example(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "stack.json"
            example = root / "stack.example.json"
            template = self.minimal_manifest("~/.omp/agent/mcp.json")
            example.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")

            output = io.StringIO()
            with (
                mock.patch.object(DOTAI, "DEFAULT_MANIFEST", target),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(DOTAI.main(["validate"]), 0)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), template)
                local = dict(template)
                local["localOnly"] = True
                target.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
                self.assertEqual(DOTAI.main(["validate"]), 0)
                custom = root / "custom.json"
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DOTAI.main(["--manifest", str(custom), "validate"]), 2)
                self.assertFalse(custom.exists())

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), local)
            self.assertEqual(output.getvalue().count("Initialized"), 1)

    def test_repository_example_manifest_has_no_winget_commands(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        self.assertNotIn("winget", json.dumps(manifest).lower())
        self.assertIn("/stack.json", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertEqual(DOTAI.detect_platform(), os.environ.get("DOTAI_PLATFORM", DOTAI.detect_platform()))



if __name__ == "__main__":
    unittest.main()
