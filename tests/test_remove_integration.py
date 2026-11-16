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
    mcp,
    omp,
    recommendations,
    releases,
    runtime,
    skills,
    state,
    terminal,
)


INSTALLER = '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["DOTAI_HOME"])
control = home / "installer.json"
data = json.loads(control.read_text()) if control.exists() else {"entries": []}
args = sys.argv[1:]
action = args[2]
if action == "list":
    data["lists"] = data.get("lists", 0) + 1
    control.write_text(json.dumps(data))
    if data.get("malformed"):
        print('{"unexpected": true}')
    else:
        agent = args[args.index("--agent") + 1]
        result = []
        for entry in data.get("entries", []):
            if entry["targetAgent"] != agent or not pathlib.Path(entry["path"]).exists():
                continue
            record = {k: v for k, v in entry.items() if k != "targetAgent"}
            record.setdefault("scope", "global")
            record.setdefault("agents", [])
            result.append(record)
        print(json.dumps(result))
else:
    sys.exit(11)
'''


OMP_FIXTURE = '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["DOTAI_HOME"])
project = pathlib.Path(os.environ["OMP_TEST_PROJECT"])
args = sys.argv[1:]

def load(path):
    return json.loads(path.read_text()) if path.exists() else {}

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))

if args[:2] == ["plugin", "marketplace"] and len(args) == 4 and args[2] == "remove":
    if os.environ.get("OMP_TEST_FAIL") == "marketplace-remove":
        print("marketplace remove failed", file=sys.stderr)
        sys.exit(7)
    name = args[3]
    path = home / ".omp" / "marketplaces.json"
    registry = load(path)
    records = registry.get("marketplaces", {})
    if name not in records:
        print(f'Marketplace "{name}" not found', file=sys.stderr)
        sys.exit(1)
    del records[name]
    save(path, registry)
    sys.exit(0)

if args[:2] == ["plugin", "uninstall"] and len(args) == 5 and args[2] == "--scope":
    if os.environ.get("OMP_TEST_FAIL") == "plugin-uninstall":
        print("plugin uninstall failed", file=sys.stderr)
        sys.exit(7)
    scope = args[3]
    plugin_id = args[4]
    path = (home if scope == "user" else project) / ".omp" / "plugins" / "installed_plugins.json"
    registry = load(path)
    records = registry.get("plugins", {})
    if plugin_id not in records:
        print(f"{plugin_id} is not installed", file=sys.stderr)
        sys.exit(1)
    del records[plugin_id]
    save(path, registry)
    sys.exit(0)

sys.exit(64)
'''


