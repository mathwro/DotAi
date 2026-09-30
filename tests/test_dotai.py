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

    def compact_routing(self, providers: list[str], primary: str) -> dict:
        return {
            "providers": providers,
            "primaryProvider": primary,
            "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
            "usageReservePct": 10,
            "usageReservePolicy": "auto",
            "fallbackRevertPolicy": "cooldown-expiry",
        }

    def omp_output(self, selectors: list[str], values: dict[str, object]):
        catalog = json.dumps({"models": [{"selector": selector} for selector in selectors]})

        def output(command: list[str]) -> str:
            if command == ["omp", "models", "--json"]:
                return catalog
            return json.dumps({"key": command[3], "value": values[command[3]]})

        return output

    def compact_status_case(self) -> tuple[dict, list[str], dict[str, object]]:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = self.compact_routing(
            ["anthropic", "github-copilot"], "anthropic"
        )
        selectors = [
            "anthropic/claude-opus-4-8",
            "anthropic/claude-haiku-4-5",
            "github-copilot/gpt-5.6-terra",
            "github-copilot/gpt-5.6-sol",
        ]
        values = {
            "modelRoles": {
                "custom": "private/keep",
                "default": "anthropic/claude-opus-4-8",
                "task": "github-copilot/gpt-5.6-terra",
                "smol": "github-copilot/gpt-5.6-terra",
                "slow": "anthropic/claude-opus-4-8:high",
            },
            "retry.fallbackChains": {
                "custom": ["private/keep"],
                "default": [
                    "anthropic/claude-opus-4-8",
                    "github-copilot/gpt-5.6-sol",
                ],
                "task": [
                    "github-copilot/gpt-5.6-terra",
                    "anthropic/claude-opus-4-8",
                ],
                "smol": [
                    "github-copilot/gpt-5.6-terra",
                    "anthropic/claude-haiku-4-5",
                ],
                "slow": [
                    "anthropic/claude-opus-4-8:high",
                    "github-copilot/gpt-5.6-sol:high",
                    "github-copilot/gpt-5.6-terra:high",
                ],
            },
            "task.agentModelOverrides": {
                "reviewer": "@slow",
                "sonic": "@smol",
                "task": "@task",
            },
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        return manifest, selectors, values

    def test_routing_recommendation_catalog_is_exact_and_valid(self) -> None:
        recommendations = DOTAI.load_routing_recommendations()
        self.assertEqual(recommendations["version"], 1)
        self.assertEqual(
            set(recommendations["providers"]),
            {"github-copilot", "openai-codex", "anthropic"},
        )
        self.assertEqual(
            recommendations["providers"]["anthropic"]["roles"],
            {
                "default": [
                    "anthropic/claude-opus-5",
                    "anthropic/claude-opus-4-8",
                    "anthropic/claude-opus-4-7",
                    "anthropic/claude-opus-4-6",
                ],
                "task": [
                    "anthropic/claude-sonnet-5",
                    "anthropic/claude-sonnet-4-6",
                    "anthropic/claude-opus-5",
                    "anthropic/claude-opus-4-8",
                ],
                "smol": [
                    "anthropic/claude-haiku-4-5",
                    "anthropic/claude-sonnet-5",
                    "anthropic/claude-sonnet-4-6",
                ],
                "slow": [
                    "anthropic/claude-fable-5-1:high",
                    "anthropic/claude-opus-5:high",
                    "anthropic/claude-opus-4-8:high",
                    "anthropic/claude-opus-4-7:high",
                    "anthropic/claude-opus-4-6:high",
                ],
            },
        )
        self.assertEqual(
            recommendations["providers"]["github-copilot"]["roles"]["default"][0],
            "github-copilot/gpt-6-astra",
        )
        self.assertEqual(
            recommendations["providers"]["github-copilot"]["roles"]["slow"][:2],
            [
                "github-copilot/gpt-6-astra:high",
                "github-copilot/gpt-5.6-sol:high",
            ],
        )
        self.assertEqual(
            recommendations["providers"]["openai-codex"]["roles"],
            {
                "default": [
                    "openai-codex/gpt-6-astra",
                    "openai-codex/gpt-5.6-sol",
                ],
                "task": [
                    "openai-codex/gpt-5.6-terra",
                    "openai-codex/gpt-5.6-sol",
                ],
                "smol": [
                    "openai-codex/gpt-5.6-luna",
                    "openai-codex/gpt-5.4-mini",
                ],
                "slow": [
                    "openai-codex/gpt-6-astra:high",
                    "openai-codex/gpt-5.6-sol:high",
                ],
            },
        )

    def test_validate_omp_routing_accepts_compact_intent_and_null(self) -> None:
        routing = self.compact_routing(["anthropic", "github-copilot"], "anthropic")
        self.assertEqual(DOTAI.validate_omp_routing(routing), routing)
        self.assertEqual(DOTAI.validate_omp_routing(None), {})

    def test_stack_schema_has_exact_omp_routing_defaults(self) -> None:
        schema = json.loads((ROOT / "stack.schema.json").read_text(encoding="utf-8"))
        properties = schema["properties"]["ompRouting"]["oneOf"][1]["properties"]
        self.assertEqual(
            {
                "usageReservePct": properties["usageReservePct"],
                "usageReservePolicy": properties["usageReservePolicy"],
                "fallbackRevertPolicy": properties["fallbackRevertPolicy"],
            },
            {
                "usageReservePct": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
                "usageReservePolicy": {"enum": ["confirm", "auto", "fail-closed"], "default": "auto"},
                "fallbackRevertPolicy": {"enum": ["cooldown-expiry", "never"], "default": "cooldown-expiry"},
            },
        )

    def test_validate_omp_routing_rejects_invalid_compact_intent(self) -> None:
        valid = self.compact_routing(["anthropic"], "anthropic")
        invalid = [
            [],
            {**valid, "providers": []},
            {**valid, "providers": ["anthropic", "anthropic"]},
            {**valid, "providers": [1]},
            {**valid, "providers": ["unknown"]},
            {**valid, "primaryProvider": "openai-codex"},
            {**valid, "agentModelOverrides": []},
            {**valid, "usageReservePct": True},
            {**valid, "usageReservePct": 101},
            {**valid, "usageReservePolicy": "ask"},
            {**valid, "fallbackRevertPolicy": "always"},
            {**valid, "unexpected": True},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DOTAI.DotAiError):
                DOTAI.validate_omp_routing(value)


    def test_compact_manifest_loads_without_routing_recommendations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = {
                "providers": ["anthropic"],
                "primaryProvider": "anthropic",
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            error = DOTAI.DotAiError("missing recommendations")
            with mock.patch.object(
                DOTAI, "load_routing_recommendations", side_effect=error
            ):
                loaded = DOTAI.load_manifest(path)
                self.assertEqual(loaded["ompRouting"]["providers"], ["anthropic"])
                self.assertEqual(
                    DOTAI.omp_routing_status(loaded, DOTAI.Runner("ubuntu")),
                    ("FAIL", "unable to read routing recommendations"),
                )

    def test_validate_routing_recommendations_rejects_malformed_data(self) -> None:
        valid = DOTAI.load_routing_recommendations()
        missing_role = json.loads(json.dumps(valid))
        del missing_role["providers"]["anthropic"]["roles"]["smol"]
        empty_role = json.loads(json.dumps(valid))
        empty_role["providers"]["anthropic"]["roles"]["smol"] = []
        cross_provider = json.loads(json.dumps(valid))
        cross_provider["providers"]["anthropic"]["roles"]["smol"] = [
            "openai-codex/gpt-5.4-mini"
        ]
        missing_provider = json.loads(json.dumps(valid))
        del missing_provider["providers"]["anthropic"]
        extra_provider = json.loads(json.dumps(valid))
        extra_provider["providers"]["other"] = extra_provider["providers"]["anthropic"]
        altered_overrides = json.loads(json.dumps(valid))
        altered_overrides["agentModelOverrides"]["sonic"] = "@task"
        missing_override = json.loads(json.dumps(valid))
        del missing_override["agentModelOverrides"]["task"]
        invalid = [
            {**valid, "version": 2},
            {"version": 1, "agentModelOverrides": {}},
            missing_role,
            empty_role,
            cross_provider,
            {**valid, "agentModelOverrides": []},
            missing_provider,
            extra_provider,
            altered_overrides,
            missing_override,
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DOTAI.DotAiError):
                DOTAI.validate_routing_recommendations(value)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-recommendations.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(DOTAI.DotAiError):
                DOTAI.load_routing_recommendations(path)

    def test_static_routing_is_only_accepted_for_configure_migration(self) -> None:
        legacy = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
        with self.assertRaisesRegex(DOTAI.DotAiError, "configure omp-routing"):
            DOTAI.validate_omp_routing(legacy)
        self.assertEqual(
            DOTAI.validate_omp_routing(legacy, allow_legacy=True)["roles"],
            legacy["roles"],
        )

    def test_load_manifest_rejects_non_object_roots_and_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for root in ([], [self.minimal_manifest("mcp.json")], None, "stack"):
                with self.subTest(root=root):
                    path.write_text(json.dumps(root), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

            for section in ("packages", "skills", "marketplaces", "plugins", "mcp"):
                with self.subTest(missing=section):
                    manifest = self.minimal_manifest("mcp.json")
                    del manifest[section]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

            for field in ("target", "servers"):
                with self.subTest(missing_mcp_field=field):
                    manifest = self.minimal_manifest("mcp.json")
                    del manifest["mcp"][field]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

    def test_load_manifest_rejects_malformed_packages_and_platform_commands(self) -> None:
        package = {
            "name": "sample", "check": ["sample", "--version"],
            "install": {"default": [["sample", "install"]]},
        }
        invalid = {
            "non-object": 1,
            "missing-name": {"check": ["sample"], "install": []},
            "invalid-name": {**package, "name": False},
            "missing-check": {"name": "sample", "install": []},
            "invalid-check": {**package, "check": ["sample", 42]},
            "invalid-platform-check": {**package, "check": {"unknown": ["sample"]}},
            "missing-install": {"name": "sample", "check": ["sample"]},
            "invalid-install": {**package, "install": "sample install"},
            "empty-command": {**package, "install": [""]},
            "invalid-argv": {**package, "install": [["sample", 42]]},
            "invalid-platform-command": {**package, "install": {"ubuntu": "sample install"}},
            "invalid-platform-name": {**package, "install": {"unknown": [["sample"]]}},
            "invalid-update": {**package, "update": [42]},
            "invalid-configure": {**package, "configure": {"default": [["sample", None]]}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for case, entry in invalid.items():
                with self.subTest(case=case):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["packages"] = [entry]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

    def test_load_manifest_rejects_malformed_skills_plugins_and_extensions(self) -> None:
        invalid = (
            ("skills", [None]),
            ("skills", [{}]),
            ("skills", [{"source": 4}]),
            ("skills", [{"source": "owner/skill", "agent": False}]),
            ("skills", [{"source": "owner/skill", "skills": [3]}]),
            ("skills", [{"source": "owner/skill", "checkSkills": "skill"}]),
            ("marketplaces", [{"name": "market"}]),
            ("marketplaces", [{"name": "market", "source": 12}]),
            ("plugins", [{"id": "not-a-plugin-id"}]),
            ("plugins", [{"id": "plugin@market", "scope": "machine"}]),
            ("ompExtensions", ["path", "path"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for section, entries in invalid:
                with self.subTest(section=section, entries=entries):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest[section] = entries
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

    def test_load_manifest_rejects_malformed_mcp_servers(self) -> None:
        invalid = {
            "non-object": [],
            "missing-stdio-command": {"type": "stdio"},
            "invalid-stdio-command": {"command": ["npx"]},
            "invalid-stdio-args": {"command": "npx", "args": ["-y", 9]},
            "invalid-stdio-env": {"command": "npx", "env": {"TOKEN": 3}},
            "invalid-stdio-cwd": {"command": "npx", "cwd": False},
            "missing-http-url": {"type": "http"},
            "invalid-http-url": {"type": "http", "url": "not a URI"},
            "invalid-port-name": {"type": "http", "url": "https://example.test:not-a-port/mcp"},
            "invalid-port-range": {"type": "sse", "url": "https://example.test:65536/mcp"},
            "invalid-http-headers": {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": 3}},
            "invalid-enabled": {"type": "sse", "url": "https://example.test/mcp", "enabled": "yes"},
            "invalid-timeout": {"command": "npx", "timeout": True},
            "unsupported-type": {"type": "grpc", "command": "npx"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for case, server in invalid.items():
                with self.subTest(case=case):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["mcp"]["servers"] = {"custom": server}
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

            for field, value in (("target", ""), ("servers", [])):
                with self.subTest(mcp_field=field):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["mcp"][field] = value
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(DOTAI.DotAiError):
                        DOTAI.load_manifest(path)

    def test_load_manifest_preserves_user_owned_extra_fields(self) -> None:
        manifest = self.minimal_manifest("mcp.json")
        manifest["localOnly"] = {"owner": "user"}
        manifest["mcp"]["servers"]["custom"] = {
            "command": "npx", "args": ["server"], "providerSetting": {"keep": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = DOTAI.load_manifest(path)
        self.assertEqual(loaded["localOnly"], {"owner": "user"})
        self.assertEqual(loaded["mcp"]["servers"]["custom"]["providerSetting"], {"keep": True})

    def test_load_manifest_normalizes_present_omp_routing_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertNotIn("ompRouting", DOTAI.load_manifest(path))

            manifest["ompRouting"] = {
                "providers": ["anthropic"],
                "primaryProvider": "anthropic",
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                DOTAI.load_manifest(path)["ompRouting"],
                {
                    "providers": ["anthropic"],
                    "primaryProvider": "anthropic",
                    "agentModelOverrides": {},
                    "usageReservePct": 10,
                    "usageReservePolicy": "auto",
                    "fallbackRevertPolicy": "cooldown-expiry",
                },
            )

    def test_repository_example_starts_with_unconfigured_routing(self) -> None:
        raw = json.loads((ROOT / "stack.example.json").read_text(encoding="utf-8"))
        self.assertIsNone(raw["ompRouting"])
        self.assertEqual(DOTAI.load_manifest(ROOT / "stack.example.json")["ompRouting"], {})

    def test_loaded_null_omp_routing_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = DOTAI.load_manifest(path)

        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output") as output, mock.patch.object(runner, "run") as run:
            self.assertEqual(DOTAI.omp_routing_status(loaded, runner), ("OK", "not configured in manifest"))
            report = io.StringIO()
            with mock.patch.object(DOTAI, "mcp_status", return_value=(True, "managed")), contextlib.redirect_stdout(report):
                self.assertTrue(DOTAI.print_status(loaded, runner))
        output.assert_not_called()
        run.assert_not_called()
        self.assertIn("[INACTIVE]", report.getvalue())
        self.assertIn("configure omp-routing --dry-run", report.getvalue())

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

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not Windows ACLs")
    def test_config_backups_are_private_even_when_sources_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"mcpServers": {"context7": {"type": "http", "url": "https://old.example/mcp", "headers": {"X-Key": "old-reference"}}}}),
                encoding="utf-8",
            )
            target.chmod(0o644)
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                self.assertTrue(DOTAI.sync_mcp(self.minimal_manifest("~/.omp/agent/mcp.json"), DOTAI.Runner("ubuntu")))
            backup = next(target.parent.glob("mcp.json.bak.*"))
            self.assertIn("old-reference", backup.read_text(encoding="utf-8"))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

            custom = home / "custom-stack.json"
            custom.write_text('{"env":"OLD_TOKEN"}', encoding="utf-8")
            custom.chmod(0o644)
            prior = DOTAI.backup_manifest(custom)
            self.assertEqual(prior.read_text(encoding="utf-8"), custom.read_text(encoding="utf-8"))
            self.assertEqual(prior.stat().st_mode & 0o777, 0o600)

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

    def test_mcp_sync_respects_disabled_server_without_reporting_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            original = json.dumps(
                {
                    "disabledServers": ["context7"],
                    "mcpServers": {
                        "context7": manifest["mcp"]["servers"]["context7"],
                        "personal": {"type": "http", "url": "https://personal.example/mcp"},
                    },
                    "customTopLevel": {"owner": "user"},
                }
            )
            target.write_text(original, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
                self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
            self.assertNotIn("OK", output.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_respects_disabled_reserved_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / "mcp.json"
            required = {"type": "http", "url": "https://example.test/mcp"}
            original = json.dumps({
                "disabledServers": ["second"],
                "mcpServers": {"second": required},
            })
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest(str(target))
            manifest["mcp"]["servers"] = {"first": required, "second": required}
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = DOTAI.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(DOTAI.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(home.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_replaces_non_object_managed_entry_and_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "context7": None,
                "personal": {"type": "http", "url": "https://personal.example/mcp"},
            }, "customTopLevel": {"owner": "user"}}
            target.write_text(json.dumps(original), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                try:
                    changed = DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu"))
                except AttributeError as exc:
                    self.fail(f"MCP sync crashed on a non-object managed entry: {exc}")
                self.assertTrue(changed)
                self.assertTrue(DOTAI.mcp_status(manifest)[0])
            updated = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(updated["mcpServers"]["context7"], manifest["mcp"]["servers"]["context7"])
            self.assertEqual(updated["mcpServers"]["personal"], original["mcpServers"]["personal"])
            self.assertEqual(updated["customTopLevel"], original["customTopLevel"])
            backup = next(target.parent.glob("mcp.json.bak.*"))
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_mcp_sync_does_not_duplicate_disabled_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            provider = home / ".config" / "opencode" / "opencode.json"
            provider.parent.mkdir(parents=True)
            provider.write_text(
                json.dumps(
                    {
                        "disabledServers": ["disabled-alias"],
                        "mcp": {
                            "disabled-alias": {
                                "type": "remote", "url": "https://mcp.context7.com/mcp"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            original = provider.read_text(encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
                self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
            self.assertNotIn("OK", output.getvalue())
            self.assertFalse(target.exists())
            self.assertEqual(provider.read_text(encoding="utf-8"), original)

    def test_mcp_sync_keeps_same_command_instances_in_their_managed_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 5},
                "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 5},
            }}
            target.write_text(json.dumps(original), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 30},
                "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 30},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                runner = DOTAI.Runner("ubuntu")
                self.assertTrue(DOTAI.sync_mcp(manifest, runner))
                updated = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(updated["mcpServers"], {
                    "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 30},
                    "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 30},
                })
                healthy, detail = DOTAI.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                after = target.read_bytes()
                self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                self.assertEqual(runner.failures, [])
                self.assertEqual(target.read_bytes(), after)
            backups = list(target.parent.glob("mcp.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_mcp_equivalent_requirements_share_one_healthy_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({"mcpServers": {
                "provider-alias": {"url": "https://shared.example/mcp", "headers": {"X-Auth": "SHARED_TOKEN_FROM_ENV"}},
            }})
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "first": {"url": "https://shared.example/mcp"},
                "second": {"url": "https://shared.example/mcp"},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                with self.subTest(operation="status"):
                    healthy, detail = DOTAI.mcp_status(manifest)
                    self.assertTrue(healthy, detail)
                with self.subTest(operation="sync"):
                    runner = DOTAI.Runner("ubuntu")
                    self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                    self.assertEqual(runner.failures, [])
                    self.assertEqual(target.read_text(encoding="utf-8"), original)
                    self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_does_not_overwrite_a_named_slot_satisfying_another_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({"mcpServers": {
                "b": {"url": "https://first.example/mcp", "headers": {"X-Auth": "FIRST_TOKEN_FROM_ENV"}},
            }})
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "a": {"url": "https://first.example/mcp"},
                "b": {"url": "https://second.example/mcp"},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = DOTAI.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(DOTAI.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_preserves_unmanaged_alias_colliding_with_another_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({
                "mcpServers": {"personal": {
                    "url": "https://personal.example/mcp", "timeout": 5,
                    "headers": {"X-Auth": "PERSONAL_TOKEN_FROM_ENV"},
                }},
                "servers": {"personal": {"url": "https://managed.example/mcp", "timeout": 5}},
                "customTopLevel": {"owner": "user"},
            })
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "managed": {"url": "https://managed.example/mcp", "timeout": 30}
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = DOTAI.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(DOTAI.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(DOTAI.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_ignores_malformed_args_on_unrelated_disabled_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "unrelated": {"command": "python3", "args": None, "enabled": False},
            }, "customTopLevel": {"owner": "user"}}
            target.write_text(json.dumps(original), encoding="utf-8")
            before = target.read_bytes()
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "managed": {"command": "python3", "args": ["-m", "example"]}
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = DOTAI.Runner("ubuntu", dry_run=dry_run)
                        try:
                            changed = DOTAI.sync_mcp(manifest, runner)
                        except TypeError as exc:
                            self.fail(f"MCP sync crashed on unrelated malformed args: {exc}")
                        self.assertTrue(changed)
                        self.assertEqual(runner.failures, [])
                        if dry_run:
                            self.assertEqual(target.read_bytes(), before)
                            self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])
                updated = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(updated["mcpServers"], {
                    "unrelated": {"command": "python3", "args": None, "enabled": False},
                    "managed": {"command": "python3", "args": ["-m", "example"]},
                })
                self.assertEqual(updated["customTopLevel"], {"owner": "user"})
                healthy, detail = DOTAI.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                after = target.read_bytes()
                self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                self.assertEqual(target.read_bytes(), after)
            backups = list(target.parent.glob("mcp.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_mcp_sync_reconciles_stdio_cwd_and_timeout_without_losing_alias_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local-alias": {
                                "type": "stdio", "command": "python3", "args": ["-m", "example"],
                                "cwd": "/workspace/old", "timeout": 5,
                                "env": {"TOKEN": "TOKEN_FROM_ENV", "LOCAL_SETTING": "keep"},
                                "providerOptions": {"retry": 2},
                            },
                            "personal": {"type": "http", "url": "https://personal.example/mcp"},
                        },
                        "customTopLevel": {"owner": "user"},
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "local": {
                    "type": "stdio", "command": "python3", "args": ["-m", "example"],
                    "cwd": "/workspace/current", "timeout": 30, "env": {"TOKEN": "TOKEN_FROM_ENV"},
                }
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                merged = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(set(merged["mcpServers"]), {"local-alias", "personal"})
                self.assertEqual(merged["mcpServers"]["local-alias"]["cwd"], "/workspace/current")
                self.assertEqual(merged["mcpServers"]["local-alias"]["timeout"], 30)
                self.assertEqual(
                    merged["mcpServers"]["local-alias"]["env"],
                    {"TOKEN": "TOKEN_FROM_ENV", "LOCAL_SETTING": "keep"},
                )
                self.assertEqual(merged["mcpServers"]["local-alias"]["providerOptions"], {"retry": 2})
                self.assertEqual(merged["customTopLevel"], {"owner": "user"})
                self.assertTrue(DOTAI.mcp_status(manifest)[0])
                after = target.read_bytes()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                self.assertEqual(target.read_bytes(), after)
                self.assertEqual(len(list(target.parent.glob("mcp.json.bak.*"))), 1)

    def test_mcp_sync_reconciles_remote_timeout_while_preserving_extra_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "remote-alias": {
                                "type": "remote", "url": "https://mcp.example.test/mcp", "timeout": 5,
                                "enabled": True, "headers": {"X-Extra": "EXTRA_FROM_ENV"},
                                "providerOptions": {"keep": True},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "remote": {
                    "type": "http", "url": "https://mcp.example.test/mcp",
                    "timeout": 20, "enabled": True,
                }
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                self.assertFalse(DOTAI.mcp_status(manifest)[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(DOTAI.sync_mcp(manifest, DOTAI.Runner("ubuntu")))
                merged = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
                self.assertEqual(set(merged), {"remote-alias"})
                self.assertEqual(merged["remote-alias"]["timeout"], 20)
                self.assertEqual(merged["remote-alias"]["headers"], {"X-Extra": "EXTRA_FROM_ENV"})
                self.assertEqual(merged["remote-alias"]["providerOptions"], {"keep": True})
                self.assertTrue(DOTAI.mcp_status(manifest)[0])

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

    def test_detected_routing_providers_require_supported_recommendations(self) -> None:
        recommendations = DOTAI.load_routing_recommendations()
        available = {
            "github-copilot/gpt-5.6-terra",
            "openai-codex/gpt-5.6-sol",
            "private/model",
        }
        self.assertEqual(
            DOTAI.detected_routing_providers(recommendations, available),
            ["github-copilot", "openai-codex"],
        )
        with self.assertRaisesRegex(DOTAI.DotAiError, "no recommended models"):
            DOTAI.detected_routing_providers(
                recommendations,
                {"anthropic/claude-unknown"},
            )
        self.assertEqual(
            DOTAI.detected_routing_providers(recommendations, {"private/model"}),
            [],
        )

    def test_choose_primary_provider_applies_subscription_rules(self) -> None:
        cases = [
            (["github-copilot"], None, None, "github-copilot"),
            (["anthropic"], None, None, "anthropic"),
            (["openai-codex"], None, None, "openai-codex"),
            (["anthropic", "github-copilot"], None, None, "anthropic"),
            (["github-copilot", "openai-codex"], None, None, "openai-codex"),
            (["anthropic", "openai-codex"], "anthropic", None, "anthropic"),
            (["anthropic", "openai-codex"], "openai-codex", None, "openai-codex"),
            (["anthropic", "openai-codex"], None, "anthropic", "anthropic"),
        ]
        for providers, current, requested, expected in cases:
            with self.subTest(providers=providers, current=current, requested=requested):
                self.assertEqual(
                    DOTAI.choose_primary_provider(providers, current, requested),
                    expected,
                )

        for response, expected in (("1", "anthropic"), ("2", "openai-codex")):
            with (
                self.subTest(response=response),
                mock.patch.object(sys.stdin, "isatty", return_value=True),
                mock.patch("builtins.input", return_value=response) as prompt,
            ):
                self.assertEqual(
                    DOTAI.choose_primary_provider(
                        ["anthropic", "openai-codex"], None, None
                    ),
                    expected,
                )
                prompt.assert_called_once_with(
                    "Choose interactive primary: [1] Anthropic [2] OpenAI Codex: "
                )

        with (
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            self.assertRaisesRegex(DOTAI.DotAiError, "--primary"),
        ):
            DOTAI.choose_primary_provider(["anthropic", "openai-codex"], None, None)

        with self.assertRaisesRegex(DOTAI.DotAiError, "--primary.*not available"):
            DOTAI.choose_primary_provider(["anthropic"], None, "openai-codex")

        for providers, requested in (
            (["github-copilot"], "anthropic"),
            (["anthropic"], "github-copilot"),
            (["anthropic", "openai-codex"], "github-copilot"),
            (["anthropic", "openai-codex"], "private"),
        ):
            with (
                self.subTest(providers=providers, requested=requested),
                self.assertRaisesRegex(DOTAI.DotAiError, "--primary.*not available"),
            ):
                DOTAI.choose_primary_provider(providers, None, requested)

        with (
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="3") as prompt,
            self.assertRaisesRegex(DOTAI.DotAiError, "Invalid primary selection"),
        ):
            DOTAI.choose_primary_provider(["anthropic", "openai-codex"], None, None)
        prompt.assert_called_once()

        cancellation_errors = []
        for interruption in (EOFError, KeyboardInterrupt):
            with (
                self.subTest(interruption=interruption.__name__),
                mock.patch.object(sys.stdin, "isatty", return_value=True),
                mock.patch("builtins.input", side_effect=interruption),
                self.assertRaises(DOTAI.DotAiError) as raised,
            ):
                DOTAI.choose_primary_provider(
                    ["anthropic", "openai-codex"], None, None
                )
            cancellation_errors.append(str(raised.exception))
        self.assertEqual(cancellation_errors, ["Primary selection cancelled"] * 2)

    def test_resolve_omp_routing_handles_provider_combinations(self) -> None:
        recommendations = DOTAI.load_routing_recommendations()
        provider_roles = recommendations["providers"]

        def available_for(providers: list[str]) -> set[str]:
            return {
                DOTAI.selector_identity(provider_roles[provider]["roles"][role][0])
                for provider in providers
                for role in DOTAI.ROUTING_ROLES
            }

        copilot = {
            "default": "github-copilot/gpt-6-astra",
            "task": "github-copilot/gpt-5.6-terra",
            "smol": "github-copilot/gpt-5.6-luna",
            "slow": "github-copilot/gpt-6-astra:high",
        }
        anthropic = {
            "default": "anthropic/claude-opus-5",
            "task": "anthropic/claude-sonnet-5",
            "smol": "anthropic/claude-haiku-4-5",
            "slow": "anthropic/claude-fable-5-1:high",
        }
        codex = {
            "default": "openai-codex/gpt-6-astra",
            "task": "openai-codex/gpt-5.6-terra",
            "smol": "openai-codex/gpt-5.6-luna",
            "slow": "openai-codex/gpt-6-astra:high",
        }
        cases = [
            (["github-copilot"], "github-copilot", copilot),
            (["anthropic"], "anthropic", anthropic),
            (["openai-codex"], "openai-codex", codex),
            (
                ["github-copilot", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": codex["slow"]},
            ),
            (
                ["anthropic", "github-copilot"],
                "anthropic",
                {"default": anthropic["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "openai-codex"],
                "anthropic",
                {"default": anthropic["default"], "task": anthropic["task"], "smol": anthropic["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": anthropic["task"], "smol": anthropic["smol"], "slow": codex["slow"]},
            ),
            (
                ["anthropic", "github-copilot", "openai-codex"],
                "anthropic",
                {"default": anthropic["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "github-copilot", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": codex["slow"]},
            ),
        ]
        worker_order = ["github-copilot", "anthropic", "openai-codex"]
        expected_candidates = {
            "github-copilot": {
                "default": [copilot["default"]],
                "task": [copilot["task"], copilot["smol"]],
                "smol": [copilot["smol"], copilot["task"]],
                "slow": [
                    copilot["slow"],
                    "github-copilot/gpt-5.6-terra:high",
                    "github-copilot/gpt-5.6-luna:high",
                ],
            },
            "anthropic": {
                "default": [anthropic["default"]],
                "task": [anthropic["task"], anthropic["default"]],
                "smol": [anthropic["smol"], anthropic["task"]],
                "slow": [
                    anthropic["slow"],
                    "anthropic/claude-opus-5:high",
                ],
            },
            "openai-codex": {
                role: [selector] for role, selector in codex.items()
            },
        }

        for providers, primary, expected_primaries in cases:
            with self.subTest(providers=providers, primary=primary):
                primaries, fallbacks, unavailable = DOTAI.resolve_omp_routing(
                    recommendations, providers, primary, available_for(providers)
                )
                self.assertEqual(primaries, expected_primaries)
                self.assertEqual(unavailable, [])
                premium_order = [
                    primary,
                    *(
                        provider
                        for provider in ("anthropic", "openai-codex")
                        if provider in providers and provider != primary
                    ),
                    *(
                        provider
                        for provider in ("github-copilot",)
                        if provider in providers and provider != primary
                    ),
                ]
                for role in ("default", "slow"):
                    self.assertEqual(
                        fallbacks[role],
                        [
                            selector
                            for provider in premium_order
                            for selector in expected_candidates[provider][role]
                        ],
                    )
                for role in ("task", "smol"):
                    self.assertEqual(
                        fallbacks[role],
                        [
                            selector
                            for provider in worker_order
                            if provider in providers
                            for selector in expected_candidates[provider][role]
                        ],
                    )

        duplicate_recommendations = {
            "providers": {
                "anthropic": {
                    "roles": {
                        "default": ["anthropic/model", "anthropic/model"],
                        "task": ["anthropic/task"],
                        "smol": ["anthropic/missing"],
                        "slow": ["anthropic/model:high", "anthropic/model:high"],
                    }
                }
            }
        }
        primaries, fallbacks, unavailable = DOTAI.resolve_omp_routing(
            duplicate_recommendations,
            ["anthropic"],
            "anthropic",
            {"anthropic/model", "anthropic/task"},
        )
        self.assertEqual(
            primaries,
            {
                "default": "anthropic/model",
                "task": "anthropic/task",
                "slow": "anthropic/model:high",
            },
        )
        self.assertEqual(fallbacks["default"], ["anthropic/model"])
        self.assertEqual(fallbacks["slow"], ["anthropic/model:high"])
        self.assertEqual(unavailable, ["smol"])

    def test_configure_omp_routing_persists_compact_intent_and_preserves_omp_values(self) -> None:
        selectors = [
            "github-copilot/gpt-5.6-sol",
            "github-copilot/gpt-5.6-terra",
            "github-copilot/gpt-5.6-luna",
            "openai-codex/gpt-5.6-sol",
            "openai-codex/gpt-5.4-mini",
        ]
        values = {
            "modelRoles": {"custom": "private/keep", "default": "old/model"},
            "retry.fallbackChains": {"custom": ["private/keep"], "default": ["old/model"]},
            "task.agentModelOverrides": {"reviewer": "@slow"},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            runner = DOTAI.Runner("ubuntu")

            def persisted_before_omp(_command: list[str], _label: str) -> None:
                saved = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
                self.assertEqual(saved["primaryProvider"], "openai-codex")

            with (
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run", side_effect=persisted_before_omp) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner), 0)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["ompRouting"]["providers"], ["github-copilot", "openai-codex"])
            self.assertEqual(saved["ompRouting"]["primaryProvider"], "openai-codex")
            self.assertNotIn("roles", saved["ompRouting"])
            self.assertEqual(saved["ompRouting"]["agentModelOverrides"], {"sonic": "@smol", "task": "@task"})
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)

            payloads = {call.args[0][3]: json.loads(call.args[0][4]) for call in run.call_args_list}
            self.assertEqual(
                payloads["modelRoles"],
                {
                    "custom": "private/keep",
                    "default": "openai-codex/gpt-5.6-sol",
                    "task": "github-copilot/gpt-5.6-terra",
                    "smol": "github-copilot/gpt-5.6-luna",
                    "slow": "openai-codex/gpt-5.6-sol:high",
                },
            )
            self.assertEqual(
                payloads["retry.fallbackChains"],
                {
                    "custom": ["private/keep"],
                    "default": ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-sol"],
                    "task": [
                        "github-copilot/gpt-5.6-terra",
                        "github-copilot/gpt-5.6-luna",
                        "openai-codex/gpt-5.6-sol",
                    ],
                    "smol": [
                        "github-copilot/gpt-5.6-luna",
                        "github-copilot/gpt-5.6-terra",
                        "openai-codex/gpt-5.4-mini",
                    ],
                    "slow": [
                        "openai-codex/gpt-5.6-sol:high",
                        "github-copilot/gpt-5.6-sol:high",
                        "github-copilot/gpt-5.6-terra:high",
                        "github-copilot/gpt-5.6-luna:high",
                    ],
                },
            )
            self.assertEqual(
                payloads["task.agentModelOverrides"],
                {"reviewer": "@slow", "sonic": "@smol", "task": "@task"},
            )

    def test_configure_omp_routing_dry_run_changes_nothing(self) -> None:
        selectors = [
            "github-copilot/gpt-5.6-sol",
            "github-copilot/gpt-5.6-terra",
            "github-copilot/gpt-5.6-luna",
            "openai-codex/gpt-5.6-sol",
            "openai-codex/gpt-5.4-mini",
        ]
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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            before = path.read_bytes()
            runner = DOTAI.Runner("ubuntu", dry_run=True)
            report = io.StringIO()
            with (
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(report),
            ):
                self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner), 0)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
            run.assert_not_called()
            preview = report.getvalue()
            for expected in (
                "stack.json (proposed)",
                "discovered providers: github-copilot, openai-codex",
                "interactive primary: openai-codex",
                "resolved role primaries:",
                "fallback chains:",
                "pending OMP commands:",
                "omp config set modelRoles",
                "Dry run: no manifest or OMP changes applied",
            ):
                self.assertIn(expected, preview)

    def test_configure_omp_routing_is_idempotent(self) -> None:
        selectors = [
            "github-copilot/gpt-5.6-sol",
            "github-copilot/gpt-5.6-terra",
            "github-copilot/gpt-5.6-luna",
            "openai-codex/gpt-5.6-sol",
            "openai-codex/gpt-5.4-mini",
        ]
        routing = self.compact_routing(["github-copilot", "openai-codex"], "openai-codex")
        values = {
            "modelRoles": {
                "custom": "private/keep",
                "default": "openai-codex/gpt-5.6-sol",
                "task": "github-copilot/gpt-5.6-terra",
                "smol": "github-copilot/gpt-5.6-luna",
                "slow": "openai-codex/gpt-5.6-sol:high",
            },
            "retry.fallbackChains": {
                "custom": ["private/keep"],
                "default": ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-sol"],
                "task": [
                    "github-copilot/gpt-5.6-terra",
                    "github-copilot/gpt-5.6-luna",
                    "openai-codex/gpt-5.6-sol",
                ],
                "smol": [
                    "github-copilot/gpt-5.6-luna",
                    "github-copilot/gpt-5.6-terra",
                    "openai-codex/gpt-5.4-mini",
                ],
                "slow": [
                    "openai-codex/gpt-5.6-sol:high",
                    "github-copilot/gpt-5.6-sol:high",
                    "github-copilot/gpt-5.6-terra:high",
                    "github-copilot/gpt-5.6-luna:high",
                ],
            },
            "task.agentModelOverrides": {"reviewer": "@slow", **routing["agentModelOverrides"]},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = routing
            path.write_text(json.dumps(manifest), encoding="utf-8")
            before = path.read_bytes()
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
            run.assert_not_called()

    def test_configure_omp_routing_migrates_static_roles(self) -> None:
        selectors = ["openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.4-mini"]
        values = {
            "modelRoles": {
                "default": "openai-codex/gpt-5.6-sol",
                "task": "openai-codex/gpt-5.6-sol",
                "smol": "openai-codex/gpt-5.4-mini",
                "slow": "openai-codex/gpt-5.6-sol:high",
            },
            "retry.fallbackChains": {
                "default": ["openai-codex/gpt-5.6-sol"],
                "task": ["openai-codex/gpt-5.6-sol"],
                "smol": ["openai-codex/gpt-5.4-mini"],
                "slow": ["openai-codex/gpt-5.6-sol:high"],
            },
            "task.agentModelOverrides": {"sonic": "@slow", "reviewer": "@task"},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 23,
            "retry.usageReservePolicy": "fail-closed",
            "retry.fallbackRevertPolicy": "never",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            raw = self.minimal_manifest("~/.omp/agent/mcp.json")
            raw["ompRouting"] = {
                "roles": {"default": ["old/exact-model"]},
                "agentModelOverrides": {"sonic": "@slow", "reviewer": "@task"},
                "usageReservePct": 23,
                "usageReservePolicy": "fail-closed",
                "fallbackRevertPolicy": "never",
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            manifest = DOTAI.load_manifest(path, allow_legacy_routing=True)
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner), 0)

            run.assert_not_called()
            backups = list(path.parent.glob("stack.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("roles", json.loads(backups[0].read_text(encoding="utf-8"))["ompRouting"])
            routing = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
            self.assertEqual(routing["providers"], ["openai-codex"])
            self.assertEqual(routing["primaryProvider"], "openai-codex")
            self.assertNotIn("roles", routing)
            self.assertEqual(routing["agentModelOverrides"], {"sonic": "@slow", "reviewer": "@task"})
            self.assertEqual(
                (routing["usageReservePct"], routing["usageReservePolicy"], routing["fallbackRevertPolicy"]),
                (23, "fail-closed", "never"),
            )

    def test_configure_omp_routing_preflight_failures_change_nothing(self) -> None:
        valid_codex = json.dumps(
            {
                "models": [
                    {"selector": "openai-codex/gpt-5.6-sol"},
                    {"selector": "openai-codex/gpt-5.4-mini"},
                ]
            }
        )
        cases = [
            ("malformed recommendations", lambda _command: "", None, DOTAI.DotAiError("bad catalog"), True),
            ("malformed model catalog", lambda _command: "{", None, None, False),
            (
                "no supported provider",
                lambda _command: json.dumps({"models": [{"selector": "private/model"}]}),
                None,
                None,
                True,
            ),
            (
                "stale recommendations",
                lambda _command: json.dumps({"models": [{"selector": "anthropic/claude-unknown"}]}),
                None,
                None,
                True,
            ),
            (
                "unavailable managed role",
                lambda _command: json.dumps({"models": [{"selector": "openai-codex/gpt-5.6-sol"}]}),
                None,
                None,
                False,
            ),
            (
                "malformed OMP configuration",
                lambda command: valid_codex if command == ["omp", "models", "--json"] else "{",
                None,
                None,
                False,
            ),
            ("unavailable primary choice", lambda _command: valid_codex, "anthropic", None, True),
        ]
        for name, output, requested, catalog_error, raises in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "stack.json"
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                manifest["ompRouting"] = None
                path.write_text(json.dumps(manifest), encoding="utf-8")
                before = path.read_bytes()
                runner = DOTAI.Runner("ubuntu")
                loader = (
                    mock.patch.object(DOTAI, "load_routing_recommendations", side_effect=catalog_error)
                    if catalog_error
                    else contextlib.nullcontext()
                )
                with (
                    loader,
                    mock.patch.object(runner, "output", side_effect=output),
                    mock.patch.object(runner, "run") as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if raises:
                        with self.assertRaises(DOTAI.DotAiError):
                            DOTAI.configure_omp_routing(manifest, path, runner, requested)
                    else:
                        self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner, requested), 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
                run.assert_not_called()

    def test_configured_omp_value_requires_a_json_value_object(self) -> None:
        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output", return_value=json.dumps({"key": "modelRoles", "value": {"default": "model"}})):
            self.assertEqual(DOTAI.configured_omp_value(runner, "modelRoles"), {"default": "model"})
        for output in ("", "[]", "{", json.dumps({}), json.dumps({"value": None})):
            with self.subTest(output=output), mock.patch.object(runner, "output", return_value=output):
                self.assertIsNone(DOTAI.configured_omp_value(runner, "modelRoles"))

    def test_configure_omp_routing_returns_failure_after_manifest_persistence(self) -> None:
        selectors = ["openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.4-mini"]
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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            runner = DOTAI.Runner("ubuntu")

            def fail_first_write(_command: list[str], label: str) -> None:
                if not runner.failures:
                    runner.failures.append(label)

            with (
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run", side_effect=fail_first_write),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DOTAI.configure_omp_routing(manifest, path, runner), 1)

            saved = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
            self.assertEqual(saved["providers"], ["openai-codex"])
            self.assertNotIn("roles", saved)
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)

    def test_runner_formats_only_original_supported_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "{python}"
            literal_json = '{"literal":{"braces":true}}'
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                runner = DOTAI.Runner("ubuntu")
                self.assertEqual(runner.argv(["tool", "{home}"]), ["tool", str(home)])
                self.assertEqual(
                    runner.argv(["{home}", "{repo}", "{python}", literal_json]),
                    [str(home), str(DOTAI.ROOT), sys.executable, literal_json],
                )

    def test_configure_omp_routing_parser_and_lifecycle_are_explicit(self) -> None:
        args = DOTAI.build_parser().parse_args(
            ["configure", "omp-routing", "--primary", "anthropic", "--dry-run"]
        )
        self.assertEqual(
            (args.configure_target, args.primary, args.dry_run),
            ("omp-routing", "anthropic", True),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(DOTAI, "configure_omp_routing", return_value=7) as configure:
                self.assertEqual(
                    DOTAI.main(
                        [
                            "--manifest",
                            str(path),
                            "configure",
                            "omp-routing",
                            "--primary",
                            "anthropic",
                            "--dry-run",
                        ]
                    ),
                    7,
                )
            called_manifest, called_path, called_runner, called_primary = configure.call_args.args
            self.assertIn("roles", called_manifest["ompRouting"])
            self.assertEqual(called_path, path)
            self.assertTrue(called_runner.dry_run)
            self.assertEqual(called_primary, "anthropic")

            error = io.StringIO()
            with (
                mock.patch.object(
                    DOTAI,
                    "configure_omp_routing",
                    side_effect=DOTAI.DotAiError("Primary selection cancelled"),
                ),
                contextlib.redirect_stderr(error),
            ):
                self.assertEqual(
                    DOTAI.main(["--manifest", str(path), "configure", "omp-routing"]),
                    2,
                )
            self.assertIn("Primary selection cancelled", error.getvalue())

            commands = {
                "validate": ["validate"],
                "status": ["status"],
                "install": ["install", "--dry-run"],
                "update": ["update", "--dry-run"],
                "sync": ["sync", "--dry-run"],
            }
            for name, command in commands.items():
                error = io.StringIO()
                with self.subTest(command=name):
                    with (
                        mock.patch.object(DOTAI, "print_release_notice"),
                        mock.patch.object(DOTAI, "available_omp_models", side_effect=AssertionError(name)),
                        contextlib.redirect_stderr(error),
                    ):
                        self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 2)
                self.assertIn("configure omp-routing", error.getvalue())

            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            for command in ("install", "update", "sync"):
                with self.subTest(isolated_command=command):
                    with (
                        mock.patch.object(DOTAI, "print_release_notice"),
                        mock.patch.object(DOTAI, "available_omp_models", side_effect=AssertionError(command)),
                        mock.patch.object(DOTAI, "sync_mcp", return_value=False),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(DOTAI.main(["--manifest", str(path), command, "--dry-run"]), 0)

    def test_omp_routing_status_reports_ok_for_compact_intent(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        original_manifest = json.loads(json.dumps(manifest))
        runner = DOTAI.Runner("ubuntu")
        with (
            mock.patch.object(
                runner, "output", side_effect=self.omp_output(selectors, values)
            ),
            mock.patch.object(runner, "run") as run,
            mock.patch.object(
                DOTAI,
                "choose_primary_provider",
                side_effect=AssertionError("status must not select or prompt"),
            ),
        ):
            self.assertEqual(
                DOTAI.omp_routing_status(manifest, runner),
                ("OK", "configured roles match"),
            )
        run.assert_not_called()
        self.assertEqual(manifest, original_manifest)

    def test_omp_routing_status_reports_provider_selection_drift(self) -> None:
        manifest, selectors, _ = self.compact_status_case()
        cases = {
            "provider added": [*selectors, "openai-codex/gpt-5.6-sol"],
            "provider removed": selectors[:2],
        }
        for name, available in cases.items():
            with self.subTest(name=name):
                runner = DOTAI.Runner("ubuntu")
                with (
                    mock.patch.object(
                        runner,
                        "output",
                        return_value=json.dumps(
                            {
                                "models": [
                                    {"selector": selector} for selector in available
                                ]
                            }
                        ),
                    ) as output,
                    mock.patch.object(runner, "run") as run,
                    mock.patch.object(
                        DOTAI,
                        "choose_primary_provider",
                        side_effect=AssertionError("status must not select or prompt"),
                    ),
                ):
                    self.assertEqual(
                        DOTAI.omp_routing_status(manifest, runner),
                        (
                            "DRIFT",
                            "authenticated providers changed; run 'dotai configure omp-routing'",
                        ),
                    )
                output.assert_called_once_with(["omp", "models", "--json"])
                run.assert_not_called()

    def test_omp_routing_status_reports_omp_drift(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        cases = [
            (
                "modelRoles",
                {**values["modelRoles"], "default": "old/model"},
                "model role differs: default",
            ),
            (
                "retry.fallbackChains",
                {**values["retry.fallbackChains"], "task": ["old/model"]},
                "fallback chain differs: task",
            ),
            (
                "task.agentModelOverrides",
                {**values["task.agentModelOverrides"], "sonic": "old/model"},
                "agent override differs: sonic",
            ),
            ("retry.modelFallback", False, "retry.modelFallback differs"),
            (
                "retry.usageAwareFallback",
                False,
                "retry.usageAwareFallback differs",
            ),
            ("retry.usageReservePct", 5, "retry.usageReservePct differs"),
            (
                "retry.usageReservePolicy",
                "confirm",
                "retry.usageReservePolicy differs",
            ),
            (
                "retry.fallbackRevertPolicy",
                "never",
                "retry.fallbackRevertPolicy differs",
            ),
        ]
        for key, changed, detail in cases:
            with self.subTest(detail=detail):
                runner = DOTAI.Runner("ubuntu")
                current = {**values, key: changed}
                with (
                    mock.patch.object(
                        runner,
                        "output",
                        side_effect=self.omp_output(selectors, current),
                    ),
                    mock.patch.object(runner, "run") as run,
                    mock.patch.object(
                        DOTAI,
                        "choose_primary_provider",
                        side_effect=AssertionError("status must not select or prompt"),
                    ),
                ):
                    self.assertEqual(
                        DOTAI.omp_routing_status(manifest, runner),
                        ("DRIFT", detail),
                    )
                run.assert_not_called()

        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = self.compact_routing(["anthropic"], "anthropic")
        runner = DOTAI.Runner("ubuntu")
        with (
            mock.patch.object(
                runner,
                "output",
                return_value=json.dumps(
                    {"models": [{"selector": "anthropic/claude-opus-4-8"}]}
                ),
            ) as output,
            mock.patch.object(runner, "run") as run,
            mock.patch.object(
                DOTAI,
                "choose_primary_provider",
                side_effect=AssertionError("status must not select or prompt"),
            ),
        ):
            self.assertEqual(
                DOTAI.omp_routing_status(manifest, runner),
                ("DRIFT", "unavailable roles: smol"),
            )
        output.assert_called_once_with(["omp", "models", "--json"])
        run.assert_not_called()

    def test_omp_routing_status_reports_inactive_and_fail(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        runner = DOTAI.Runner("ubuntu")

        with (
            mock.patch.object(
                runner,
                "output",
                return_value=json.dumps(
                    {"models": [{"selector": "openai-codex/gpt-5.6-sol"}]}
                ),
            ) as output,
            mock.patch.object(runner, "run") as run,
        ):
            self.assertEqual(
                DOTAI.omp_routing_status(manifest, runner),
                ("INACTIVE", "configured providers are unavailable"),
            )
        output.assert_called_once_with(["omp", "models", "--json"])
        run.assert_not_called()

        failures = [
            (
                mock.patch.object(
                    DOTAI,
                    "load_routing_recommendations",
                    side_effect=DOTAI.DotAiError("malformed recommendations"),
                ),
                mock.patch.object(runner, "output"),
                ("FAIL", "unable to read routing recommendations"),
            ),
            (
                contextlib.nullcontext(),
                mock.patch.object(runner, "output", return_value="{"),
                ("FAIL", "unable to read OMP model catalog"),
            ),
            (
                contextlib.nullcontext(),
                mock.patch.object(
                    runner,
                    "output",
                    side_effect=self.omp_output(
                        selectors,
                        {**values, "modelRoles": None},
                    ),
                ),
                ("FAIL", "unable to read required OMP configuration"),
            ),
        ]
        for loader, output_patch, expected in failures:
            with self.subTest(expected=expected), loader, output_patch as output, mock.patch.object(
                runner, "run"
            ) as run:
                self.assertEqual(DOTAI.omp_routing_status(manifest, runner), expected)
            run.assert_not_called()

        for routing in (None, "absent"):
            with self.subTest(routing=routing):
                unconfigured = self.minimal_manifest("~/.omp/agent/mcp.json")
                if routing is None:
                    unconfigured["ompRouting"] = None
                with (
                    mock.patch.object(
                        DOTAI,
                        "load_routing_recommendations",
                        side_effect=AssertionError("recommendations must not load"),
                    ),
                    mock.patch.object(
                        runner,
                        "output",
                        side_effect=AssertionError("OMP must not be queried"),
                    ),
                    mock.patch.object(runner, "run") as run,
                ):
                    self.assertEqual(
                        DOTAI.omp_routing_status(unconfigured, runner),
                        ("OK", "not configured in manifest"),
                    )
                run.assert_not_called()

    def test_print_status_reports_configured_routing_health(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        runner = DOTAI.Runner("ubuntu")
        manifest["ompRouting"] = self.compact_routing(["anthropic"], "anthropic")
        DOTAI.configure_color("always")
        self.addCleanup(DOTAI.configure_color, "never")
        cases = [
            ("OK", "configured roles match", True, "\033[32;1m[OK]\033[0m"),
            (
                "DRIFT",
                "model role differs: default",
                False,
                "\033[33;1m[DRIFT]\033[0m",
            ),
            (
                "INACTIVE",
                "configured providers are unavailable",
                False,
                "\033[33;1m[INACTIVE]\033[0m",
            ),
            (
                "FAIL",
                "unable to read OMP model catalog",
                False,
                "\033[31;1m[FAIL]\033[0m",
            ),
        ]
        for label, detail, healthy, colored_badge in cases:
            with self.subTest(label=label):
                output = io.StringIO()
                with (
                    mock.patch.object(
                        DOTAI, "mcp_status", return_value=(True, "managed")
                    ),
                    mock.patch.object(
                        DOTAI, "omp_routing_status", return_value=(label, detail)
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(DOTAI.print_status(manifest, runner), healthy)
                self.assertIn("OMP routing:", output.getvalue())
                self.assertIn(f"{colored_badge} {detail}", output.getvalue())

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

    def github_skill_lock_entry(self, source: str, folder_hash: str, name: str = "alpha") -> dict:
        return {
            "source": source,
            "sourceType": "github",
            "sourceUrl": f"https://github.com/{source}.git",
            "skillPath": f"skills/{name}/SKILL.md",
            "skillFolderHash": folder_hash,
            "installedAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def test_sync_uses_xdg_skill_lock_for_healthy_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            entry = self.github_skill_lock_entry("owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298")
            xdg = home / "state"
            lock = xdg / "skills" / ".skill-lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_text(json.dumps({"version": 3, "skills": {"alpha": entry}}), encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry("owner/old", entry["skillFolderHash"])},
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": str(xdg), "GH_HOST": "github.com"}),
                contextlib.redirect_stdout(output),
            ):
                DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
            self.assertNotIn("npx", output.getvalue())
            self.assertIn("already installed", output.getvalue())

    def test_sync_recognizes_equivalent_github_repository_sources(self) -> None:
        sources = (
            "https://github.com/owner/skills",
            "https://github.com/owner/skills.git",
            "https://github.com/owner/skills/",
            "github:owner/skills",
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298",
                )},
            }), encoding="utf-8")
            for source in sources:
                with self.subTest(source=source):
                    manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                    manifest["skills"] = [{"source": source, "checkSkills": ["alpha"]}]
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
                    self.assertNotIn("npx", output.getvalue())
                    self.assertIn("already installed", output.getvalue())

    def test_sync_does_not_attribute_another_agents_copy_from_global_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            universal = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            codex = home / ".codex" / "skills" / "alpha" / "SKILL.md"
            for target, content in ((universal, "# Old alpha\n"), (codex, "# New alpha\n")):
                target.parent.mkdir(parents=True)
                target.write_text(content, encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/new", "c7c75786a2a668d0a7dcd9964d3e0ce15ba5f7a2",
                )},
                "lastSelectedAgents": ["codex"],
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/new", "agent": "universal", "checkSkills": ["alpha"]}]
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                contextlib.redirect_stdout(output),
            ):
                DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
            self.assertIn("Reconcile skills from owner/new", output.getvalue())
            self.assertIn("--agent universal", output.getvalue())
            self.assertEqual(codex.read_text(encoding="utf-8"), "# New alpha\n")
            self.assertEqual(universal.read_text(encoding="utf-8"), "# Old alpha\n")

    def test_sync_does_not_merge_distinct_hosts_refs_or_subpaths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            cases = [
                ("owner/skills", {
                    "source": "owner/skills", "sourceType": "gitlab",
                    "sourceUrl": "https://gitlab.com/owner/skills.git",
                }),
                ("owner/skills", {"ref": "old-branch"}),
                ("owner/skills/other-subpath", {}),
                ("https://github.com/owner/skills/tree/main/other-subpath", {}),
                ("https://example.com/owner/skills.git", {}),
                ("https://github.com/owner/other", {}),
            ]
            for source, fields in cases:
                with self.subTest(source=source, fields=fields):
                    entry = self.github_skill_lock_entry("owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298")
                    entry.update(fields)
                    (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                        "version": 3, "skills": {"alpha": entry},
                    }), encoding="utf-8")
                    manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                    manifest["skills"] = [{"source": source, "checkSkills": ["alpha"]}]
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
                    self.assertIn("Reconcile skills from", output.getvalue())

    def test_sync_checks_supporting_files_before_skipping_owned_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / ".agents" / "skills" / "alpha"
            (folder / "references").mkdir(parents=True)
            (folder / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            guide = folder / "references" / "guide.md"
            guide.write_text("old\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "920656539539d9ffa86223950d6b3883e72b0e32",
                )},
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            for content, refresh in (("old\n", True), ("new\n", False)):
                with self.subTest(content=content):
                    guide.write_text(content, encoding="utf-8")
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
                    self.assertEqual("Reconcile skills from" in output.getvalue(), refresh)

    def test_sync_refreshes_when_recorded_content_hash_cannot_prove_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            for folder_hash in ("", "not-a-tree-hash"):
                with self.subTest(folder_hash=folder_hash):
                    (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                        "version": 3,
                        "skills": {"alpha": self.github_skill_lock_entry("owner/skills", folder_hash)},
                    }), encoding="utf-8")
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
                    self.assertIn("Reconcile skills from owner/skills", output.getvalue())

    def test_sync_leaves_healthy_skills_untouched_by_default(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["skills"] = [{
            "source": "owner/skills",
            "agent": "universal",
            "skills": ["alpha", "beta"],
            "checkSkills": ["alpha", "beta"],
        }]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in ("alpha", "beta"):
                target = home / ".agents" / "skills" / name / "SKILL.md"
                target.parent.mkdir(parents=True)
                target.write_text(f"# {name}\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {
                    "alpha": self.github_skill_lock_entry("owner/skills", "51937797c336f38c66ecfb342d9cc37ac2c56a74"),
                    "beta": self.github_skill_lock_entry("owner/skills", "d4228d69961f61bf69edc471a90918494661c475", "beta"),
                },
            }), encoding="utf-8")
            plan = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(DOTAI.subprocess, "run", side_effect=AssertionError("healthy skills must not fetch")),
                contextlib.redirect_stdout(plan),
            ):
                DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux"))
            self.assertNotIn("Reconcile skills from", plan.getvalue())
            self.assertNotIn("npx", plan.getvalue())

    def test_sync_refreshes_existing_skill_owned_by_another_source(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["skills"] = [{
            "source": "owner/new", "agent": "universal", "skills": ["alpha"], "checkSkills": ["alpha"],
        }]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Old alpha\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/old", "5536fe3d742b7fa77283d2551b2af2166451a702",
                )},
            }), encoding="utf-8")
            plan = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}), contextlib.redirect_stdout(plan):
                DOTAI.reconcile_skills(manifest, DOTAI.Runner("ubuntu", dry_run=True))
            self.assertIn("Reconcile skills from owner/new", plan.getvalue())

    def test_install_force_refreshes_an_existing_skill_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3, "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298",
                )},
            }), encoding="utf-8")
            manifest_path = home / "stack.json"
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{
                "source": "owner/skills", "agent": "universal", "skills": ["alpha"], "checkSkills": ["alpha"],
            }]
            plan = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}), contextlib.redirect_stdout(plan):
                self.assertEqual(DOTAI.reconcile(
                    manifest, manifest_path, DOTAI.Runner("ubuntu", dry_run=True), "install", force=True,
                ), 0)
            self.assertIn("Reconcile skills from owner/skills", plan.getvalue())

    def test_sync_installs_skills_when_any_required_skill_is_missing(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["skills"] = [{
            "source": "owner/skills",
            "agent": "universal",
            "skills": ["alpha", "beta"],
            "checkSkills": ["alpha", "beta"],
        }]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# alpha\n", encoding="utf-8")
            plan = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(plan):
                DOTAI.reconcile_skills(manifest, DOTAI.Runner("linux", dry_run=True))
            self.assertIn("Reconcile skills from owner/skills", plan.getvalue())
            self.assertIn("--skill beta", plan.getvalue())

    def test_sync_update_skills_explicitly_refreshes_healthy_installations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# alpha\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3, "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "51937797c336f38c66ecfb342d9cc37ac2c56a74",
                )},
            }), encoding="utf-8")
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{
                "source": "owner/skills",
                "agent": "universal",
                "skills": ["alpha"],
                "checkSkills": ["alpha"],
            }]
            path = home / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(DOTAI, "print_release_notice"),
                contextlib.redirect_stdout(plan),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        result = DOTAI.main(["--manifest", str(path), "sync", "--update-skills", "--dry-run"])
                    except SystemExit as exc:
                        result = exc.code
                self.assertEqual(result, 0)
            self.assertIn("Reconcile skills from owner/skills", plan.getvalue())


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

    def test_add_mcp_invalid_port_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["localOnly"] = {"owner": "user"}
            manifest["mcp"]["servers"]["custom"] = {
                "command": "npx", "env": {"TOKEN": "LOCAL_API_TOKEN"},
                "providerSetting": {"keep": True},
            }
            original = (json.dumps(manifest, indent=4) + "\n\n").encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    DOTAI.main([
                        "--manifest", str(path), "add", "mcp", "bad", "--url",
                        "https://example.test:not-a-port/mcp",
                    ]),
                    2,
                )
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(
                    DOTAI.main([
                        "--manifest", str(path), "add", "mcp", "bad", "--url",
                        "https://example.test:443/mcp", "--header", "Authorization=API_TOKEN",
                    ]),
                    0,
                )
            updated = DOTAI.load_manifest(path)
            self.assertEqual(updated["mcp"]["servers"]["bad"], {
                "type": "http", "url": "https://example.test:443/mcp",
                "headers": {"Authorization": "API_TOKEN"},
            })
            self.assertEqual(updated["localOnly"], {"owner": "user"})
            self.assertEqual(updated["mcp"]["servers"]["custom"], manifest["mcp"]["servers"]["custom"])

    def test_add_plugin_invalid_id_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            original = json.dumps(self.minimal_manifest("mcp.json"), indent=4).encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "add", "plugin", "bad"]), 2)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(DOTAI.main(["--manifest", str(path), "add", "plugin", "review@team"]), 0)
            self.assertEqual(DOTAI.load_manifest(path)["plugins"], [{"id": "review@team", "scope": "user"}])

    def test_add_tool_empty_check_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["packages"] = [{"name": "sample", "check": "sample --version", "install": []}]
            original = json.dumps(manifest, indent=4).encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(DOTAI.main([
                    "--manifest", str(path), "add", "tool", "sample", "--check", "",
                    "--install", "default=sample install",
                ]), 2)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(DOTAI.main([
                    "--manifest", str(path), "add", "tool", "sample", "--check", "sample --version",
                    "--install", "default=sample install",
                ]), 0)
            self.assertEqual(DOTAI.load_manifest(path)["packages"], [{
                "name": "sample", "check": "sample --version", "install": {"default": ["sample install"]},
            }])

    def test_add_empty_integration_values_do_not_write_manifest(self) -> None:
        commands = [
            ["add", "skill", ""],
            ["add", "marketplace", "team", ""],
            ["add", "mcp", "local", "--command", ""],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            original = json.dumps(self.minimal_manifest("mcp.json"), indent=4).encode("utf-8")
            for command in commands:
                with self.subTest(command=command):
                    path.write_bytes(original)
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(DOTAI.main(["--manifest", str(path), *command]), 2)
                    self.assertEqual(path.read_bytes(), original)

    def test_add_preserves_unconfigured_routing_for_subsequent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "add", "plugin", "review@team"]), 0)
                self.assertEqual(DOTAI.main(["--manifest", str(path), "add", "skill", "owner/skills"]), 0)
                self.assertEqual(DOTAI.main(["--manifest", str(path), "validate"]), 0)
            self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["ompRouting"])
            self.assertEqual(DOTAI.load_manifest(path)["skills"][0]["source"], "owner/skills")

    def test_write_manifest_rejects_invalid_candidate_before_creating_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            original = json.dumps(manifest, indent=4).encode("utf-8")
            path.write_bytes(original)
            manifest["plugins"] = [{"id": "bad"}]
            with self.assertRaises(DOTAI.DotAiError):
                DOTAI.write_manifest(path, manifest)
            self.assertEqual(path.read_bytes(), original)
            missing = root / "new" / "stack.json"
            with self.assertRaises(DOTAI.DotAiError):
                DOTAI.write_manifest(missing, manifest)
            self.assertFalse(missing.parent.exists())
            self.assertEqual(set(root.iterdir()), {path})

    def test_init_rejects_invalid_template_before_creating_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "stack.example.json"
            invalid = self.minimal_manifest("mcp.json")
            invalid["plugins"] = [{"id": "bad"}]
            example.write_text(json.dumps(invalid), encoding="utf-8")
            target = root / "new" / "stack.json"
            with (
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(DOTAI.main(["--manifest", str(target), "init"]), 2)
                self.assertFalse(target.parent.exists())
                example.write_text(json.dumps(self.minimal_manifest("mcp.json")), encoding="utf-8")
                self.assertEqual(DOTAI.main(["--manifest", str(target), "init"]), 0)
            self.assertEqual(DOTAI.load_manifest(target)["plugins"], [])

    def test_added_named_skill_reports_installed_only_after_skill_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "foo"]), 0)
                before = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(before, 1)
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][0]["checkSkills"], ["foo"])

            skill_file = home / ".agents" / "skills" / "foo" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# foo\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                result = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

    def test_added_uppercase_skill_is_healthy_in_installer_normalized_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "ALPHA"]), 0)

            skill_file = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# ALPHA\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                result = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())
            self.assertNotIn("[MISSING] owner/skills:", output.getvalue())
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["skills"][0]["skills"], ["ALPHA"])

    def test_added_skill_checks_follow_installer_punctuation_and_name_boundaries(self) -> None:
        cases = [
            (".. My__ Skill / V2!..", "my__-skill-v2"),
            ("...---...", "unnamed-skill"),
            ("A" * 256, "a" * 255),
        ]
        for selection, installed_name in cases:
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                manifest_path = home / "stack.json"
                mcp_path = home / "mcp.json"
                mcp_path.write_text("{}", encoding="utf-8")
                manifest = self.minimal_manifest(str(mcp_path))
                manifest["mcp"]["servers"] = {}
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", selection]), 0)

                skill_file = home / ".agents" / "skills" / installed_name / "SKILL.md"
                skill_file.parent.mkdir(parents=True)
                skill_file.write_text("# installed skill\n", encoding="utf-8")
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                    result = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
                self.assertEqual(result, 0, output.getvalue())
                self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())
                saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["skills"][0]["skills"], [selection])

    def test_explicit_skill_check_keeps_exact_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized_file = home / ".agents" / "skills" / "actual-skill" / "SKILL.md"
            normalized_file.parent.mkdir(parents=True)
            normalized_file.write_text("# normalized, not the explicit check\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "ALPHA", "--check-skill", "Actual Skill!"]), 0)
                missing = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(missing, 1, output.getvalue())
            self.assertIn("[MISSING] owner/skills:", output.getvalue())

            exact_file = home / ".agents" / "skills" / "Actual Skill!" / "SKILL.md"
            exact_file.parent.mkdir(parents=True)
            exact_file.write_text("# explicitly checked skill\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                healthy = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(healthy, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

    def test_added_wildcard_skill_reports_unverified_instead_of_missing_or_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills"]), 0)
                result = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 1)
            self.assertIn("[UNVERIFIED] owner/skills:", output.getvalue())
            self.assertNotIn("[MISSING] owner/skills:", output.getvalue())
            self.assertNotIn("[OK] owner/skills:", output.getvalue())

    def test_explicit_skill_check_overrides_named_install_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            skill_file = home / ".agents" / "skills" / "actual" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# actual\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(DOTAI.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "alias", "--check-skill", "actual"]), 0)
                result = DOTAI.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

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

    def test_sync_reports_invalid_mcp_config_and_exits_unsuccessfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "mcp.json"
            target.write_text("{not valid JSON", encoding="utf-8")
            path = root / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest(str(target))), encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(root / "state")}),
                mock.patch.object(DOTAI, "latest_release_version", return_value=None),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                result = DOTAI.main(["--manifest", str(path), "sync"])
            self.assertEqual(result, 1)
            self.assertIn("MCP", output.getvalue())
            self.assertIn("invalid json", output.getvalue().lower())
            self.assertIn(str(target), output.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8"), "{not valid JSON")

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

    def test_accepted_recommendation_refreshes_a_healthy_skill_source(self) -> None:
        before = {"source": "owner/recommended", "agent": "universal", "skills": ["*"], "checkSkills": ["keep"]}
        after = {**before, "skills": ["keep"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            example_path = home / "stack.example.json"
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["skills"] = [before]
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            example = {**manifest, "skills": [after]}
            example_path.write_text(json.dumps(example), encoding="utf-8")
            skill_file = home / ".agents" / "skills" / "keep" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# keep\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"keep": self.github_skill_lock_entry(
                    "owner/recommended", "999550e4425ecd9ea5aeb58fef9f1a05ddf50d86", "keep",
                )},
            }), encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "DOTAI_STATE_DIR": str(home / "state"), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(DOTAI, "latest_release_version", return_value=None),
                contextlib.redirect_stdout(output),
            ):
                result = DOTAI.main(["--manifest", str(manifest_path), "sync", "--recommended-skills", "--enforce", "--dry-run"])
            self.assertEqual(result, 0)
            self.assertIn("Reconcile skills from owner/recommended", output.getvalue())

    def test_recommended_skill_sync_accepts_all_without_overwriting_custom_skills(self) -> None:
        retired = {
            "source": "owner/retired",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        added = {
            "source": "owner/added",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom-skill"],
            "checkSkills": ["custom-skill"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired, custom]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [added]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "state.json").write_text(
                json.dumps({"manifest": str(path.resolve()), "managedRecommendedSkills": [retired]}),
                encoding="utf-8",
            )
            installed = json.dumps(
                [
                    {
                        "name": "old-skill",
                        "path": str(root / ".agents" / "skills" / "old-skill"),
                        "scope": "global",
                        "agents": ["Universal"],
                        "source": "owner/retired",
                        "sourceUrl": "https://github.com/owner/retired.git",
                        "sourceType": "github",
                    }
                ]
            )
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(DOTAI, "latest_release_version", return_value=None),
                mock.patch.object(DOTAI, "reconcile_omp_extensions"),
                mock.patch.object(DOTAI, "reconcile_plugins"),
                mock.patch.object(DOTAI, "sync_mcp", return_value=False),
                mock.patch.object(DOTAI.Runner, "output", side_effect=[installed, "[]"]),
                mock.patch.object(DOTAI.Runner, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(output),
            ):
                try:
                    result = DOTAI.main(["--manifest", str(path), "sync", "--recommended-skills"])
                except SystemExit as exc:
                    result = exc.code
                self.assertEqual(result, 0)

            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"], [custom, added])
            self.assertEqual(len(list(root.glob("stack.json.bak.*"))), 1)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(
                [
                    "npx",
                    "--yes",
                    "skills@latest",
                    "remove",
                    "old-skill",
                    "--global",
                    "--yes",
                ],
                commands,
            )
            self.assertIn(DOTAI.skill_command(added), commands)
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(history[os.path.normcase(str(path.resolve()))], {"version": 1, "skills": [added]})
            self.assertIn("owner/retired", output.getvalue())
            self.assertIn("owner/added", output.getvalue())

    def test_enforced_recommended_skill_sync_adopts_matching_sources_and_removes_retired_skills(self) -> None:
        before = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["*"],
            "checkSkills": ["keep"],
        }
        after = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["keep"],
            "checkSkills": ["keep"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom"],
            "checkSkills": ["custom"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [before, custom]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [after]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": []}}),
                encoding="utf-8",
            )
            installed = json.dumps(
                [
                    {
                        "name": name,
                        "path": str(root / ".agents" / "skills" / name),
                        "scope": "global",
                        "agents": ["Universal"],
                        "source": before["source"],
                        "sourceUrl": "https://github.com/owner/recommended.git",
                        "sourceType": "github",
                    }
                    for name in ("keep", "retired")
                ]
            )
            remaining = json.dumps(
                [
                    {
                        "name": "keep",
                        "path": str(root / ".agents" / "skills" / "keep"),
                        "scope": "global",
                        "agents": ["Universal"],
                        "source": before["source"],
                        "sourceUrl": "https://github.com/owner/recommended.git",
                        "sourceType": "github",
                    }
                ]
            )
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(DOTAI, "latest_release_version", return_value=None),
                mock.patch.object(DOTAI, "reconcile_omp_extensions"),
                mock.patch.object(DOTAI, "reconcile_plugins"),
                mock.patch.object(DOTAI, "sync_mcp", return_value=False),
                mock.patch.object(DOTAI.Runner, "output", side_effect=[installed, remaining]),
                mock.patch.object(
                    DOTAI.Runner,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run,
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                try:
                    result = DOTAI.main(
                        ["--manifest", str(path), "sync", "--recommended-skills", "--enforce"]
                    )
                except SystemExit as exc:
                    result = exc.code

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["skills"], [after, custom])
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(
                history[os.path.normcase(str(path.resolve()))],
                {"version": 1, "skills": [after]},
            )
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(
                [
                    "npx",
                    "--yes",
                    "skills@latest",
                    "remove",
                    "retired",
                    "--global",
                    "--yes",
                ],
                commands,
            )
            self.assertIn(DOTAI.skill_command(after), commands)

    def test_recommended_skill_dry_run_does_not_query_machine_for_wildcard_removal(self) -> None:
        retired = {
            "source": "owner/retired",
            "agent": "universal",
            "skills": ["*"],
            "checkSkills": ["old-skill"],
        }
        runner = DOTAI.Runner("ubuntu", dry_run=True)
        changes = [{"kind": "remove", "source": retired["source"], "before": retired, "after": None}]
        output = io.StringIO()
        with mock.patch.object(runner, "output", return_value="[]") as machine_query, contextlib.redirect_stdout(output):
            DOTAI.remove_retired_skills(changes, runner)

        machine_query.assert_not_called()
        self.assertIn("resolved when applied", output.getvalue())

    def test_recommended_skill_dry_run_does_not_verify_named_removal(self) -> None:
        retired = {
            "source": "owner/retired",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        runner = DOTAI.Runner("ubuntu", dry_run=True)
        changes = [{"kind": "remove", "source": retired["source"], "before": retired, "after": None}]
        output = io.StringIO()
        with mock.patch.object(runner, "output", return_value="[]") as machine_query, contextlib.redirect_stdout(output):
            DOTAI.remove_retired_skills(changes, runner)

        machine_query.assert_not_called()
        self.assertEqual(runner.failures, [])
        self.assertIn("remove old-skill", output.getvalue())


    def test_runner_output_ignores_stderr(self) -> None:
        runner = DOTAI.Runner("ubuntu")
        result = subprocess.CompletedProcess(["command"], 0, '{"valid": true}')
        with mock.patch.object(DOTAI.subprocess, "run", return_value=result) as run:
            self.assertEqual(runner.output(["command"]), '{"valid": true}')

        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_installed_skill_listing_uses_universal_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            listing = json.dumps(
                [
                    {
                        "name": "old-skill",
                        "path": str(home / ".agents" / "skills" / "old-skill"),
                        "scope": "global",
                        "agents": ["Pi"],
                        "source": "owner/retired",
                    }
                ]
            )
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}),
                mock.patch.object(runner, "output", return_value=listing),
            ):
                self.assertEqual(
                    DOTAI.installed_skill_names("universal", runner, "owner/retired"),
                    {"old-skill"},
                )


    def test_installed_skill_listing_excludes_other_agents(self) -> None:
        listing = json.dumps(
            [
                {
                    "name": "old-skill",
                    "path": "/tmp/old-skill",
                    "scope": "global",
                    "agents": ["Pi"],
                    "source": "owner/retired",
                    "sourceUrl": "https://github.com/owner/retired.git",
                    "sourceType": "github",
                }
            ]
        )
        runner = DOTAI.Runner("ubuntu")
        with mock.patch.object(runner, "output", return_value=listing):
            self.assertEqual(DOTAI.installed_skill_names("universal", runner, "owner/retired"), set())

    def test_recommended_skill_review_applies_each_choice_and_keeps_rejections_pending(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        added = {"source": "owner/added", "agent": "universal", "skills": ["new-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [added]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "state.json").write_text(
                json.dumps({"manifest": str(path.resolve()), "managedRecommendedSkills": [retired]}),
                encoding="utf-8",
            )
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(runner, "run") as run,
                mock.patch("builtins.input", side_effect=["e", "n", "y"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                updated, managed = DOTAI.review_recommended_skills(manifest, path, runner)
                DOTAI.save_state(path, runner, "sync", managed)
                _, pending, _ = DOTAI.recommended_skill_plan(updated, path)

            self.assertEqual(updated["skills"], [retired, added])
            self.assertEqual(managed, [retired, added])
            self.assertEqual([(change["kind"], change["source"]) for change in pending], [("remove", "owner/retired")])
            run.assert_not_called()

    def test_recommended_skill_update_removes_old_agent_installation(self) -> None:
        before = {"source": "owner/skills", "agent": "pi", "skills": ["review"]}
        after = {"source": "owner/skills", "agent": "universal", "skills": ["*"]}
        runner = DOTAI.Runner("ubuntu")
        installed = json.dumps(
            [
                {
                    "name": "review",
                    "path": "/tmp/review",
                    "scope": "global",
                    "agents": ["Pi"],
                    "source": "owner/skills",
                    "sourceUrl": "https://github.com/owner/skills.git",
                    "sourceType": "github",
                }
            ]
        )
        change = {"kind": "update", "source": before["source"], "before": before, "after": after}
        with mock.patch.object(runner, "output", side_effect=[installed, "[]"]), mock.patch.object(runner, "run") as run:
            DOTAI.remove_retired_skills([change], runner)

        run.assert_called_once_with(
            [
                "npx",
                "--yes",
                "skills@latest",
                "remove",
                "review",
                "--global",
                "--agent",
                "pi",
                "--yes",
            ],
            "Remove retired skills from owner/skills",
        )

    def test_legacy_recommendation_state_updates_current_recommendation_sources(self) -> None:
        before = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        after = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom-skill"],
            "checkSkills": ["custom-skill"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [before, custom]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [after]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): [before, custom]}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
            ):
                managed, changes, conflicts = DOTAI.recommended_skill_plan(manifest, path)

        self.assertEqual(managed, [before])
        self.assertEqual([(change["kind"], change["source"]) for change in changes], [("update", "owner/recommended")])
        self.assertEqual(conflicts, [])



    def test_recommended_skill_history_is_preserved_for_each_manifest(self) -> None:
        first = {"source": "owner/first", "agent": "universal", "skills": ["first"]}
        second = {"source": "owner/second", "agent": "universal", "skills": ["second"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            example_path = root / "stack.example.json"
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            second_manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            first_manifest["skills"] = [first]
            second_manifest["skills"] = [second]
            first_path.write_text(json.dumps(first_manifest), encoding="utf-8")
            second_path.write_text(json.dumps(second_manifest), encoding="utf-8")
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
            ):
                DOTAI.save_state(first_path, runner, "sync", [first])
                DOTAI.save_state(second_path, runner, "sync", [second])
                _, first_changes, _ = DOTAI.recommended_skill_plan(first_manifest, first_path)
                _, second_changes, _ = DOTAI.recommended_skill_plan(second_manifest, second_path)

            self.assertEqual([(change["kind"], change["source"]) for change in first_changes], [("remove", "owner/first")])
            self.assertEqual([(change["kind"], change["source"]) for change in second_changes], [("remove", "owner/second")])

    def test_recommended_skill_sync_writes_manifest_before_uninstalling(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": [retired]}}),
                encoding="utf-8",
            )
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(DOTAI, "write_manifest", side_effect=OSError("locked")),
                mock.patch.object(runner, "output", return_value=json.dumps([{"name": "old-skill", "source": "owner/retired"}])),
                mock.patch.object(runner, "run") as run,
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(OSError):
                    DOTAI.review_recommended_skills(manifest, path, runner)

            run.assert_not_called()

    def test_recommended_skill_sync_fails_when_retired_files_remain(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        installed = json.dumps(
            [
                {
                    "name": "old-skill",
                    "path": "/tmp/old-skill",
                    "scope": "global",
                    "agents": ["Universal"],
                    "source": "owner/retired",
                    "sourceUrl": "https://github.com/owner/retired.git",
                    "sourceType": "github",
                }
            ]
        )
        untracked = json.dumps(
            [
                {
                    "name": "old-skill",
                    "path": "/tmp/old-skill",
                    "scope": "global",
                    "agents": ["Universal"],
                    "source": None,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        )
        runner = DOTAI.Runner("ubuntu")
        change = {"kind": "remove", "source": retired["source"], "before": retired, "after": None}
        with (
            mock.patch.object(runner, "output", side_effect=[installed, untracked]),
            mock.patch.object(runner, "run", return_value=subprocess.CompletedProcess([], 0)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            DOTAI.remove_retired_skills([change], runner)

        self.assertEqual(runner.failures, ["Retired skills still installed for universal: old-skill"])

    def test_recommended_skill_sync_restores_manifest_when_removal_verification_fails(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        installed = json.dumps([{"name": "old-skill", "agents": ["Universal"], "source": "owner/retired"}])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            history_path = state_root / "recommended-skills.json"
            history_path.write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": [retired]}}),
                encoding="utf-8",
            )
            runner = DOTAI.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(runner, "output", side_effect=[installed, installed]),
                mock.patch.object(runner, "run", return_value=subprocess.CompletedProcess([], 0)),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                updated, managed = DOTAI.review_recommended_skills(manifest, path, runner)

            self.assertEqual(updated, manifest)
            self.assertEqual(managed, [retired])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)
            self.assertEqual(
                json.loads(history_path.read_text(encoding="utf-8"))[os.path.normcase(str(path.resolve()))],
                {"version": 1, "skills": [retired]},
            )

    def test_accepted_recommendations_persist_when_unrelated_sync_fails(self) -> None:
        added = {"source": "owner/added", "agent": "universal", "skills": ["new-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [added]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")

            def fail_extension_sync(_manifest: dict, runner: DOTAI.Runner) -> None:
                runner.failures.append("unrelated extension failure")

            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(DOTAI, "EXAMPLE_MANIFEST", example_path),
                mock.patch.object(DOTAI, "latest_release_version", return_value=None),
                mock.patch.object(DOTAI, "reconcile_omp_extensions", side_effect=fail_extension_sync),
                mock.patch.object(DOTAI, "reconcile_skills"),
                mock.patch.object(DOTAI, "reconcile_plugins"),
                mock.patch.object(DOTAI, "sync_mcp", return_value=False),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DOTAI.main(["--manifest", str(path), "sync", "--recommended-skills"]), 1)

            self.assertTrue((state_root / "recommended-skills.json").is_file())
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(history[os.path.normcase(str(path.resolve()))], {"version": 1, "skills": [added]})

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

    def test_package_check_uses_declared_minimum_for_any_package(self) -> None:
        package = {"name": "Other tool", "check": ["other", "--version"], "minimumVersion": "0.43"}
        runner = DOTAI.Runner("ubuntu")
        for version, expected in (("other 0.42.99", False), ("other 0.43.0", True), ("other 0.50.0", True)):
            with self.subTest(version=version):
                result = subprocess.CompletedProcess(["other", "--version"], 0, version)
                with mock.patch.object(DOTAI.subprocess, "run", return_value=result):
                    self.assertEqual(DOTAI.package_check(package, runner), expected)

    def test_package_version_check_reads_stderr_only_version_output(self) -> None:
        command = [sys.executable, "-c", "import sys; print('other 0.50.0', file=sys.stderr)"]
        package = {"name": "Other tool", "check": command, "minimumVersion": "0.43"}
        self.assertEqual(
            DOTAI.package_version_check(package, DOTAI.Runner("ubuntu"), command),
            (True, "other 0.50.0"),
        )

    def test_package_version_check_finds_version_after_stdout_notice(self) -> None:
        command = [
            sys.executable, "-c",
            "import sys; print('Checking installation'); print('other 0.50.0', file=sys.stderr)",
        ]
        package = {"name": "Other tool", "check": command, "minimumVersion": "0.43"}
        installed, report = DOTAI.package_version_check(package, DOTAI.Runner("ubuntu"), command)
        self.assertTrue(installed, report)
        self.assertIn("other 0.50.0", report)

    def test_rtk_status_checks_minimum_version_on_supported_platforms(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        manifest["packages"] = [next(package for package in manifest["packages"] if package["name"] == "RTK")]
        manifest["skills"] = []
        manifest["ompExtensions"] = []
        for platform in ("windows", "macos", "ubuntu", "wsl", "arch"):
            for version, expected in (("rtk 0.42.9", False), ("rtk 0.43.0", True), ("rtk 0.50.0", True)):
                with self.subTest(platform=platform, version=version):
                    runner = DOTAI.Runner(platform)
                    result = subprocess.CompletedProcess(["rtk", "--version"], 0, version)
                    output = io.StringIO()
                    with (
                        mock.patch.object(DOTAI.subprocess, "run", return_value=result),
                        mock.patch.object(DOTAI, "mcp_status", return_value=(True, "configured")),
                        contextlib.redirect_stdout(output),
                    ):
                        healthy = DOTAI.print_status(manifest, runner)
                    self.assertEqual(healthy, expected)
                    self.assertIn(f"[{'OK' if expected else 'MISSING'}] RTK:", output.getvalue())

    def test_rtk_old_version_updates_while_missing_binary_installs(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        manifest["packages"] = [next(package for package in manifest["packages"] if package["name"] == "RTK")]
        for platform in ("windows", "macos", "ubuntu", "wsl", "arch"):
            for version, returncode, operation in (
                ("rtk 0.42.9", 0, "Check/update RTK"),
                ("", 127, "Install RTK"),
                ("rtk 0.50.0", 0, "Check/update RTK"),
            ):
                with self.subTest(platform=platform, version=version, returncode=returncode):
                    runner = DOTAI.Runner(platform, dry_run=True)
                    result = subprocess.CompletedProcess(["rtk", "--version"], returncode, version)
                    output = io.StringIO()
                    with (
                        mock.patch.object(DOTAI.subprocess, "run", return_value=result),
                        contextlib.redirect_stdout(output),
                    ):
                        DOTAI.reconcile_packages(manifest, runner, "update")
                    self.assertIn(operation, output.getvalue())

    def test_install_upgrades_present_rtk_below_minimum(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        manifest["packages"] = [next(package for package in manifest["packages"] if package["name"] == "RTK")]
        for platform in ("windows", "macos", "ubuntu", "wsl", "arch"):
            with self.subTest(platform=platform):
                runner = DOTAI.Runner(platform, dry_run=True)
                result = subprocess.CompletedProcess(["rtk", "--version"], 0, "rtk 0.42.9")
                output = io.StringIO()
                with mock.patch.object(DOTAI.subprocess, "run", return_value=result), contextlib.redirect_stdout(output):
                    DOTAI.reconcile_packages(manifest, runner, "install")
                self.assertIn("Check/update RTK", output.getvalue())

    def test_update_dependency_minimum_respects_presence_and_opt_in(self) -> None:
        for reported_version, returncode, include_dependencies, expected_operation in (
            ("dependency 0.42.9", 0, False, None),
            ("dependency version unknown", 0, False, None),
            ("dependency 0.42.9", 0, True, "update"),
            ("dependency version unknown", 0, True, "update"),
            ("", 127, False, "install"),
            ("", 127, True, "install"),
        ):
            with self.subTest(
                reported_version=reported_version,
                include_dependencies=include_dependencies,
                expected_operation=expected_operation,
            ), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "operation"
                check = [
                    sys.executable, "-c",
                    "import pathlib, sys; "
                    f"changed = pathlib.Path({str(marker)!r}).exists(); "
                    f"print('dependency 0.50.0' if changed else {reported_version!r}); "
                    f"sys.exit(0 if changed else {returncode})",
                ]
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                manifest["packages"] = [{
                    "name": "Dependency",
                    "updateGroup": "dependency",
                    "minimumVersion": "0.43",
                    "check": check,
                    "install": {"default": [[
                        sys.executable, "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('install')",
                    ]]},
                    "update": {"default": [[
                        sys.executable, "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('update')",
                    ]]},
                }]
                runner = DOTAI.Runner("ubuntu")
                with contextlib.redirect_stdout(io.StringIO()):
                    DOTAI.reconcile_packages(
                        manifest, runner, "update", include_dependencies=include_dependencies,
                    )
                if expected_operation is None:
                    self.assertFalse(marker.exists(), "Dependency update requires explicit opt-in")
                else:
                    self.assertEqual(marker.read_text(), expected_operation)
                    self.assertEqual(runner.failures, [])

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

    def test_linux_update_keeps_omp_updater_and_pinned_rtk_update(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        manifest["packages"] = [
            package for package in manifest["packages"] if package["name"] in {"RTK", "Oh My Pi"}
        ]
        plan = io.StringIO()
        with (
            mock.patch.object(DOTAI, "package_check", return_value=True),
            contextlib.redirect_stdout(plan),
        ):
            DOTAI.reconcile_packages(manifest, DOTAI.Runner("wsl", dry_run=True), "update")
        self.assertIn("omp update", plan.getvalue())
        self.assertIn("Check/update RTK", plan.getvalue())
        self.assertIn("releases/download/v0.50.0/rtk-", plan.getvalue())
        self.assertIn("sha256sum -c -", plan.getvalue())
        self.assertNotIn("install.sh", plan.getvalue())

    def test_update_installs_a_missing_versioned_core_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "operation"
            check = [
                sys.executable, "-c",
                "import pathlib, sys; "
                f"present = pathlib.Path({str(marker)!r}).exists(); "
                "print('tool 0.50.0' if present else ''); "
                "sys.exit(0 if present else 127)",
            ]
            manifest = self.minimal_manifest("mcp.json")
            manifest["packages"] = [{
                "name": "Versioned core tool", "minimumVersion": "0.43", "check": check,
                "install": {"linux": [[
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('install')",
                ]]},
                "update": {"linux": [[
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('update')",
                ]]},
            }]
            runner = DOTAI.Runner("ubuntu")
            with contextlib.redirect_stdout(io.StringIO()):
                DOTAI.reconcile_packages(manifest, runner, "update")
            self.assertEqual(marker.read_text(), "install")
            self.assertEqual(runner.failures, [])

    def test_default_omp_privacy_configuration_and_dependencies(self) -> None:
        manifest = DOTAI.load_manifest(ROOT / "stack.example.json")
        packages = {package["name"]: package for package in manifest["packages"]}
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
        self.assertNotIn("--codex", serialized)
        self.assertIn("/stack.json", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertEqual(DOTAI.detect_platform(), os.environ.get("DOTAI_PLATFORM", DOTAI.detect_platform()))

    def test_version_warns_when_newer_release_exists(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"tag_name": "v0.4.0"}'
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(
            output.getvalue(),
            "0.3.5\n[UPDATE] DotAi 0.4.0 is available (current: 0.3.5); pull the repository to update.\n",
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
                response.read.return_value = b'{"tag_name": "v0.4.0"}'
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
                self.assertIn("[UPDATE] DotAi 0.4.0", output.getvalue())

    def test_release_warning_is_silent_when_current_version_is_latest(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"tag_name": "0.3.5"}'
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(output.getvalue(), "0.3.5\n")

    def test_release_check_failure_does_not_change_version_output(self) -> None:
        output = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), contextlib.redirect_stdout(output):
            self.assertEqual(DOTAI.main(["version"]), 0)
        self.assertEqual(output.getvalue(), "0.3.5\n")

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
        self.assertEqual(result.stdout, "0.3.5\n")



if __name__ == "__main__":
    unittest.main()
