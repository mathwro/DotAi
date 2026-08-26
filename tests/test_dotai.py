from __future__ import annotations

import contextlib
import importlib.util
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
            "ompExtensions": [],
            "mcp": {
                "target": target,
                "servers": {
                    "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                    "microsoft-learn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
                },
            },
        }

    def test_validate_omp_routing_normalizes_valid_fixture(self) -> None:
        routing = {
            "roles": {"default": ["openai-codex/gpt-5.6-sol"]},
            "agentModelOverrides": {"sonic": "@smol"},
            "usageReservePct": 25,
            "usageReservePolicy": "confirm",
            "fallbackRevertPolicy": "never",
        }

        self.assertEqual(DOTAI.validate_omp_routing(routing), routing)
        self.assertEqual(
            DOTAI.validate_omp_routing({"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}),
            {
                "roles": {"default": ["openai-codex/gpt-5.6-sol"]},
                "agentModelOverrides": {},
                "usageReservePct": 10,
                "usageReservePolicy": "auto",
                "fallbackRevertPolicy": "cooldown-expiry",
            },
        )
        self.assertEqual(DOTAI.validate_omp_routing(None), {})

    def test_validate_omp_routing_rejects_invalid_values(self) -> None:
        valid_roles = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
        invalid_values = [
            [],
            {"roles": {}},
            {"roles": {"": ["openai-codex/gpt-5.6-sol"]}},
            {"roles": {"default": []}},
            {"roles": {"default": "openai-codex/gpt-5.6-sol"}},
            {"roles": {"default": [""]}},
            {"roles": {"default": [1]}},
            {"roles": {1: ["openai-codex/gpt-5.6-sol"]}},
            {**valid_roles, "agentModelOverrides": []},
            {**valid_roles, "agentModelOverrides": {"": "@smol"}},
            {**valid_roles, "agentModelOverrides": {"sonic": ""}},
            {**valid_roles, "agentModelOverrides": {"sonic": 1}},
            {**valid_roles, "usageReservePct": True},
            {**valid_roles, "usageReservePct": 101},
            {**valid_roles, "usageReservePct": -1},
            {**valid_roles, "usageReservePct": 1.5},
            {**valid_roles, "usageReservePolicy": "ask"},
            {**valid_roles, "usageReservePolicy": []},
            {**valid_roles, "fallbackRevertPolicy": "always"},
            {**valid_roles, "fallbackRevertPolicy": []},
            {**valid_roles, "unexpected": True},
        ]

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(DOTAI.DotAiError):
                    DOTAI.validate_omp_routing(value)

    def test_load_manifest_normalizes_present_omp_routing_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertNotIn("ompRouting", DOTAI.load_manifest(path))

            manifest["ompRouting"] = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                DOTAI.load_manifest(path)["ompRouting"]["usageReservePct"],
                10,
            )

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

    def test_selector_identity_removes_one_recognized_thinking_suffix(self) -> None:
        self.assertEqual(DOTAI.selector_identity("openai-codex/gpt-5.6-sol:high"), "openai-codex/gpt-5.6-sol")
        self.assertEqual(DOTAI.selector_identity("openai-codex/gpt-5.6-sol:high:auto"), "openai-codex/gpt-5.6-sol:high")
        self.assertEqual(DOTAI.selector_identity("openai-codex/gpt-5.6-sol:custom"), "openai-codex/gpt-5.6-sol:custom")

    def test_available_omp_models_parses_only_complete_catalogs(self) -> None:
        runner = DOTAI.Runner("ubuntu")
        catalog = json.dumps(
            {
                "models": [
                    {"selector": "openai-codex/gpt-5.6-sol"},
                    {"selector": "github-copilot/gpt-5.6-sol"},
                    {"selector": "github-copilot/gpt-5.4-mini"},
                ]
            }
        )

        with mock.patch.object(runner, "output", return_value=catalog) as output:
            self.assertEqual(
                DOTAI.available_omp_models(runner),
                {
                    "openai-codex/gpt-5.6-sol",
                    "github-copilot/gpt-5.6-sol",
                    "github-copilot/gpt-5.4-mini",
                },
            )
        output.assert_called_once_with(["omp", "models", "--json"])

        for malformed in ("", "[]", "{", "{}", '{"models": {}}', '{"models": ["selector"]}', '{"models": [{}]}', '{"models": [{"selector": ""}]}'):
            with self.subTest(malformed=malformed), mock.patch.object(runner, "output", return_value=malformed):
                self.assertIsNone(DOTAI.available_omp_models(runner))

        with mock.patch.object(runner, "output", return_value='{"models": []}'):
            self.assertEqual(DOTAI.available_omp_models(runner), set())

    def test_resolve_omp_routing_preserves_order_suffixes_and_unavailable_roles(self) -> None:
        routing = {
            "roles": {
                "default": [
                    "openai-codex/gpt-5.6-sol",
                    "github-copilot/gpt-5.6-sol",
                    "openai-codex/gpt-5.6-sol",
                ],
                "slow": ["openai-codex/gpt-5.6-sol:high", "github-copilot/gpt-5.6-sol:high"],
                "smol": ["github-copilot/gpt-5.4-mini"],
                "unavailable": ["openai-codex/gpt-5.4-mini"],
            }
        }
        available = {
            "openai-codex/gpt-5.6-sol",
            "github-copilot/gpt-5.6-sol",
            "github-copilot/gpt-5.4-mini",
        }

        primaries, fallbacks, unavailable = DOTAI.resolve_omp_routing(routing, available)

        self.assertEqual(
            primaries,
            {
                "default": "openai-codex/gpt-5.6-sol",
                "slow": "openai-codex/gpt-5.6-sol:high",
                "smol": "github-copilot/gpt-5.4-mini",
            },
        )
        self.assertEqual(
            fallbacks["default"], ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-sol"]
        )
        self.assertEqual(
            fallbacks["slow"], ["openai-codex/gpt-5.6-sol:high", "github-copilot/gpt-5.6-sol:high"]
        )
        self.assertEqual(unavailable, ["unavailable"])

    def test_configure_omp_routing_merges_managed_values_and_writes_scalars(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = {
            "roles": {
                "default": ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-sol"],
                "fast": ["github-copilot/gpt-5.4-mini"],
                "unavailable": ["anthropic/claude-test"],
            },
            "agentModelOverrides": {"sonic": "@smol"},
            "usageReservePct": 10,
            "usageReservePolicy": "auto",
            "fallbackRevertPolicy": "cooldown-expiry",
        }
        values = {
            "modelRoles": {"custom": "private/keep", "default": "old/model"},
            "retry.fallbackChains": {"custom": ["private/keep"], "default": ["old/model"]},
            "task.agentModelOverrides": {"reviewer": "@fast", "sonic": "old"},
            "retry.modelFallback": False,
            "retry.usageAwareFallback": False,
            "retry.usageReservePct": 5,
            "retry.usageReservePolicy": "confirm",
            "retry.fallbackRevertPolicy": "never",
        }
        runner = DOTAI.Runner("ubuntu")
        catalog = json.dumps(
            {
                "models": [
                    {"selector": "openai-codex/gpt-5.6-sol"},
                    {"selector": "github-copilot/gpt-5.6-sol"},
                    {"selector": "github-copilot/gpt-5.4-mini"},
                ]
            }
        )

        def output(command: list[str]) -> str:
            if command[1] == "models":
                return catalog
            return json.dumps({"key": command[3], "value": values[command[3]]})

        report = io.StringIO()
        with (
            mock.patch.object(runner, "output", side_effect=output),
            mock.patch.object(runner, "run") as run,
            contextlib.redirect_stdout(report),
        ):
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 0)

        commands = [call.args[0] for call in run.call_args_list]
        payloads = {command[3]: command[4] for command in commands}
        self.assertEqual(json.loads(payloads["modelRoles"]), {
            "custom": "private/keep",
            "default": "openai-codex/gpt-5.6-sol",
            "fast": "github-copilot/gpt-5.4-mini",
        })
        self.assertEqual(json.loads(payloads["retry.fallbackChains"]), {
            "custom": ["private/keep"],
            "default": ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-sol"],
            "fast": ["github-copilot/gpt-5.4-mini"],
        })
        self.assertEqual(json.loads(payloads["task.agentModelOverrides"]), {"reviewer": "@fast", "sonic": "@smol"})
        self.assertEqual(
            {key: payloads[key] for key in (
                "retry.modelFallback",
                "retry.usageAwareFallback",
                "retry.usageReservePct",
                "retry.usageReservePolicy",
                "retry.fallbackRevertPolicy",
            )},
            {
                "retry.modelFallback": "true",
                "retry.usageAwareFallback": "true",
                "retry.usageReservePct": "10",
                "retry.usageReservePolicy": "auto",
                "retry.fallbackRevertPolicy": "cooldown-expiry",
            },
        )
        self.assertIn("github-copilot", report.getvalue())
        self.assertIn("unavailable", report.getvalue())

    def test_configure_omp_routing_is_idempotent_and_dry_run_only_prints_plan(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = {
            "roles": {"default": ["openai-codex/gpt-5.6-sol"]},
            "agentModelOverrides": {"sonic": "@smol"},
            "usageReservePct": 10,
            "usageReservePolicy": "auto",
            "fallbackRevertPolicy": "cooldown-expiry",
        }
        catalog = json.dumps({"models": [{"selector": "openai-codex/gpt-5.6-sol"}]})
        current = {
            "modelRoles": {"custom": "private/keep", "default": "openai-codex/gpt-5.6-sol"},
            "retry.fallbackChains": {"custom": ["private/keep"], "default": ["openai-codex/gpt-5.6-sol"]},
            "task.agentModelOverrides": {"reviewer": "@fast", "sonic": "@smol"},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }

        def output(command: list[str]) -> str:
            return catalog if command[1] == "models" else json.dumps({"value": current[command[3]]})

        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output", side_effect=output), mock.patch.object(runner, "run") as run:
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 0)
        run.assert_not_called()

        dry_runner = DOTAI.Runner("ubuntu", dry_run=True)
        changed = {**current, "retry.usageReservePct": 5}
        report = io.StringIO()
        with (
            mock.patch.object(
                dry_runner,
                "output",
                side_effect=lambda command: catalog if command[1] == "models" else json.dumps({"value": changed[command[3]]}),
            ),
            mock.patch.object(dry_runner, "run") as run,
            contextlib.redirect_stdout(report),
        ):
            self.assertEqual(DOTAI.configure_omp_routing(manifest, dry_runner), 0)
        run.assert_not_called()
        self.assertIn("Dry run", report.getvalue())
        self.assertIn("retry.usageReservePct", report.getvalue())

    def test_configure_omp_routing_rejects_unreadable_catalog_or_configuration(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = DOTAI.validate_omp_routing({"roles": {"default": ["openai-codex/gpt-5.6-sol"]}})
        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output", return_value="{"), mock.patch.object(runner, "run") as run:
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 1)
        run.assert_not_called()

        catalog = json.dumps({"models": [{"selector": "openai-codex/gpt-5.6-sol"}]})
        with (
            mock.patch.object(
                runner,
                "output",
                side_effect=lambda command: catalog if command[1] == "models" else json.dumps({"value": [] if command[3] == "modelRoles" else None}),
            ),
            mock.patch.object(runner, "run") as run,
        ):
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 1)
        run.assert_not_called()

        with (
            mock.patch.object(runner, "output", return_value=json.dumps({"models": []})),
            mock.patch.object(runner, "run") as run,
        ):
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 1)
        run.assert_not_called()

    def test_configured_omp_value_requires_a_json_value_object(self) -> None:
        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output", return_value=json.dumps({"key": "modelRoles", "value": {"default": "model"}})):
            self.assertEqual(DOTAI.configured_omp_value(runner, "modelRoles"), {"default": "model"})
        for output in ("", "[]", "{", json.dumps({}), json.dumps({"value": None})):
            with self.subTest(output=output), mock.patch.object(runner, "output", return_value=output):
                self.assertIsNone(DOTAI.configured_omp_value(runner, "modelRoles"))

    def test_configure_omp_routing_returns_failure_when_a_write_fails(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = DOTAI.validate_omp_routing({"roles": {"default": ["openai-codex/gpt-5.6-sol"]}})
        values = {
            "modelRoles": {},
            "retry.fallbackChains": {},
            "task.agentModelOverrides": {},
            "retry.modelFallback": False,
            "retry.usageAwareFallback": False,
            "retry.usageReservePct": 0,
            "retry.usageReservePolicy": "confirm",
            "retry.fallbackRevertPolicy": "never",
        }
        runner = DOTAI.Runner("ubuntu")
        catalog = json.dumps({"models": [{"selector": "openai-codex/gpt-5.6-sol"}]})

        def failed_write(_command: list[str], label: str) -> None:
            runner.failures.append(label)

        with (
            mock.patch.object(
                runner,
                "output",
                side_effect=lambda command: catalog if command[1] == "models" else json.dumps({"value": values[command[3]]}),
            ),
            mock.patch.object(runner, "run", side_effect=failed_write) as run,
        ):
            self.assertEqual(DOTAI.configure_omp_routing(manifest, runner), 1)
        self.assertTrue(run.called)

    def test_configure_omp_routing_parser_and_lifecycle_are_explicit(self) -> None:
        args = DOTAI.build_parser().parse_args(["configure", "omp-routing", "--dry-run"])
        self.assertEqual((args.command, args.configure_target, args.dry_run), ("configure", "omp-routing", True))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(DOTAI, "configure_omp_routing", return_value=7) as configure:
                self.assertEqual(DOTAI.main(["--manifest", str(path), "configure", "omp-routing", "--dry-run"]), 7)
            configure.assert_called_once()
            for command in ("install", "update", "sync"):
                with (
                    mock.patch.object(DOTAI, "available_omp_models", side_effect=AssertionError(command)),
                    mock.patch.object(DOTAI, "sync_mcp", return_value=False),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(DOTAI.main(["--manifest", str(path), command, "--dry-run"]), 0)

    def test_omp_extension_reconciliation_preserves_existing_entries(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        managed = "~/.pi/agent/extensions/rtk.ts"
        manifest["ompExtensions"] = [managed]
        runner = DOTAI.Runner("ubuntu")
        current = json.dumps({"key": "extensions", "value": ["~/custom/extension.ts"]})

        with (
            mock.patch.object(runner, "output", return_value=current),
            mock.patch.object(runner, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            DOTAI.reconcile_omp_extensions(manifest, runner)

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["omp", "config", "set", "extensions"])
        self.assertEqual(
            json.loads(command[4]),
            ["~/custom/extension.ts", managed],
        )

        dry_runner = DOTAI.Runner("ubuntu", dry_run=True)
        output = io.StringIO()
        with (
            mock.patch.object(dry_runner, "output", return_value=""),
            contextlib.redirect_stdout(output),
        ):
            DOTAI.reconcile_omp_extensions(manifest, dry_runner)
        self.assertFalse(dry_runner.failures)
        self.assertIn("OMP extensions: configure", output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / ".pi" / "agent" / "extensions" / "rtk.ts"
            source.parent.mkdir(parents=True)
            source.write_text("// test extension\n", encoding="utf-8")
            configured = json.dumps({"key": "extensions", "value": [managed]})
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}),
                mock.patch.object(runner, "output", return_value=configured),
            ):
                healthy, detail = DOTAI.omp_extension_status(manifest, runner)
            self.assertTrue(healthy, detail)

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
    def test_universal_skill_target_uses_omp_discovery_path(self) -> None:
        skill = {"source": "owner/skills", "agent": "universal", "checkSkills": ["alpha"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\nname: alpha\ndescription: test\n---\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                installed, detail = DOTAI.skill_status(skill)
            self.assertTrue(installed)
            self.assertEqual(detail, "installed for universal")
        self.assertEqual(
            DOTAI.skill_command(skill)[:9],
            ["npx", "--yes", "skills@latest", "add", "owner/skills", "--global", "--agent", "universal", "--skill"],
        )

    def test_status_highlights_legacy_pi_skill_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            skill_path = home / ".pi" / "agent" / "skills" / "legacy" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# legacy\n", encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "agent": "pi", "checkSkills": ["legacy"]}]
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                healthy = DOTAI.print_status(manifest, DOTAI.Runner("ubuntu"))
            self.assertFalse(healthy)
            self.assertIn("[DRIFT] Legacy Pi skill targets", output.getvalue())
            self.assertIn("Run 'dotai fix'", output.getvalue())

    def test_update_highlights_legacy_pi_skill_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "agent": "pi", "checkSkills": ["legacy"]}]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.object(DOTAI, "reconcile_packages"),
                mock.patch.object(DOTAI, "reconcile_omp_extensions"),
                mock.patch.object(DOTAI, "reconcile_skills"),
                mock.patch.object(DOTAI, "reconcile_plugins"),
                mock.patch.object(DOTAI, "sync_mcp", return_value=True),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "update", "--dry-run"]), 0)
            self.assertIn("[DRIFT] Legacy Pi skill targets", output.getvalue())
            self.assertIn("Run 'dotai fix'", output.getvalue())

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
            self.assertNotIn("Legacy Pi skill targets", plain.getvalue())

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
                    "--update-group",
                    "dependency",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["skills"][0]["source"], "owner/skills")
            self.assertEqual(value["skills"][0]["agent"], "universal")
            self.assertEqual(value["marketplaces"][0]["name"], "team")
            self.assertEqual(value["plugins"][0]["id"], "review@team")
            self.assertEqual(value["mcp"]["servers"]["local"]["args"], ["-y", "server-package"])
            self.assertEqual(value["packages"][0]["install"]["windows"], ["scoop install example"])
            self.assertEqual(value["packages"][0]["updateGroup"], "dependency")

    def test_skill_migration_updates_one_source_and_preserves_other_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [
                {
                    "source": "mattpocock/skills",
                    "agent": "pi",
                    "skills": ["grill-me", "grill-with-docs"],
                    "checkSkills": ["grill-me", "grill-with-docs"],
                },
                {
                    "source": "other/skills",
                    "agent": "pi",
                    "skills": ["other"],
                    "checkSkills": ["other"],
                },
            ]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    DOTAI.main(
                        [
                            "--manifest",
                            str(path),
                            "add",
                            "skill",
                            "mattpocock/skills",
                            "--agent",
                            "universal",
                            "--skill",
                            "grill-me",
                            "--skill",
                            "grill-with-docs",
                            "--check-skill",
                            "grill-me",
                            "--check-skill",
                            "grill-with-docs",
                        ]
                    ),
                    0,
                )
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"][0]["agent"], "universal")
            self.assertEqual(updated["skills"][1], manifest["skills"][1])

    def test_sync_does_not_rewrite_existing_skill_agent_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [
                {
                    "source": "mattpocock/skills",
                    "agent": "pi",
                    "skills": ["grill-me", "grill-with-docs"],
                    "checkSkills": ["grill-me", "grill-with-docs"],
                }
            ]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    DOTAI.main(["--manifest", str(path), "sync", "--dry-run"]),
                    0,
                )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)
            self.assertIn("--agent pi", output.getvalue())

    def test_fix_shows_diff_and_applies_after_confirmation_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [
                {
                    "source": "mattpocock/skills",
                    "agent": "pi",
                    "skills": ["grill-me", "grill-with-docs"],
                    "checkSkills": ["grill-me", "grill-with-docs"],
                },
                {
                    "source": "custom/skills",
                    "agent": "claude",
                    "skills": ["custom"],
                    "checkSkills": ["custom"],
                },
            ]
            original = json.dumps(manifest)
            path.write_text(original, encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch("builtins.input", return_value="y"),
                mock.patch.object(DOTAI, "reconcile_skills") as reconcile,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "fix"]), 0)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"][0]["agent"], "universal")
            self.assertEqual(updated["skills"][1], manifest["skills"][1])
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)
            self.assertEqual(json.loads(next(path.parent.glob("stack.json.bak.*")).read_text()), manifest)
            self.assertIn('"agent": "pi"', output.getvalue())
            self.assertIn('"agent": "universal"', output.getvalue())
            reconcile.assert_called_once()

    def test_fix_dry_run_shows_migration_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "agent": "pi", "checkSkills": ["one"]}]
            original = json.dumps(manifest)
            path.write_text(original, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "fix", "--dry-run"]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(list(path.parent.glob("stack.json.bak.*")))
            self.assertIn('"agent": "universal"', output.getvalue())
            self.assertIn("--agent universal", output.getvalue())

    def test_update_skips_dependency_group_unless_explicitly_included(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["packages"] = [
            {
                "name": "Core",
                "check": ["core", "--version"],
                "install": {"default": [["core", "install"]]},
                "update": {"default": [["core", "update"]]},
            },
            {
                "name": "Dependency",
                "updateGroup": "dependency",
                "check": ["dependency", "--version"],
                "install": {"default": [["dependency", "install"]]},
                "update": {"default": [["dependency", "update"]]},
            },
        ]
        runner = DOTAI.Runner("linux", dry_run=True)
        output = io.StringIO()
        with (
            mock.patch.object(DOTAI, "package_check", return_value=True),
            contextlib.redirect_stdout(output),
        ):
            DOTAI.reconcile_packages(manifest, runner, "update")
        plan = output.getvalue()
        self.assertIn("Check/update Core", plan)
        self.assertIn("Dependency: dependency update skipped", plan)
        self.assertNotIn("Check/update Dependency", plan)

        output = io.StringIO()
        with (
            mock.patch.object(
                DOTAI,
                "package_check",
                side_effect=lambda package, _runner: package["name"] == "Core",
            ),
            contextlib.redirect_stdout(output),
        ):
            DOTAI.reconcile_packages(manifest, runner, "update")
        self.assertIn("Install Dependency", output.getvalue())

        output = io.StringIO()
        with (
            mock.patch.object(DOTAI, "package_check", return_value=True),
            contextlib.redirect_stdout(output),
        ):
            DOTAI.reconcile_packages(manifest, runner, "update", include_dependencies=True)
        self.assertIn("Check/update Dependency", output.getvalue())

    def test_default_omp_update_uses_omp_updater_and_marks_dependencies(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        packages = {package["name"]: package for package in manifest["packages"]}
        self.assertEqual(DOTAI.selected(packages["Oh My Pi"]["update"], "wsl"), [["omp", "update"]])
        self.assertEqual(
            packages["Oh My Pi"]["configure"]["default"],
            [["omp", "config", "set", "secrets.enabled", "true"]],
        )
        self.assertEqual(packages["Node.js"]["updateGroup"], "dependency")
        self.assertEqual(packages["uv"]["updateGroup"], "dependency")

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
                default_local = dict(template)
                default_local["localOnly"] = True
                target.write_text(json.dumps(default_local, indent=2) + "\n", encoding="utf-8")
                self.assertEqual(DOTAI.main(["validate"]), 0)
                custom = root / "custom.json"
                with contextlib.redirect_stdout(output):
                    self.assertEqual(DOTAI.main(["--manifest", str(custom), "init"]), 0)
                self.assertEqual(json.loads(custom.read_text(encoding="utf-8")), template)
                custom_local = dict(template)
                custom_local["localOnly"] = True
                custom.write_text(json.dumps(custom_local, indent=2) + "\n", encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DOTAI.main(["--manifest", str(custom), "init"]), 2)
                self.assertEqual(json.loads(custom.read_text(encoding="utf-8")), custom_local)
                missing_custom = root / "missing.json"
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DOTAI.main(["--manifest", str(missing_custom), "validate"]), 2)
                self.assertFalse(missing_custom.exists())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), default_local)
            self.assertEqual(output.getvalue().count("Initialized"), 2)

    def test_repository_example_manifest_has_no_winget_commands(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        self.assertNotIn("winget", json.dumps(manifest).lower())
        serialized = json.dumps(manifest)
        rtk = next(package for package in manifest["packages"] if package["name"] == "RTK")
        self.assertIn(["rtk", "init", "-g", "--agent", "pi"], rtk["configure"]["default"])
        self.assertTrue(
            all(skill.get("agent") == "universal" for skill in manifest["skills"]),
        )
        self.assertEqual(manifest["ompExtensions"], ["~/.pi/agent/extensions/rtk.ts"])
        self.assertEqual(manifest["ompRouting"]["roles"]["default"][0], "openai-codex/gpt-5.6-sol")
        grill_me = next(skill for skill in manifest["skills"] if skill["source"] == "mattpocock/skills")
        self.assertEqual(grill_me["skills"], ["grill-me", "grill-with-docs"])
        self.assertEqual(grill_me["checkSkills"], ["grill-me", "grill-with-docs"])
        commit_and_document = next(skill for skill in manifest["skills"] if skill["source"] == "mathwro/Skills")
        self.assertEqual(commit_and_document["skills"], ["commit-and-document"])
        self.assertEqual(commit_and_document["checkSkills"], ["commit-and-document"])
        self.assertNotIn("--codex", serialized)
        self.assertIn("/stack.json", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertEqual(DOTAI.detect_platform(), os.environ.get("DOTAI_PLATFORM", DOTAI.detect_platform()))

    def test_version_warns_when_newer_release_exists(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"tag_name": "v0.2.0"}'
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(
            output.getvalue(),
            "0.1.0\n[UPDATE] DotAi 0.2.0 is available (current: 0.1.0); pull the repository to update.\n",
        )

    def test_release_warning_is_checked_by_status_sync_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            for command in ("status", "sync", "install"):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = b'{"tag_name": "v0.2.0"}'
                output = io.StringIO()
                argv = ["--manifest", str(path), command]
                if command != "status":
                    argv.append("--dry-run")
                with (
                    mock.patch("urllib.request.urlopen", return_value=response),
                    mock.patch.object(DOTAI, "print_status", return_value=True),
                    mock.patch.object(DOTAI, "reconcile", return_value=0),
                    mock.patch.object(DOTAI, "reconcile_omp_extensions"),
                    mock.patch.object(DOTAI, "reconcile_skills"),
                    mock.patch.object(DOTAI, "reconcile_plugins"),
                    mock.patch.object(DOTAI, "sync_mcp", return_value=True),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(DOTAI.main(argv), 0)
                self.assertIn("[UPDATE] DotAi 0.2.0", output.getvalue())

    def test_release_warning_is_silent_when_current_version_is_latest(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"tag_name": "0.1.0"}'
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(output.getvalue(), "0.1.0\n")

    def test_release_check_failure_does_not_change_version_output(self) -> None:
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(output.getvalue(), "0.1.0\n")

    def test_malformed_release_response_is_ignored(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(DOTAI.latest_release_version())

    def test_version_command_prints_current_version(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "dotai.py"), "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "0.1.0\n")



if __name__ == "__main__":
    unittest.main()