class RemoveIntegrationTests(unittest.TestCase):
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

        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "DOTAI_HOME": str(self.home),
            "DOTAI_STATE_DIR": str(self.root / "state"),
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "GH_HOST": "github.com",
            "NO_COLOR": "1",
            "DOTAI_PLATFORM": "linux",
            "OMP_TEST_PROJECT": str(self.project),
        }
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(runtime, "ROOT", self.project),
            mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.example_path),
            mock.patch.object(releases, "latest_release_version", return_value=None),
            mock.patch.object(terminal, "_COLOR_ENABLED", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        npx_script = self.bin / "npx.py"
        npx_script.write_text(INSTALLER, encoding="utf-8")
        omp_script = self.bin / "omp.py"
        omp_script.write_text(OMP_FIXTURE, encoding="utf-8")
        real_run = subprocess.run

        def route_commands(command, *args, **kwargs):
            if isinstance(command, list) and command:
                if command[0] == "npx":
                    command = [sys.executable, str(npx_script), *command[1:]]
                elif command[0] == "omp":
                    command = [sys.executable, str(omp_script), *command[1:]]
            return real_run(command, *args, **kwargs)

        run_patch = mock.patch.object(subprocess, "run", side_effect=route_commands)
        run_patch.start()
        self.addCleanup(run_patch.stop)

        self.base_manifest = {
            "version": 1,
            "packages": [
                {
                    "name": "curl",
                    "check": ["curl", "--version"],
                    "install": {"linux": ["echo install curl"]},
                },
                {
                    "name": "Example",
                    "check": "example --version",
                    "install": {"linux": ["echo install example"]},
                },
            ],
            "skills": [
                {
                    "source": "owner/skills",
                    "agent": "universal",
                    "skills": ["review"],
                    "checkSkills": ["review"],
                    "ompEnvironment": {"REVIEW_MODE": "strict"},
                },
                {
                    "source": "other/skills",
                    "agent": "universal",
                    "skills": ["keep"],
                    "checkSkills": ["keep"],
                    "ompEnvironment": {"KEEP_DEFAULT": "1"},
                },
            ],
            "marketplaces": [
                {"name": "team", "source": "owner/team"},
                {"name": "keep-mp", "source": "owner/keep"},
            ],
            "plugins": [
                {"id": "review@team", "scope": "user"},
                {"id": "local@team", "scope": "project"},
            ],
            "ompExtensions": [],
            "ompRouting": None,
            "mcp": {
                "target": "~/.omp/agent/mcp.json",
                "servers": {
                    "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                    "local-tools": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
                },
            },
        }
        self.write_manifest_file(self.base_manifest)
        self.example_path.write_text(json.dumps(self.base_manifest, indent=2) + "\n", encoding="utf-8")

    def write_manifest_file(self, data: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_manifest_file(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def invoke(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--manifest", str(self.manifest_path), "--color", "never", *args])
        return code, out.getvalue(), err.getvalue()

    def backups(self, path: Path) -> list[Path]:
        return sorted(path.parent.glob(f"{path.name}.bak.*"))

    def test_remove_manifest_only_for_every_supported_kind_with_private_backups(self) -> None:
        commands = [
            ("tool", "Example"),
            ("skill", "owner/skills"),
            ("marketplace", "team"),
            ("plugin", "review@team"),
            ("mcp", "local-tools"),
        ]
        for kind, identifier in commands:
            code, out, err = self.invoke("remove", kind, identifier)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(err, "")
            self.assertIn("[OK]", out)

        updated = self.read_manifest_file()
        self.assertEqual([pkg["name"] for pkg in updated["packages"]], ["curl"])
        self.assertEqual([sk["source"] for sk in updated["skills"]], ["other/skills"])
        self.assertEqual([mp["name"] for mp in updated["marketplaces"]], ["keep-mp"])
        self.assertEqual([pl["id"] for pl in updated["plugins"]], ["local@team"])
        self.assertEqual(list(updated["mcp"]["servers"]), ["context7"])
        self.assertIsNone(updated["ompRouting"])
        manifest_backups = self.backups(self.manifest_path)
        self.assertEqual(len(manifest_backups), 5)
        if os.name != "nt":
            for backup in manifest_backups:
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_remove_missing_entry_exits_2_without_writing_or_backing_up(self) -> None:
        original = self.manifest_path.read_bytes()
        for kind, identifier in (
            ("tool", "Nonexistent"),
            ("skill", "missing/repo"),
            ("marketplace", "missing"),
            ("plugin", "missing@team"),
            ("mcp", "missing"),
        ):
            with self.subTest(kind=kind):
                code, out, err = self.invoke("remove", kind, identifier)
                self.assertEqual(code, 2, out + err)
                self.assertIn("dotai:", err)
                self.assertEqual(self.manifest_path.read_bytes(), original)
                self.assertEqual(self.backups(self.manifest_path), [])

    def test_remove_skill_prefers_exact_source_and_falls_back_to_unambiguous_github_repo(self) -> None:
        # Unambiguous normalized fallback: manifest has https://github.com/Owner/Skills.git
        data = json.loads(json.dumps(self.base_manifest))
        data["skills"] = [
            {"source": "https://github.com/Owner/Skills.git", "agent": "universal", "skills": ["review"], "checkSkills": ["review"]},
            {"source": "other/skills", "agent": "universal", "skills": ["keep"], "checkSkills": ["keep"]},
        ]
        self.write_manifest_file(data)
        code, out, err = self.invoke("remove", "skill", "owner/skills")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([sk["source"] for sk in self.read_manifest_file()["skills"]], ["other/skills"])

        # Exact match wins even when another spelling normalizes to the same repo
        data["skills"] = [
            {"source": "owner/skills", "agent": "universal", "skills": ["a"], "checkSkills": ["a"]},
            {"source": "https://github.com/owner/skills", "agent": "universal", "skills": ["b"], "checkSkills": ["b"]},
        ]
        self.write_manifest_file(data)
        code, out, err = self.invoke("remove", "skill", "owner/skills")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            [sk["source"] for sk in self.read_manifest_file()["skills"]],
            ["https://github.com/owner/skills"],
        )

        # Ambiguous normalized fallback (neither exact, two entries normalize to same repo) -> DotAiError (exit 2)
        data["skills"] = [
            {"source": "github:owner/skills", "agent": "universal", "skills": ["a"], "checkSkills": ["a"]},
            {"source": "https://github.com/owner/skills.git", "agent": "universal", "skills": ["b"], "checkSkills": ["b"]},
        ]
        self.write_manifest_file(data)
        before = self.manifest_path.read_bytes()
        code, out, err = self.invoke("remove", "skill", "owner/skills")
        self.assertEqual(code, 2, out + err)
        self.assertIn("Ambiguous", err)
        self.assertEqual(self.manifest_path.read_bytes(), before)

    def test_remove_tool_uninstall_is_refused_with_clear_error_and_no_mutation(self) -> None:
        original = self.manifest_path.read_bytes()
        code, out, err = self.invoke("remove", "tool", "curl", "--uninstall")
        self.assertEqual(code, 2, out + err)
        self.assertIn("System packages are not uninstalled automatically", err)
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertEqual(self.backups(self.manifest_path), [])

    def test_remove_dry_run_previews_manifest_and_uninstall_without_mutating_files(self) -> None:
        skill_dir = self.home / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# review\n", encoding="utf-8")
        self.env_target.parent.mkdir(parents=True, exist_ok=True)
        self.env_target.write_bytes(
            b"USER_VAR=1\n# BEGIN DotAi skill environment\nKEEP_DEFAULT=1\nREVIEW_MODE=strict\n# END DotAi skill environment\n"
        )
        self.mcp_target.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_target.write_text(
            json.dumps({"mcpServers": {"local-tools": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}}}),
            encoding="utf-8",
        )
        mp_path = self.home / ".omp" / "marketplaces.json"
        mp_path.write_text(json.dumps({"marketplaces": {"team": {"source": "owner/team"}}}), encoding="utf-8")
        pl_path = self.home / ".omp" / "plugins" / "installed_plugins.json"
        pl_path.parent.mkdir(parents=True, exist_ok=True)
        pl_path.write_text(json.dumps({"plugins": {"review@team": {"revision": 1}}}), encoding="utf-8")

        snapshots = {
            path: path.read_bytes()
            for path in (self.manifest_path, self.env_target, self.mcp_target, mp_path, pl_path)
        }
        for args in (
            ("remove", "tool", "Example", "--dry-run"),
            ("remove", "skill", "owner/skills", "--uninstall", "--dry-run"),
            ("remove", "mcp", "local-tools", "--uninstall", "--dry-run"),
            ("remove", "marketplace", "team", "--uninstall", "--dry-run"),
            ("remove", "plugin", "review@team", "--uninstall", "--dry-run"),
        ):
            with self.subTest(args=args):
                code, out, err = self.invoke(*args)
                self.assertEqual(code, 0, out + err)
                self.assertIn("[RUN]", out)
                self.assertEqual(
                    {path: path.read_bytes() for path in snapshots},
                    snapshots,
                )
                self.assertEqual(self.backups(self.manifest_path), [])
                self.assertEqual(self.backups(self.mcp_target), [])
                self.assertEqual(self.backups(self.env_target), [])
                self.assertTrue(skill_dir.is_dir())

    def test_remove_skill_uninstall_retires_skill_and_reconciles_remaining_omp_environment(self) -> None:
        skill_dir = self.home / ".agents" / "skills" / "review"
        keep_dir = self.home / ".agents" / "skills" / "keep"
        skill_dir.mkdir(parents=True)
        keep_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# review\n", encoding="utf-8")
        (keep_dir / "SKILL.md").write_text("# keep\n", encoding="utf-8")
        (self.home / "installer.json").write_text(
            json.dumps({
                "entries": [
                    {"name": "review", "path": str(skill_dir), "source": "owner/skills", "targetAgent": "universal"},
                    {"name": "keep", "path": str(keep_dir), "source": "other/skills", "targetAgent": "universal"},
                ]
            }),
            encoding="utf-8",
        )
        self.env_target.parent.mkdir(parents=True, exist_ok=True)
        self.env_target.write_bytes(
            b"USER_VAR=kept\n# BEGIN DotAi skill environment\nKEEP_DEFAULT=1\nREVIEW_MODE=strict\n# END DotAi skill environment\n"
        )

        code, out, err = self.invoke("remove", "skill", "owner/skills", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(skill_dir.exists())
        self.assertTrue(keep_dir.is_dir())
        self.assertEqual([sk["source"] for sk in self.read_manifest_file()["skills"]], ["other/skills"])
        self.assertEqual(
            self.env_target.read_bytes(),
            b"USER_VAR=kept\n# BEGIN DotAi skill environment\nKEEP_DEFAULT=1\n# END DotAi skill environment\n",
        )
        self.assertEqual(len(self.backups(self.env_target)), 1)

    def test_remove_skill_uninstall_restores_manifest_and_exits_1_when_retirement_fails(self) -> None:
        skill_dir = self.home / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# review\n", encoding="utf-8")
        # Shared with an unknown agent -> retirement_directory refuses
        (self.home / "installer.json").write_text(
            json.dumps({
                "entries": [
                    {
                        "name": "review",
                        "path": str(skill_dir),
                        "source": "owner/skills",
                        "targetAgent": "universal",
                        "agents": ["Cursor"],
                    }
                ]
            }),
            encoding="utf-8",
        )
        self.env_target.parent.mkdir(parents=True, exist_ok=True)
        original_env = b"# BEGIN DotAi skill environment\nKEEP_DEFAULT=1\nREVIEW_MODE=strict\n# END DotAi skill environment\n"
        self.env_target.write_bytes(original_env)
        original_manifest = self.manifest_path.read_bytes()

        code, out, err = self.invoke("remove", "skill", "owner/skills", "--uninstall")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Cannot retire shared or ambiguous skill directory", out)
        self.assertIn("Restored", out)
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
        self.assertTrue(skill_dir.is_dir())
        self.assertEqual(self.env_target.read_bytes(), original_env)

    def test_removed_recommended_skill_stays_preserved_as_user_choice_until_enforced(self) -> None:
        state.save_managed_recommendations(self.manifest_path, list(self.base_manifest["skills"]))
        code, out, err = self.invoke("remove", "skill", "owner/skills")
        self.assertEqual(code, 0, out + err)

        manifest = manifests.load_manifest(self.manifest_path)
        _, changes, conflicts = recommendations.recommended_skill_plan(manifest, self.manifest_path, enforce=False)
        self.assertEqual(changes, [])
        self.assertEqual(conflicts, ["owner/skills"])

        _, enforced_changes, enforced_conflicts = recommendations.recommended_skill_plan(
            manifest, self.manifest_path, enforce=True
        )
        self.assertEqual(enforced_conflicts, [])
        self.assertEqual(len(enforced_changes), 1)
        self.assertEqual(enforced_changes[0]["kind"], "add")
        self.assertEqual(enforced_changes[0]["source"], "owner/skills")

    def test_remove_mcp_uninstall_updates_only_target_mcpservers_and_leaves_external_configs(self) -> None:
        self.mcp_target.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_target.write_text(
            json.dumps({
                "$schema": "https://example.test/schema.json",
                "disabledServers": ["other"],
                "customSetting": {"keep": True},
                "mcpServers": {
                    "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                    "local-tools": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
                    "unmanaged": {"type": "http", "url": "https://private.example.test/mcp"},
                },
            }),
            encoding="utf-8",
        )
        cursor_mcp = self.home / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        cursor_payload = json.dumps({
            "mcpServers": {
                "local-tools": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
            }
        }).encode("utf-8")
        cursor_mcp.write_bytes(cursor_payload)

        code, out, err = self.invoke("remove", "mcp", "local-tools", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local-tools", self.read_manifest_file()["mcp"]["servers"])

        target_after = json.loads(self.mcp_target.read_text(encoding="utf-8"))
        self.assertEqual(set(target_after["mcpServers"]), {"context7", "unmanaged"})
        self.assertEqual(target_after["customSetting"], {"keep": True})
        self.assertEqual(target_after["disabledServers"], ["other"])
        self.assertEqual(cursor_mcp.read_bytes(), cursor_payload)
        target_backups = self.backups(self.mcp_target)
        self.assertEqual(len(target_backups), 1)
        if os.name != "nt":
            self.assertEqual(target_backups[0].stat().st_mode & 0o777, 0o600)

    def test_remove_mcp_uninstall_succeeds_cleanly_when_target_missing_or_server_absent(self) -> None:
        # Target file absent
        code, out, err = self.invoke("remove", "mcp", "local-tools", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertIn("[OK]", out)
        self.assertFalse(self.mcp_target.exists())

        # Target file exists without context7 in mcpServers
        self.mcp_target.parent.mkdir(parents=True, exist_ok=True)
        payload = b'{"mcpServers": {"unmanaged": {"url": "https://private.example.test/mcp"}}}\n'
        self.mcp_target.write_bytes(payload)
        code, out, err = self.invoke("remove", "mcp", "context7", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertIn("[OK]", out)
        self.assertEqual(self.mcp_target.read_bytes(), payload)
        self.assertEqual(self.backups(self.mcp_target), [])

    def test_remove_mcp_uninstall_rejects_invalid_target_json_before_manifest_mutation(self) -> None:
        self.mcp_target.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_target.write_bytes(b'{"mcpServers": [1, 2]}')
        original_manifest = self.manifest_path.read_bytes()

        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                code, out, err = self.invoke("remove", "mcp", "local-tools", "--uninstall", *extra)
                self.assertEqual(code, 1, out + err)
                self.assertIn("[FAIL]", out)
                self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
                self.assertEqual(self.backups(self.manifest_path), [])
                self.assertEqual(self.backups(self.mcp_target), [])

    def test_remove_skill_uninstall_rejects_malformed_dotenv_before_manifest_or_skill_mutation(self) -> None:
        skill_dir = self.home / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# review\n", encoding="utf-8")
        (self.home / "installer.json").write_text(
            json.dumps({
                "entries": [
                    {"name": "review", "path": str(skill_dir), "source": "owner/skills", "targetAgent": "universal"}
                ]
            }),
            encoding="utf-8",
        )
        self.env_target.parent.mkdir(parents=True, exist_ok=True)
        malformed_env = b"# BEGIN DotAi skill environment\nUNTERMINATED=1\n"
        self.env_target.write_bytes(malformed_env)
        original_manifest = self.manifest_path.read_bytes()

        code, out, err = self.invoke("remove", "skill", "owner/skills", "--uninstall")
        self.assertEqual(code, 1, out + err)
        self.assertIn("[FAIL]", out)
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
        self.assertEqual(self.backups(self.manifest_path), [])
        self.assertTrue(skill_dir.is_dir())
        self.assertEqual(self.env_target.read_bytes(), malformed_env)
    def test_remove_plugin_and_marketplace_uninstall_use_omp_and_handle_uninstalled_cleanly(self) -> None:
        mp_path = self.home / ".omp" / "marketplaces.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(
            json.dumps({"marketplaces": {"team": {"source": "owner/team"}, "keep-mp": {"source": "owner/keep"}}}),
            encoding="utf-8",
        )
        user_plugins = self.home / ".omp" / "plugins" / "installed_plugins.json"
        user_plugins.parent.mkdir(parents=True, exist_ok=True)
        user_plugins.write_text(json.dumps({"plugins": {"review@team": {"revision": 1}}}), encoding="utf-8")

        # Installed user plugin is uninstalled via omp
        code, out, err = self.invoke("remove", "plugin", "review@team", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(user_plugins.read_text(encoding="utf-8")), {"plugins": {}})
        self.assertEqual([pl["id"] for pl in self.read_manifest_file()["plugins"]], ["local@team"])

        # Project-scoped plugin absent from project registry succeeds without failing omp uninstall
        code, out, err = self.invoke("remove", "plugin", "local@team", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read_manifest_file()["plugins"], [])

        # Installed marketplace is removed via omp
        code, out, err = self.invoke("remove", "marketplace", "team", "--uninstall")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            json.loads(mp_path.read_text(encoding="utf-8")),
            {"marketplaces": {"keep-mp": {"source": "owner/keep"}}},
        )

    def test_remove_plugin_or_marketplace_uninstall_restores_manifest_on_omp_failure(self) -> None:
        mp_path = self.home / ".omp" / "marketplaces.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(json.dumps({"marketplaces": {"team": {"source": "owner/team"}}}), encoding="utf-8")
        user_plugins = self.home / ".omp" / "plugins" / "installed_plugins.json"
        user_plugins.parent.mkdir(parents=True, exist_ok=True)
        user_plugins.write_text(json.dumps({"plugins": {"review@team": {"revision": 1}}}), encoding="utf-8")

        original_manifest = self.manifest_path.read_bytes()
        with mock.patch.dict(os.environ, {"OMP_TEST_FAIL": "plugin-uninstall"}):
            code, out, err = self.invoke("remove", "plugin", "review@team", "--uninstall")
        self.assertEqual(code, 1, out + err)
        self.assertIn("[FAIL]", out)
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)

        with mock.patch.dict(os.environ, {"OMP_TEST_FAIL": "marketplace-remove"}):
            code, out, err = self.invoke("remove", "marketplace", "team", "--uninstall")
        self.assertEqual(code, 1, out + err)
        self.assertIn("[FAIL]", out)
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)


if __name__ == "__main__":
    unittest.main()
