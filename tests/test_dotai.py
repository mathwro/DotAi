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
        self.assertNotIn("OMP routing:", report.getvalue())

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

    def test_print_status_renders_routing_only_when_configured_and_uses_its_health(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        runner = DOTAI.Runner("ubuntu")
        output = io.StringIO()
        with (
            mock.patch.object(DOTAI, "mcp_status", return_value=(True, "managed")),
            mock.patch.object(
                DOTAI,
                "omp_routing_status",
                return_value=("DRIFT", "model role differs: default"),
            ) as routing_status,
            contextlib.redirect_stdout(output),
        ):
            self.assertTrue(DOTAI.print_status(manifest, runner))
        routing_status.assert_not_called()
        self.assertNotIn("OMP routing:", output.getvalue())

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
        ponytail = next(skill for skill in manifest["skills"] if skill["source"] == "DietrichGebert/ponytail")
        self.assertEqual(ponytail["skills"], ["ponytail", "ponytail-review"])
        self.assertEqual(ponytail["checkSkills"], ["ponytail", "ponytail-review"])
        superpowers = next(skill for skill in manifest["skills"] if skill["source"] == "obra/superpowers")
        self.assertEqual(
            superpowers["skills"],
            ["test-driven-development", "verification-before-completion", "receiving-code-review", "writing-skills"],
        )
        self.assertEqual(superpowers["checkSkills"], superpowers["skills"])
        grill_me = next(skill for skill in manifest["skills"] if skill["source"] == "mattpocock/skills")
        self.assertEqual(
            grill_me["skills"],
            ["grill-me", "grill-with-docs", "grilling", "writing-for-agents"],
        )
        self.assertEqual(grill_me["checkSkills"], grill_me["skills"])
        commit_and_document = next(skill for skill in manifest["skills"] if skill["source"] == "mathwro/Skills")
        self.assertEqual(
            commit_and_document["skills"],
            ["choosing-branch-structure", "commit-and-document"],
        )
        self.assertEqual(
            commit_and_document["checkSkills"],
            ["choosing-branch-structure", "commit-and-document"],
        )
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
