from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import cli, manifest as manifests, mcp as mcp_config, omp as omp_config, packages as package_manager
from dotai_app import recommendations, releases, runtime, skills as skill_manager, state, terminal


def minimal_manifest(mcp_target: str = "mcp.json") -> dict:
    return {
        "version": 1,
        "packages": [],
        "skills": [],
        "marketplaces": [],
        "plugins": [],
        "ompExtensions": [],
        "ompRouting": None,
        "mcp": {"target": mcp_target, "servers": {}},
    }


def make_package(name: str, *, check: str = "true", install_cmd: str = "echo install", **extra) -> dict:
    return {
        "name": name,
        "check": [check, "--version"],
        "install": {"default": [install_cmd]},
        **extra,
    }


class BaselineRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "stack.json"
        self.example_path = self.root / "stack.example.json"
        self.state_dir = self.root / "state"
        self.mcp_target = self.root / ".omp" / "agent" / "mcp.json"
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
            "DOTAI_HOME": str(self.root),
            "DOTAI_STATE_DIR": str(self.state_dir),
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "GH_HOST": "github.com",
            "NO_COLOR": "1",
            "DOTAI_PLATFORM": "linux",
        })
        environment.start()
        self.addCleanup(environment.stop)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)
        example_patch = mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.example_path)
        example_patch.start()
        self.addCleanup(example_patch.stop)
        release_patch = mock.patch.object(releases, "latest_release_version", return_value=None)
        release_patch.start()
        self.addCleanup(release_patch.stop)

    def write_manifests(self, local: dict, example: dict) -> None:
        self.path.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
        self.example_path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")

    def test_state_persists_and_validates_managed_stack_baseline_per_manifest(self) -> None:
        other = self.root / "other.json"
        pkg = make_package("GitHub CLI", check="gh")
        baseline = {
            "packages": [pkg],
            "mcpServers": {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}},
            "ompExtensions": ["~/.pi/agent/extensions/rtk.ts"],
        }
        other_baseline = {
            "packages": [],
            "mcpServers": {},
            "ompExtensions": ["~/other.ts"],
        }
        self.assertIsNone(state.managed_stack_baseline(self.path))
        state.save_managed_stack_baseline(self.path, baseline)
        state.save_managed_stack_baseline(other, other_baseline)

        self.assertEqual(state.managed_stack_baseline(self.path), baseline)
        self.assertEqual(state.managed_stack_baseline(other), other_baseline)

        raw = json.loads((self.state_dir / "recommended-baseline.json").read_text(encoding="utf-8"))
        key = os.path.normcase(str(self.path.resolve()))
        self.assertEqual(raw[key]["version"], 1)

        # Corrupt / malformed payloads return None without mutating disk
        target = self.state_dir / "recommended-baseline.json"
        for payload in (
            b"{",
            b"[]",
            b"\xff",
            json.dumps({key: {"version": 1, "packages": ["bad"], "mcpServers": {}, "ompExtensions": []}}).encode(),
            json.dumps({key: {"version": 1, "packages": [], "mcpServers": [], "ompExtensions": []}}).encode(),
            json.dumps({key: {"version": 1, "packages": [], "mcpServers": {}, "ompExtensions": [123]}}).encode(),
            json.dumps({key: {"version": 2, "packages": [], "mcpServers": {}, "ompExtensions": []}}).encode(),
        ):
            with self.subTest(payload=payload):
                target.write_bytes(payload)
                self.assertIsNone(state.managed_stack_baseline(self.path))
                self.assertEqual(target.read_bytes(), payload)

    def test_first_run_conservative_ownership_detects_add_converged_and_drift_vs_enforce(self) -> None:
        curl_pkg = make_package("curl", check="curl")
        gh_pkg = make_package("GitHub CLI", check="gh")
        old_rtk = make_package("RTK", check="rtk", install_cmd="curl https://old.invalid | sh")
        new_rtk = make_package(
            "RTK", check="rtk", minimumVersion="0.43",
            install_cmd="sha256-pinned-v0.50.0",
            update={"linux": ["pinned-update"]},
        )
        old_graphify = make_package(
            "Graphify", check="graphify",
            configure={"default": [["graphify", "install", "--platform", "pi"]]},
        )
        new_graphify = make_package("Graphify", check="graphify")
        custom_pkg = make_package("PersonalTool", check="personal")

        local = minimal_manifest(str(self.mcp_target))
        local["packages"] = [curl_pkg, old_rtk, old_graphify, custom_pkg]
        local["mcp"]["servers"] = {
            "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
            "personal-mcp": {"type": "http", "url": "https://personal.example.com/mcp"},
        }
        local["ompExtensions"] = ["~/custom-ext.ts"]

        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [curl_pkg, gh_pkg, new_rtk, new_graphify]
        example["mcp"]["servers"] = {
            "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
            "microsoft-learn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
        }
        example["ompExtensions"] = ["~/.pi/agent/extensions/rtk.ts"]
        self.write_manifests(local, example)

        baseline, changes, conflicts = recommendations.recommended_stack_plan(local, self.path, enforce=False)
        self.assertEqual(baseline["packages"], [curl_pkg])
        self.assertEqual(baseline["mcpServers"], {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}})
        self.assertEqual(baseline["ompExtensions"], [])
        self.assertEqual(
            [(c["section"], c["kind"], c["id"]) for c in changes],
            [
                ("packages", "add", "GitHub CLI"),
                ("mcpServers", "add", "microsoft-learn"),
                ("ompExtensions", "add", "~/.pi/agent/extensions/rtk.ts"),
            ],
        )
        self.assertEqual(
            conflicts,
            [("packages", "RTK"), ("packages", "Graphify")],
        )

        # With enforce=True, RTK and Graphify become update proposals, while custom items remain untouched
        _, enforced_changes, enforced_conflicts = recommendations.recommended_stack_plan(local, self.path, enforce=True)
        self.assertEqual(enforced_conflicts, [])
        self.assertEqual(
            [(c["section"], c["kind"], c["id"]) for c in enforced_changes],
            [
                ("packages", "add", "GitHub CLI"),
                ("packages", "update", "RTK"),
                ("packages", "update", "Graphify"),
                ("mcpServers", "add", "microsoft-learn"),
                ("ompExtensions", "add", "~/.pi/agent/extensions/rtk.ts"),
            ],
        )

    def test_preview_describes_changes_safely_and_redacts_secrets_without_json_dump(self) -> None:
        secret = secrets.token_urlsafe(24)
        old_pkg = make_package(
            "RTK", check="rtk",
            install_cmd=f"curl --header 'Authorization: Bearer {secret}' https://example.com",
        )
        new_pkg = make_package(
            "RTK", check="rtk", minimumVersion="0.43", updateGroup="core",
            install_cmd="pinned-install", update={"default": ["pinned-update"]},
        )
        old_mcp = {
            "type": "http",
            "url": "https://mcp.example.com/old",
            "headers": {"Authorization": f"Bearer {secret}", "X-Api-Key": secret},
        }
        new_mcp = {
            "type": "http",
            "url": "https://mcp.example.com/new",
            "headers": {"Authorization": "API_TOKEN_ENV", "X-Api-Key": "API_KEY_ENV"},
        }
        stdio_mcp = {
            "command": "npx",
            "args": ["-y", "@scope/mcp"],
            "env": {"SECRET_TOKEN": secret},
        }

        local = minimal_manifest(str(self.mcp_target))
        local["packages"] = [old_pkg]
        local["mcp"]["servers"] = {"example": old_mcp, "custom": {"type": "http", "url": f"https://user:{secret}@custom.invalid/mcp"}}
        local["ompExtensions"] = ["~/old-ext.ts"]

        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [new_pkg, make_package("GitHub CLI", check="gh")]
        example["mcp"]["servers"] = {"example": new_mcp, "local-stdio": stdio_mcp}
        example["ompExtensions"] = ["~/.pi/agent/extensions/rtk.ts"]
        self.write_manifests(local, example)
        state.save_managed_stack_baseline(self.path, {
            "packages": [old_pkg],
            "mcpServers": {"example": old_mcp},
            "ompExtensions": ["~/old-ext.ts"],
        })

        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LEAKED_TEST_SECRET": secret}),
            mock.patch("builtins.input", side_effect=AssertionError("dry-run must not prompt")),
            contextlib.redirect_stdout(output),
        ):
            runner = runtime.Runner("linux", dry_run=True)
            proposed, _, _ = recommendations.review_recommended_stack(local, self.path, runner, enforce=False)

        text = output.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn("API_TOKEN_ENV", text)
        self.assertNotIn("API_KEY_ENV", text)
        self.assertNotIn('"packages":', text)
        self.assertNotIn('"servers":', text)
        self.assertIn("Update package RTK", text)
        self.assertIn("install", text)
        self.assertIn("minimumVersion", text)
        self.assertIn("update", text)
        self.assertIn("Add package GitHub CLI", text)
        self.assertIn("Update MCP server example (http)", text)
        self.assertIn("headers", text)
        self.assertIn("url", text)
        self.assertIn("Add MCP server local-stdio (stdio)", text)
        self.assertIn("Remove OMP extension ~/old-ext.ts", text)
        self.assertIn("Add OMP extension ~/.pi/agent/extensions/rtk.ts", text)
        # Dry run did not mutate manifest, backups, or state
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), local)
        self.assertEqual(list(self.root.glob("stack.json.bak.*")), [])
        self.assertEqual(proposed["mcp"]["servers"]["custom"], local["mcp"]["servers"]["custom"])

    def test_install_recommended_applies_all_creates_private_backup_preserves_custom_and_runs_install(self) -> None:
        gh_pkg = make_package("GitHub CLI", check="gh")
        custom_pkg = make_package("MyTool", check="mytool")
        skill_added = {
            "source": "owner/added",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        custom_skill = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom-skill"],
            "checkSkills": ["custom-skill"],
        }
        local = minimal_manifest(str(self.mcp_target))
        local["packages"] = [custom_pkg]
        local["skills"] = [custom_skill]
        local["mcp"]["servers"] = {"custom-mcp": {"type": "http", "url": "https://custom.example.com/mcp"}}
        local["ompExtensions"] = ["~/custom.ts"]

        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [gh_pkg]
        example["skills"] = [skill_added]
        example["mcp"]["servers"] = {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}
        example["ompExtensions"] = ["~/.pi/agent/extensions/rtk.ts"]
        self.write_manifests(local, example)

        reconciled_manifests: list[dict] = []

        def fake_packages(manifest, runner, mode, force, include_deps):
            reconciled_manifests.append((mode, list(manifest["packages"])))

        output = io.StringIO()
        with (
            mock.patch.object(package_manager, "reconcile_packages", side_effect=fake_packages),
            mock.patch.object(omp_config, "reconcile_omp_extensions"),
            mock.patch.object(skill_manager, "reconcile_skills"),
            mock.patch.object(omp_config, "reconcile_plugins"),
            mock.patch.object(mcp_config, "sync_mcp", return_value=True),
            mock.patch("builtins.input", return_value="a"),
            contextlib.redirect_stdout(output),
        ):
            rc = cli.main(["--manifest", str(self.path), "install", "--recommended"])

        self.assertEqual(rc, 0)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["packages"], [custom_pkg, gh_pkg])
        self.assertEqual(saved["skills"], [custom_skill, skill_added])
        self.assertEqual(
            saved["mcp"]["servers"],
            {
                "custom-mcp": {"type": "http", "url": "https://custom.example.com/mcp"},
                "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
            },
        )
        self.assertEqual(saved["ompExtensions"], ["~/custom.ts", "~/.pi/agent/extensions/rtk.ts"])

        backups = list(self.root.glob("stack.json.bak.*"))
        self.assertEqual(len(backups), 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

        self.assertEqual(reconciled_manifests, [("install", [custom_pkg, gh_pkg])])
        self.assertEqual(state.managed_recommendations(self.path), [skill_added])
        self.assertEqual(
            state.managed_stack_baseline(self.path),
            {
                "packages": [gh_pkg],
                "mcpServers": {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}},
                "ompExtensions": ["~/.pi/agent/extensions/rtk.ts"],
            },
        )

    def test_update_recommended_enforce_updates_modified_packages_without_deleting_custom_items(self) -> None:
        old_rtk = make_package("RTK", check="rtk", install_cmd="unpinned")
        new_rtk = make_package("RTK", check="rtk", minimumVersion="0.43", install_cmd="pinned-v0.50.0")
        old_graphify = make_package(
            "Graphify", check="graphify",
            configure={"default": [["graphify", "install", "--platform", "pi"]]},
        )
        new_graphify = make_package("Graphify", check="graphify")
        custom_pkg = make_package("CustomCli", check="custom")
        custom_mcp = {"type": "http", "url": "https://my.mcp.invalid/mcp"}

        local = minimal_manifest(str(self.mcp_target))
        local["packages"] = [old_rtk, old_graphify, custom_pkg]
        local["mcp"]["servers"] = {"my-mcp": custom_mcp}

        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [new_rtk, new_graphify]
        self.write_manifests(local, example)

        output = io.StringIO()
        with (
            mock.patch.object(package_manager, "reconcile_packages") as pkg_mock,
            mock.patch.object(omp_config, "reconcile_omp_extensions"),
            mock.patch.object(skill_manager, "reconcile_skills"),
            mock.patch.object(omp_config, "reconcile_plugins"),
            mock.patch.object(mcp_config, "sync_mcp", return_value=True),
            mock.patch("builtins.input", return_value="a"),
            contextlib.redirect_stdout(output),
        ):
            rc = cli.main(["--manifest", str(self.path), "update", "--recommended", "--enforce"])

        self.assertEqual(rc, 0)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["packages"], [new_rtk, new_graphify, custom_pkg])
        self.assertEqual(saved["mcp"]["servers"], {"my-mcp": custom_mcp})
        pkg_mock.assert_called_once()
        self.assertEqual(pkg_mock.call_args[0][0]["packages"], [new_rtk, new_graphify, custom_pkg])
        self.assertEqual(pkg_mock.call_args[0][2], "update")

    def test_review_each_and_none_choices_and_sync_package_hint(self) -> None:
        gh_pkg = make_package("GitHub CLI", check="gh")
        mcp_srv = {"type": "http", "url": "https://mcp.context7.com/mcp"}
        local = minimal_manifest(str(self.mcp_target))
        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [gh_pkg]
        example["mcp"]["servers"] = {"context7": mcp_srv}
        example["ompExtensions"] = ["~/.pi/agent/extensions/rtk.ts"]
        self.write_manifests(local, example)

        # First test 'none' -> no manifest write, no backup
        out_none = io.StringIO()
        with (
            mock.patch.object(omp_config, "reconcile_omp_extensions"),
            mock.patch.object(skill_manager, "reconcile_skills"),
            mock.patch.object(omp_config, "reconcile_plugins"),
            mock.patch.object(mcp_config, "sync_mcp", return_value=True),
            mock.patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(out_none),
        ):
            self.assertEqual(cli.main(["--manifest", str(self.path), "sync", "--recommended"]), 0)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), local)
        self.assertEqual(list(self.root.glob("stack.json.bak.*")), [])

        # Next test 'each' -> accept package (y), reject mcp (n), accept extension (y)
        # And verify sync never runs package_manager.reconcile_packages, and prints a hint for packages!
        out_each = io.StringIO()
        answers = iter(["e", "y", "n", "y"])
        with (
            mock.patch.object(package_manager, "reconcile_packages", side_effect=AssertionError("sync must not run packages")),
            mock.patch.object(omp_config, "reconcile_omp_extensions"),
            mock.patch.object(skill_manager, "reconcile_skills"),
            mock.patch.object(omp_config, "reconcile_plugins"),
            mock.patch.object(mcp_config, "sync_mcp", return_value=True),
            mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)),
            contextlib.redirect_stdout(out_each),
        ):
            self.assertEqual(cli.main(["--manifest", str(self.path), "sync", "--recommended"]), 0)

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["packages"], [gh_pkg])
        self.assertEqual(saved["mcp"]["servers"], {})
        self.assertEqual(saved["ompExtensions"], ["~/.pi/agent/extensions/rtk.ts"])
        self.assertIn("install", out_each.getvalue())
        self.assertEqual(
            state.managed_stack_baseline(self.path),
            {
                "packages": [gh_pkg],
                "mcpServers": {},
                "ompExtensions": ["~/.pi/agent/extensions/rtk.ts"],
            },
        )

    def test_cli_flag_validation_rejects_invalid_combinations_before_mutation(self) -> None:
        local = minimal_manifest(str(self.mcp_target))
        self.write_manifests(local, local)
        original = self.path.read_bytes()
        for bad_args in (
            ["install", "--enforce"],
            ["update", "--enforce"],
            ["sync", "--enforce"],
            ["sync", "--recommended", "--recommended-skills"],
        ):
            with self.subTest(bad_args=bad_args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["--manifest", str(self.path), *bad_args])
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(self.path.read_bytes(), original)

    def test_install_and_update_print_concise_baseline_notice_without_prompting_or_mutating(self) -> None:
        gh_pkg = make_package("GitHub CLI", check="gh")
        local = minimal_manifest(str(self.mcp_target))
        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [gh_pkg]
        self.write_manifests(local, example)
        original = self.path.read_text(encoding="utf-8")

        for mode in ("install", "update"):
            with self.subTest(mode=mode):
                output = io.StringIO()
                with (
                    mock.patch.object(package_manager, "reconcile_packages"),
                    mock.patch.object(omp_config, "reconcile_omp_extensions"),
                    mock.patch.object(skill_manager, "reconcile_skills"),
                    mock.patch.object(omp_config, "reconcile_plugins"),
                    mock.patch.object(mcp_config, "sync_mcp", return_value=True),
                    mock.patch("builtins.input", side_effect=AssertionError("must not prompt")),
                    contextlib.redirect_stdout(output),
                ):
                    rc = cli.main(["--manifest", str(self.path), mode, "--dry-run"])
                self.assertEqual(rc, 0)
                self.assertIn("GitHub CLI", output.getvalue())
                self.assertIn(f"{mode} --recommended", output.getvalue())
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_retired_skill_failure_during_stack_review_restores_manifest_and_preserves_state(self) -> None:
        retired_skill = {
            "source": "owner/retired",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        gh_pkg = make_package("GitHub CLI", check="gh")
        local = minimal_manifest(str(self.mcp_target))
        local["skills"] = [retired_skill]
        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [gh_pkg]
        self.write_manifests(local, example)
        state.save_managed_recommendations(self.path, [retired_skill])
        state.save_managed_stack_baseline(self.path, {"packages": [], "mcpServers": {}, "ompExtensions": []})
        manifest_before = self.path.read_bytes()

        with (
            mock.patch.object(runtime.Runner, "output", return_value="not-json"),
            mock.patch("builtins.input", return_value="a"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = cli.main(["--manifest", str(self.path), "install", "--recommended"])

        self.assertEqual(rc, 1)
        self.assertEqual(self.path.read_bytes(), manifest_before)
        self.assertEqual(state.managed_recommendations(self.path), [retired_skill])
        self.assertEqual(
            state.managed_stack_baseline(self.path),
            {"packages": [], "mcpServers": {}, "ompExtensions": []},
        )

    def test_omp_extensions_match_semantic_expanded_or_backslash_paths_without_duplicates(self) -> None:
        expanded_rtk = str((self.root / ".pi" / "agent" / "extensions" / "rtk.ts").resolve())
        local = minimal_manifest(str(self.mcp_target))
        local["ompExtensions"] = [expanded_rtk]
        example = minimal_manifest(str(self.mcp_target))
        example["ompExtensions"] = ["~/.pi/agent/extensions/rtk.ts"]
        self.write_manifests(local, example)

        baseline, changes, conflicts = recommendations.recommended_stack_plan(local, self.path)
        self.assertEqual(changes, [])
        self.assertEqual(conflicts, [])
        self.assertEqual(baseline["ompExtensions"], ["~/.pi/agent/extensions/rtk.ts"])

    def test_initialized_manifest_owns_example_baseline_before_any_successful_run(self) -> None:
        # A first install that fails must still let later example recipe changes be adopted, not DRIFT.
        old_graphify = make_package("Graphify", check="graphify", install_cmd="uv tool install graphifyy")
        new_graphify = make_package("Graphify", check="graphify", install_cmd="uv tool install --python 3.13 graphifyy")
        skill = {"source": "owner/skills", "agent": "pi", "skills": ["one"]}
        example = minimal_manifest(str(self.mcp_target))
        example["packages"] = [old_graphify]
        example["skills"] = [skill]
        self.example_path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
        with mock.patch.object(manifests, "DEFAULT_MANIFEST", self.path), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--manifest", str(self.path), "validate"]), 0)

        example["packages"] = [new_graphify]
        self.example_path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
        _, changes, conflicts = recommendations.recommended_stack_plan(manifests.load_manifest(self.path), self.path)
        self.assertEqual(conflicts, [])
        self.assertEqual([(change["section"], change["id"], change["kind"]) for change in changes], [("packages", "Graphify", "update")])


if __name__ == "__main__":
    unittest.main()
