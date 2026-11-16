from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import (
    cli,
    integrations,
    manifest as manifests,
    releases,
    runtime,
    state,
    terminal,
)


BEGIN_ENV = b"# BEGIN DotAi skill environment\n"
END_ENV = b"# END DotAi skill environment\n"


def _write_python_shim(bin_dir: Path, name: str, script_path: Path) -> None:
    if os.name == "nt":
        shim = bin_dir / f"{name}.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = bin_dir / name
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)


COMMAND_LOGGER = r"""import json, os, pathlib, sys
home = pathlib.Path(os.environ["DOTAI_HOME"])
log_path = home / "commands.jsonl"
tool = sys.argv[1]
args = sys.argv[2:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": tool, "args": args}) + "\n")

if tool == "pkg":
    action = args[0]
    pkg_name = args[1]
    installed_path = home / "packages.json"
    installed = json.loads(installed_path.read_text()) if installed_path.exists() else {}
    if action == "check":
        if pkg_name in installed:
            print(installed[pkg_name])
            sys.exit(0)
        sys.exit(1)
    if action in ("install", "update"):
        installed[pkg_name] = args[2]
        installed_path.write_text(json.dumps(installed))
        sys.exit(0)
    sys.exit(2)

if tool == "npx":
    # args: --yes skills@latest add <source> --global --agent <agent> --skill <s1> ... --copy --yes
    if len(args) >= 4 and args[2] == "add":
        source = args[3]
        agent = args[args.index("--agent") + 1]
        root = home / (".agents/skills" if agent == "universal" else f".{agent}/skills")
        skills = [args[idx + 1] for idx, val in enumerate(args) if val == "--skill"]
        for s in skills:
            if s != "*":
                folder = root / s.lower()
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "SKILL.md").write_text(f"# {s}\n", encoding="utf-8")
        sys.exit(0)
    sys.exit(2)

if tool == "omp":
    if args == ["config", "list", "--json"]:
        cfg_path = home / ".omp" / "global.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        out = {k: {"value": v} for k, v in cfg.items()}
        print(json.dumps(out))
        sys.exit(0)
    if args[:2] == ["config", "get"] and args[-1] == "--json":
        key = args[2]
        cfg_path = home / ".omp" / "global.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        if key in cfg:
            print(json.dumps({"key": key, "value": cfg[key]}))
        else:
            print(json.dumps({"key": key, "value": [] if key == "extensions" else None}))
        sys.exit(0)
    if args[:3] == ["config", "set", "extensions"]:
        cfg_path = home / ".omp" / "global.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg["extensions"] = json.loads(args[3])
        cfg_path.write_text(json.dumps(cfg))
        sys.exit(0)
    if args[:2] == ["plugin", "marketplace"] and len(args) == 4:
        action, val = args[2], args[3]
        mp_path = home / ".omp" / "marketplaces.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        reg = json.loads(mp_path.read_text()) if mp_path.exists() else {"marketplaces": {}}
        if action == "add":
            name = val.rsplit("/", 1)[-1]
            reg["marketplaces"][name] = {"source": val}
            mp_path.write_text(json.dumps(reg))
            sys.exit(0)
        if action == "update":
            if val not in reg["marketplaces"]:
                sys.exit(9)
            reg["marketplaces"][val]["updated"] = True
            mp_path.write_text(json.dumps(reg))
            sys.exit(0)
    if args[:1] == ["plugin"] and len(args) >= 5:
        action = args[1]
        scope = args[args.index("--scope") + 1]
        plugin_id = args[-1]
        _, _, mp_name = plugin_id.partition("@")
        mp_path = home / ".omp" / "marketplaces.json"
        mp_reg = json.loads(mp_path.read_text()) if mp_path.exists() else {"marketplaces": {}}
        if mp_name not in mp_reg.get("marketplaces", {}):
            print(f"Missing marketplace {mp_name}", file=sys.stderr)
            sys.exit(8)
        plug_path = home / ".omp" / "plugins" / "installed_plugins.json"
        plug_path.parent.mkdir(parents=True, exist_ok=True)
        reg = json.loads(plug_path.read_text()) if plug_path.exists() else {"plugins": {}}
        if action == "install":
            reg["plugins"][plugin_id] = {"installed": True}
            plug_path.write_text(json.dumps(reg))
            sys.exit(0)
        if action == "upgrade":
            reg["plugins"].setdefault(plugin_id, {})["upgraded"] = True
            plug_path.write_text(json.dumps(reg))
            sys.exit(0)
    sys.exit(64)
"""


class ScopedExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.project.mkdir(parents=True)
        self.manifest_path = self.project / "stack.json"
        self.example_path = self.project / "stack.example.json"
        self.mcp_target = self.home / ".omp" / "agent" / "mcp.json"
        self.env_target = self.home / ".omp" / "agent" / ".env"

        logger_script = self.bin / "driver.py"
        logger_script.write_text(COMMAND_LOGGER, encoding="utf-8")
        for tool in ("npx", "omp"):
            wrapper = self.bin / f"{tool}_wrap.py"
            wrapper.write_text(
                f'import subprocess, sys\nsys.exit(subprocess.call([sys.executable, {str(logger_script)!r}, {tool!r}, *sys.argv[1:]]))\n',
                encoding="utf-8",
            )
            _write_python_shim(self.bin, tool, wrapper)

        pkg_wrapper = self.bin / "pkg_wrap.py"
        pkg_wrapper.write_text(
            f'import subprocess, sys\nsys.exit(subprocess.call([sys.executable, {str(logger_script)!r}, "pkg", *sys.argv[1:]]))\n',
            encoding="utf-8",
        )
        _write_python_shim(self.bin, "testpkg", pkg_wrapper)

        # Create extension source files
        ext_rtk = self.home / ".pi" / "agent" / "extensions" / "rtk.ts"
        ext_rtk.parent.mkdir(parents=True, exist_ok=True)
        ext_rtk.write_text("// rtk\n", encoding="utf-8")
        ext_custom = self.home / ".pi" / "agent" / "extensions" / "custom.ts"
        ext_custom.write_text("// custom\n", encoding="utf-8")

        # Create empty valid mcp.json by default
        self.mcp_target.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_target.write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")

        path_sep = os.pathsep
        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "DOTAI_HOME": str(self.home),
            "DOTAI_STATE_DIR": str(self.root / "state"),
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "GH_HOST": "github.com",
            "NO_COLOR": "1",
            "DOTAI_PLATFORM": "linux",
            "DOTAI_NO_UPDATE_CHECK": "1",
            "PATH": f"{self.bin}{path_sep}{os.environ.get('PATH', '')}",
        }
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(runtime, "ROOT", self.project),
            mock.patch.object(manifests, "DEFAULT_MANIFEST", self.manifest_path),
            mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.example_path),
            mock.patch.object(releases, "latest_release_version", return_value=None),
            mock.patch.object(terminal, "_COLOR_ENABLED", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        self.manifest_data = {
            "$schema": "./stack.schema.json",
            "version": 1,
            "ompExtensions": [
                "~/.pi/agent/extensions/rtk.ts",
                "~/.pi/agent/extensions/custom.ts",
            ],
            "ompRouting": None,
            "packages": [
                {
                    "name": "RTK",
                    "minimumVersion": "0.43",
                    "check": "testpkg check RTK",
                    "install": {"default": ["testpkg install RTK 0.50.0"]},
                    "update": {"default": ["testpkg update RTK 0.50.0"]},
                },
                {
                    "name": "Graphify",
                    "check": "testpkg check Graphify",
                    "install": {"default": ["testpkg install Graphify 1.2.0"]},
                    "update": {"default": ["testpkg update Graphify 1.2.0"]},
                },
            ],
            "skills": [
                {
                    "source": "https://github.com/mathwro/Skills",
                    "agent": "universal",
                    "skills": ["commit-and-document"],
                    "checkSkills": ["commit-and-document"],
                    "ompEnvironment": {"MATHWRO_MODE": "strict"},
                },
                {
                    "source": "GoogleChrome/modern-web-guidance",
                    "agent": "universal",
                    "skills": ["modern-web-guidance"],
                    "checkSkills": ["modern-web-guidance"],
                    "ompEnvironment": {"DISABLE_TELEMETRY": "1"},
                },
            ],
            "marketplaces": [
                {"name": "team", "source": "owner/team"},
                {"name": "other", "source": "owner/other"},
            ],
            "plugins": [
                {"id": "review@team", "scope": "user"},
                {"id": "lint@other", "scope": "user"},
            ],
            "mcp": {
                "target": "~/.omp/agent/mcp.json",
                "servers": {
                    "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                    "mslearn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
                },
            },
        }
        self.write_manifests(self.manifest_data)

    def write_manifests(self, manifest: dict, example: dict | None = None) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.example_path.write_text(
            json.dumps(example if example is not None else manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--color", "never", "--manifest", str(self.manifest_path), *argv])
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def logged_commands(self) -> list[dict]:
        log_path = self.home / "commands.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def clear_logged_commands(self) -> None:
        log_path = self.home / "commands.jsonl"
        if log_path.exists():
            log_path.unlink()

    # 1. Conflict guards and CLI validation
    def test_conflict_guard_rejects_only_and_target_with_recommendation_flags(self) -> None:
        cases = [
            ["install", "--only", "packages", "--recommended"],
            ["install", "--target", "RTK", "--recommended"],
            ["install", "--only", "packages", "--recommended", "--enforce"],
            ["update", "--only", "skills", "--recommended"],
            ["update", "--target", "RTK", "--recommended", "--enforce"],
            ["sync", "--only", "mcp", "--recommended"],
            ["sync", "--only", "skills", "--recommended-skills"],
            ["sync", "--target", "context7", "--recommended-skills"],
            ["sync", "--target", "context7", "--recommended-skills", "--enforce"],
            ["sync", "--only", "mcp", "--enforce"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn(
                    "--only and --target cannot be combined with --recommended, --recommended-skills, or --enforce",
                    err,
                )
        self.assertEqual(self.logged_commands(), [])

    def test_sync_rejects_only_packages(self) -> None:
        code, _, err = self.run_cli(["sync", "--only", "packages"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        self.assertEqual(self.logged_commands(), [])

    # 2. --only filtering on status, sync, install, update
    def test_status_only_checks_selected_sections_and_returns_scoped_exit_code(self) -> None:
        # Make MCP healthy while packages, skills, plugins, extensions are missing/unhealthy
        self.mcp_target.write_text(
            json.dumps({"mcpServers": self.manifest_data["mcp"]["servers"]}, indent=2) + "\n",
            encoding="utf-8",
        )

        code_mcp, out_mcp, _ = self.run_cli(["status", "--only", "mcp"])
        self.assertEqual(code_mcp, 0)
        self.assertIn("Platform:", out_mcp)
        self.assertIn("MCP:", out_mcp)
        self.assertNotIn("Packages:", out_mcp)
        self.assertNotIn("Skills:", out_mcp)
        self.assertNotIn("Plugins:", out_mcp)
        self.assertNotIn("OMP extensions:", out_mcp)
        self.assertNotIn("OMP routing:", out_mcp)
        self.assertEqual(self.logged_commands(), [])

        code_skills, out_skills, _ = self.run_cli(["status", "--only", "skills"])
        self.assertEqual(code_skills, 1)
        self.assertIn("Skills:", out_skills)
        self.assertNotIn("MCP:", out_skills)
        self.assertNotIn("Packages:", out_skills)

        # --only routing when unconfigured is informational INACTIVE -> exits 0
        code_routing, out_routing, _ = self.run_cli(["status", "--only", "routing"])
        self.assertEqual(code_routing, 0)
        self.assertIn("OMP routing:", out_routing)
        self.assertNotIn("MCP:", out_routing)

    def test_status_explicit_empty_plugins_section_prints_cleanly_and_exits_zero(self) -> None:
        manifest_no_plugins = {
            **self.manifest_data,
            "marketplaces": [],
            "plugins": [],
        }
        self.write_manifests(manifest_no_plugins)
        code, out, _ = self.run_cli(["status", "--only", "plugins"])
        self.assertEqual(code, 0)
        self.assertIn("Platform:", out)
        self.assertIn("Plugins:", out)
        self.assertNotIn("Packages:", out)

    def test_sync_only_mcp_runs_only_mcp_without_skills_plugins_or_extensions(self) -> None:
        code, out, err = self.run_cli(["sync", "--only", "mcp"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(self.logged_commands(), [])
        written_mcp = json.loads(self.mcp_target.read_text(encoding="utf-8"))
        self.assertEqual(set(written_mcp["mcpServers"]), {"context7", "mslearn"})
        self.assertFalse(self.env_target.exists())

    def test_install_and_update_only_packages_skip_other_sections(self) -> None:
        # Add an extra baseline skill in example so full install/update would print a notice
        example_with_extra = json.loads(json.dumps(self.manifest_data))
        example_with_extra["skills"].append(
            {
                "source": "owner/extra-recommended",
                "agent": "universal",
                "skills": ["extra"],
                "checkSkills": ["extra"],
            }
        )
        self.write_manifests(self.manifest_data, example=example_with_extra)

        code, out, err = self.run_cli(["install", "--only", "packages"])
        self.assertEqual(code, 0, msg=err)
        self.assertNotIn("extra-recommended", out)
        tools = {entry["tool"] for entry in self.logged_commands()}
        self.assertEqual(tools, {"pkg"})
        installed_pkgs = json.loads((self.home / "packages.json").read_text(encoding="utf-8"))
        self.assertEqual(set(installed_pkgs), {"RTK", "Graphify"})
        self.assertEqual(json.loads(self.mcp_target.read_text(encoding="utf-8"))["mcpServers"], {})

        self.clear_logged_commands()
        code_upd, out_upd, err_upd = self.run_cli(["update", "--only", "packages"])
        self.assertEqual(code_upd, 0, msg=err_upd)
        self.assertNotIn("extra-recommended", out_upd)
        tools_upd = {entry["tool"] for entry in self.logged_commands()}
        self.assertEqual(tools_upd, {"pkg"})

    # 3. --target matching across all supported identifier types
    def test_target_package_case_insensitive_runs_only_matched_package(self) -> None:
        code, _, err = self.run_cli(["install", "--target", "rtk"])
        self.assertEqual(code, 0, msg=err)
        installed_pkgs = json.loads((self.home / "packages.json").read_text(encoding="utf-8"))
        self.assertEqual(set(installed_pkgs), {"RTK"})
        tools = {entry["tool"] for entry in self.logged_commands()}
        self.assertEqual(tools, {"pkg"})
        self.assertEqual(json.loads(self.mcp_target.read_text(encoding="utf-8"))["mcpServers"], {})
        self.assertFalse(self.env_target.exists())

    def test_target_skill_matches_shorthand_and_skill_name_while_preserving_other_skill_env(self) -> None:
        # Pre-populate .env with unmanaged content + existing managed block from full stack
        prefix = b"UNMANAGED_KEY=keep\n"
        self.env_target.parent.mkdir(parents=True, exist_ok=True)
        self.env_target.write_bytes(
            prefix
            + BEGIN_ENV
            + b"DISABLE_TELEMETRY=1\nMATHWRO_MODE=old\n"
            + END_ENV
        )

        # Target by GitHub repo shorthand mathwro/Skills (manifest has https://github.com/mathwro/Skills)
        code, _, err = self.run_cli(["sync", "--target", "mathwro/Skills"])
        self.assertEqual(code, 0, msg=err)

        # Verify ONLY mathwro/Skills ran, and DISABLE_TELEMETRY=1 was preserved!
        cmds = self.logged_commands()
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["tool"], "npx")
        self.assertIn("https://github.com/mathwro/Skills", cmds[0]["args"])

        env_bytes = self.env_target.read_bytes()
        self.assertEqual(
            env_bytes,
            prefix
            + BEGIN_ENV
            + b"DISABLE_TELEMETRY=1\nMATHWRO_MODE=strict\n"
            + END_ENV,
        )

        # Also verify targeting by explicit skill name "modern-web-guidance"
        self.clear_logged_commands()
        code2, _, err2 = self.run_cli(["sync", "--target", "modern-web-guidance"])
        self.assertEqual(code2, 0, msg=err2)
        cmds2 = self.logged_commands()
        self.assertEqual(len(cmds2), 1)
        self.assertEqual(cmds2[0]["tool"], "npx")
        self.assertIn("GoogleChrome/modern-web-guidance", cmds2[0]["args"])
        # Both env vars remain intact
        self.assertEqual(
            self.env_target.read_bytes(),
            prefix
            + BEGIN_ENV
            + b"DISABLE_TELEMETRY=1\nMATHWRO_MODE=strict\n"
            + END_ENV,
        )

    def test_partial_mcp_sync_reconciles_only_targeted_server_leaving_other_conflict_untouched(self) -> None:
        # Put mslearn into disabledServers so a full sync would fail on mslearn conflict!
        self.mcp_target.write_text(
            json.dumps(
                {
                    "disabledServers": ["mslearn"],
                    "mcpServers": {
                        "mslearn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
                        "unmanaged": {"type": "http", "url": "https://example.com/mcp"},
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # Scoped sync targeting context7 succeeds and preserves mslearn + unmanaged
        code, _, err = self.run_cli(["sync", "--target", "context7"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(self.logged_commands(), [])
        after_scoped = json.loads(self.mcp_target.read_text(encoding="utf-8"))
        self.assertEqual(set(after_scoped["mcpServers"]), {"context7", "mslearn", "unmanaged"})
        self.assertEqual(after_scoped["disabledServers"], ["mslearn"])

        # Full MCP sync still detects the disabled mslearn conflict
        code_full, out_full, _ = self.run_cli(["sync", "--only", "mcp"])
        self.assertEqual(code_full, 1)
        self.assertIn("mslearn", out_full)

    def test_target_plugin_keeps_referenced_marketplace_and_skips_unrelated_plugins(self) -> None:
        code, _, err = self.run_cli(["sync", "--target", "review@team"])
        self.assertEqual(code, 0, msg=err)
        cmds = self.logged_commands()
        # Marketplace 'team' added + plugin 'review@team' installed; 'other' and 'lint@other' untouched
        self.assertEqual(
            [(c["tool"], c["args"]) for c in cmds],
            [
                ("omp", ["plugin", "marketplace", "add", "owner/team"]),
                ("omp", ["plugin", "install", "--force", "--scope", "user", "review@team"]),
            ],
        )

    def test_target_extension_by_expanded_path_reconciles_only_that_extension(self) -> None:
        expanded_custom = str((self.home / ".pi" / "agent" / "extensions" / "custom.ts").resolve())
        code, _, err = self.run_cli(["sync", "--target", expanded_custom])
        self.assertEqual(code, 0, msg=err)
        global_cfg = json.loads((self.home / ".omp" / "global.json").read_text(encoding="utf-8"))
        self.assertEqual(global_cfg["extensions"], ["~/.pi/agent/extensions/custom.ts"])

    def test_unknown_or_out_of_scope_target_exits_two_without_mutations_or_state_writes(self) -> None:
        before_mcp = self.mcp_target.read_bytes()
        cases = [
            ["install", "--target", "nonexistent-thing"],
            ["sync", "--target", "RTK"],  # packages not active in sync
            ["install", "--only", "mcp", "--target", "RTK"],  # RTK not in mcp section
            ["status", "--only", "skills", "--target", "context7"],  # context7 not in skills
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn("Unknown target", err)
        self.assertEqual(self.logged_commands(), [])
        self.assertEqual(self.mcp_target.read_bytes(), before_mcp)
        self.assertFalse((self.root / "state").exists())

    def test_ambiguous_targets_raise_dotai_error_and_exit_two(self) -> None:
        ambiguous_manifest = json.loads(json.dumps(self.manifest_data))
        # Two packages differing only in case
        ambiguous_manifest["packages"].append(
            {
                "name": "rtk",
                "check": "testpkg check rtk",
                "install": {"default": ["testpkg install rtk 1.0"]},
            }
        )
        # Rename query to "Rtk" so neither exact matches "Rtk"
        # Wait: exact match wins if exact match exists; "Rtk" has no exact match and matches both "RTK" and "rtk"!
        # Also two skill sources with same skill name "shared-skill"
        ambiguous_manifest["skills"] = [
            {
                "source": "https://github.com/mathwro/Skills/tree/main/skills/a",
                "agent": "universal",
                "skills": ["shared-skill"],
                "checkSkills": ["shared-skill"],
            },
            {
                "source": "https://github.com/other/Skills/tree/main/skills/b",
                "agent": "universal",
                "skills": ["shared-skill"],
                "checkSkills": ["shared-skill"],
            },
        ]
        self.write_manifests(ambiguous_manifest)

        code_pkg, _, err_pkg = self.run_cli(["install", "--target", "Rtk"])
        self.assertEqual(code_pkg, 2)
        self.assertIn("Ambiguous target", err_pkg)

        code_skill, _, err_skill = self.run_cli(["sync", "--target", "shared-skill"])
        self.assertEqual(code_skill, 2)
        self.assertIn("Ambiguous", err_skill)
        self.assertEqual(self.logged_commands(), [])

    def test_scoped_reconcile_does_not_initialize_partial_recommendation_baselines(self) -> None:
        # Pre-seed a full baseline with both RTK & Graphify and both skills, or run from clean state:
        # When state dir is initially empty, a scoped sync --target context7 must NOT record
        # only context7 (or empty skills) as the baseline in recommended-skills.json / recommended-baseline.json
        # by deriving from the narrowed manifest!
        code, _, err = self.run_cli(["sync", "--target", "context7"])
        self.assertEqual(code, 0, msg=err)
        stored_skills = state.managed_recommendations(self.manifest_path)
        stored_baseline = state.managed_stack_baseline(self.manifest_path)
        # Either state.save_state initialized from full manifest_path on disk (2 skills, 2 packages, 2 mcpServers)
        # or skipped baseline initialization; in NO case may it record a partial subset!
        if stored_skills is not None:
            self.assertEqual(len(stored_skills), 2)
        if stored_baseline is not None:
            self.assertEqual(len(stored_baseline["packages"]), 2)
            self.assertEqual(set(stored_baseline["mcpServers"]), {"context7", "mslearn"})

    def test_repeatable_targets_and_status_target_filtering(self) -> None:
        # Update targeting both a package and an MCP server
        code, _, err = self.run_cli(["install", "--target", "RTK", "--target", "context7"])
        self.assertEqual(code, 0, msg=err)
        installed_pkgs = json.loads((self.home / "packages.json").read_text(encoding="utf-8"))
        self.assertEqual(set(installed_pkgs), {"RTK"})
        after_mcp = json.loads(self.mcp_target.read_text(encoding="utf-8"))
        self.assertEqual(set(after_mcp["mcpServers"]), {"context7"})

        # Status targeting RTK and context7 is now completely healthy (Graphify & mslearn missing, ignored)
        code_st, out_st, _ = self.run_cli(["status", "--target", "RTK", "--target", "context7"])
        self.assertEqual(code_st, 0)
        self.assertIn("RTK", out_st)
        self.assertNotIn("Graphify", out_st)
        self.assertIn("MCP:", out_st)
        self.assertNotIn("Skills:", out_st)
        self.assertNotIn("OMP routing:", out_st)

        # Status targeting missing Graphify exits 1
        code_missing, out_missing, _ = self.run_cli(["status", "--target", "Graphify"])
        self.assertEqual(code_missing, 1)
        self.assertIn("Graphify", out_missing)
        self.assertNotIn("RTK", out_missing)


if __name__ == "__main__":
    unittest.main()
