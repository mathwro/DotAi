"""Regression boundaries for conservative synchronization and component lifecycle."""
from __future__ import annotations

import argparse
import copy
from contextlib import redirect_stdout
import io
import json
import os
import importlib
import importlib.util
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dotai_app import integrations, omp, runtime, skills


class RecordingRunner(runtime.Runner):
    def __init__(self, *, dry_run=False):
        super().__init__("macos", dry_run=dry_run)
        self.commands = []

    def run(self, command, label, **kwargs):
        self.commands.append(command)
        if not self.dry_run and isinstance(command, list):
            if len(command) > 3 and command[0] == "npx" and command[3] == "add":
                agent = command[command.index("--agent") + 1]
                for index, part in enumerate(command):
                    if part != "--skill":
                        continue
                    folder = skills.skill_root({"agent": agent}) / skills.installed_skill_name(command[index + 1])
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "SKILL.md").write_text("# Installed by isolated test installer\n")
            if command[:3] == ["omp", "plugin", "install"]:
                identity = command[-1]
                scope = command[command.index("--scope") + 1]
                folder = runtime.home_dir() / ".omp/plugins/cache/plugins" / (identity.replace("@", "___") + "___1.2.3")
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "package.json").write_text(json.dumps({"name": "fixture-plugin", "version": "1.2.3"}))
                registry = omp.plugin_registry({"scope": scope})
                registry.parent.mkdir(parents=True, exist_ok=True)
                registry.write_text(json.dumps({"version": 2, "plugins": {identity: [{"scope": scope, "installPath": str(folder), "version": "1.2.3", "installedAt": "2026-01-01", "lastUpdated": "2026-01-01"}]}}))
            if command[:2] == ["omp", "plugin"] and command[2] in {"enable", "disable", "uninstall"}:
                registry = omp.plugin_registry({"scope": command[command.index("--scope") + 1]})
                data = json.loads(registry.read_text())
                if command[2] == "uninstall":
                    del data["plugins"][command[-1]]
                else:
                    data["plugins"][command[-1]][0]["enabled"] = command[2] == "enable"
                registry.write_text(json.dumps(data))
        return subprocess.CompletedProcess(command, 0, "", "")


class SyncContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state"), "XDG_STATE_HOME": str(self.home / "xdg"), "GH_HOST": "github.com"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.skill = {"source": "owner/repository", "agent": "universal", "skills": ["review"], "checkSkills": ["review"]}

    def record_upstream_ownership(self, folder):
        lock = self.home / "xdg/skills/.skill-lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"version": 3, "skills": {"review": {
            "sourceType": "github", "source": "owner/repository",
            "sourceUrl": "https://github.com/owner/repository", "ref": None,
            "skillFolderHash": skills.installed_skill_tree_hash(folder),
        }}}))

    def create_skill(self):
        folder = self.home / ".agents/skills/review"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        return folder

    def test_sync_refuses_existing_unknown_content(self):
        folder = self.create_skill()
        runner = RecordingRunner()
        with redirect_stdout(io.StringIO()) as out:
            skills.reconcile_skills({"skills": [self.skill]}, runner)
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertIn("DRIFT", out.getvalue())
        self.assertEqual("# Review\n", (folder / "SKILL.md").read_text())

    def test_sync_refuses_partial_existing_selection(self):
        self.create_skill()
        declared = {**self.skill, "skills": ["review", "other"], "checkSkills": ["review", "other"]}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner)
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)

    def test_explicit_check_override_cannot_hide_unknown_installer_destination(self):
        folder = self.create_skill()
        declared = {**self.skill, "checkSkills": ["different-directory"]}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner)
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertEqual("# Review\n", (folder / "SKILL.md").read_text())

    def test_wildcard_refresh_cannot_use_subset_ownership_as_global_permission(self):
        self.record_upstream_ownership(self.create_skill())
        unrelated = self.home / ".agents/skills/unmanaged"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("# Unmanaged\n")
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [{**self.skill, "skills": ["*"]}]}, runner, mode="update")
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertEqual("# Unmanaged\n", (unrelated / "SKILL.md").read_text())

    def test_sync_installs_only_missing_selection_beside_owned_copy(self):
        folder = self.create_skill()
        self.record_upstream_ownership(folder)
        declared = {**self.skill, "skills": ["review", "NEW Skill"], "checkSkills": ["review", "new-skill"]}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner)
        self.assertEqual([], runner.failures)
        self.assertEqual([["npx", "--yes", "skills@latest", "add", "owner/repository", "--global", "--agent", "universal", "--skill", "NEW Skill", "--copy", "--yes"]], runner.commands)
        self.assertEqual("# Review\n", (folder / "SKILL.md").read_text())

    def test_sync_refuses_ambiguous_missing_selection_mapping(self):
        folder = self.create_skill()
        self.record_upstream_ownership(folder)
        declared = {**self.skill, "skills": ["review", "other", "third"], "checkSkills": ["review", "different-directory"]}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner)
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)

    def test_update_refuses_linked_other_agent_copy_even_with_matching_hash(self):
        other = self.home / ".pi/agent/skills/review"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("# Independent\n")
        root = self.home / ".agents/skills"
        root.mkdir(parents=True)
        (root / "review").symlink_to(other, target_is_directory=True)
        self.record_upstream_ownership(other)
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [self.skill]}, runner, mode="update")
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertEqual("# Independent\n", (other / "SKILL.md").read_text())

    def test_missing_skill_refuses_redirected_agent_root(self):
        other_root = self.home / ".pi/agent/skills"
        other_root.mkdir(parents=True)
        (self.home / ".agents").mkdir()
        (self.home / ".agents/skills").symlink_to(other_root, target_is_directory=True)
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [self.skill]}, runner)
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertEqual([], list(other_root.iterdir()))

    def test_sync_installs_missing_declared_revision_and_installer_version(self):
        declared = {**self.skill, "revision": "abc123", "installerVersion": "1.2.3"}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner)
        self.assertEqual([["npx", "--yes", "skills@1.2.3", "add", "https://github.com/owner/repository/tree/abc123", "--global", "--agent", "universal", "--skill", "review", "--copy", "--yes"]], runner.commands)

    def test_update_explicitly_refreshes_owned_skill(self):
        folder = self.create_skill()
        self.record_upstream_ownership(folder)
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [self.skill]}, runner, mode="update")
        self.assertEqual(1, len(runner.commands))

    def test_update_owned_current_revision_can_target_new_revision(self):
        folder = self.create_skill()
        self.record_upstream_ownership(folder)
        lock_path = self.home / "xdg/skills/.skill-lock.json"
        lock = json.loads(lock_path.read_text())
        lock["skills"]["review"]["ref"] = "old"
        lock_path.write_text(json.dumps(lock))
        declared = {**self.skill, "revision": "new", "installerVersion": "1.2.3"}
        runner = RecordingRunner()
        skills.reconcile_skills({"skills": [declared]}, runner, mode="update")
        self.assertEqual([], runner.failures)
        self.assertEqual([["npx", "--yes", "skills@1.2.3", "add", "https://github.com/owner/repository/tree/new", "--global", "--agent", "universal", "--skill", "review", "--copy", "--yes"]], runner.commands)

    def test_sync_does_not_force_install_healthy_plugins(self):
        registry = self.home / ".omp/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        folder = self.home / ".omp/plugins/cache/plugins/team___review___1.2.3"
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": "review-plugin", "version": "1.2.3"}))
        registry.write_text(json.dumps({"version": 2, "plugins": {"review@team": [{"scope": "user", "version": "1.2.3", "installPath": str(folder), "installedAt": "2026-01-01", "lastUpdated": "2026-01-01"}]}}))
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "sync")
        self.assertEqual([], runner.commands)
        self.assertEqual([], runner.failures)

    def test_missing_plugin_install_does_not_force(self):
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "sync")
        self.assertEqual([["omp", "plugin", "install", "--scope", "user", "review@team"]], runner.commands)

    def test_update_installs_missing_plugin_instead_of_upgrading_absent_entry(self):
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "update")
        self.assertEqual([["omp", "plugin", "install", "--scope", "user", "review@team"]], runner.commands)

    def test_sync_refuses_invalid_plugin_registry_before_any_installer(self):
        registry = self.home / ".omp/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("{invalid")
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "sync")
        self.assertEqual([], runner.commands)
        self.assertTrue(runner.failures)
        self.assertEqual("{invalid", registry.read_text())

    def test_incremental_skill_add_preserves_agent_and_prior_selections(self):
        manifest = {"skills": [self.skill, {**self.skill, "agent": "pi", "skills": ["independent"], "checkSkills": ["independent"]}]}
        args = argparse.Namespace(kind="skill", source="owner/repository", agent="universal", skills=["other"], check_skills=None, replace=False)
        with patch.object(integrations.manifests, "write_manifest"):
            integrations.add_integration(args, manifest, self.home / "stack.json")
        self.assertEqual(["review", "other"], manifest["skills"][0]["skills"])
        self.assertEqual(["review", "other"], manifest["skills"][0]["checkSkills"])
        self.assertEqual(["independent"], manifest["skills"][1]["skills"])

    def test_explicit_skill_replace_changes_only_same_source_agent(self):
        manifest = {"skills": [self.skill, {**self.skill, "agent": "pi"}]}
        args = argparse.Namespace(kind="skill", source="owner/repository", agent="universal", skills=["other"], check_skills=None, replace=True)
        with patch.object(integrations.manifests, "write_manifest"):
            integrations.add_integration(args, manifest, self.home / "stack.json")
        self.assertEqual(["other"], manifest["skills"][0]["skills"])
        self.assertEqual(["review"], manifest["skills"][1]["skills"])


class LifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dotai_app.lifecycle"), "Component lifecycle is not implemented")
        self.lifecycle = importlib.import_module("dotai_app.lifecycle")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state"), "XDG_STATE_HOME": str(self.home / "xdg"), "GH_HOST": "github.com"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = patch.object(runtime, "ROOT", self.home / "project")
        self.root.start()
        self.addCleanup(self.root.stop)
        self.path = self.home / "stack.json"
        self.skill = {"source": "owner/repository", "agent": "universal", "skills": ["review"], "checkSkills": ["review"]}
        self.manifest = {
            "version": 2, "prerequisites": [], "packages": [], "skills": [self.skill],
            "marketplaces": [], "plugins": [], "ompExtensions": [], "ompRouting": None,
            "mcp": {"target": str(self.home / "mcp.json"), "servers": {}},
        }
        self.runner = RecordingRunner()
        self.runner.output = self.output
        self.extensions = []

    def output(self, command):
        if command[:4] == ["omp", "config", "get", "extensions"]:
            return json.dumps({"value": self.extensions})
        if "list" in command and any(str(value).startswith("skills@") for value in command):
            records = []
            for skill in self.manifest["skills"]:
                for name in skill.get("checkSkills", []):
                    folder = skills.skill_root(skill) / name
                    if folder.exists():
                        records.append({"name": name, "source": skill["source"], "scope": "global", "path": str(folder), "agents": [skill.get("agent", "universal")]})
            return json.dumps(records)
        return ""

    def save(self):
        self.path.write_text(json.dumps(self.manifest))

    def dispatch(self, command, selector=None, *, uninstall=False, dry_run=False):
        args = argparse.Namespace(command=command, selector=selector, selectors=[], uninstall=uninstall, dry_run=dry_run)
        self.runner.dry_run = dry_run
        return self.lifecycle.dispatch(args, self.manifest, self.path, self.runner)

    def create_skill(self, name="review", agent="universal"):
        folder = skills.skill_root({"agent": agent}) / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"# {name}\n")
        return folder

    def create_mcp(self):
        required = {"type": "http", "url": "https://example.test/mcp"}
        self.manifest["skills"] = []
        self.manifest["mcp"]["servers"] = {"docs": required}
        config = {"keep": {"theme": "dark"}, "mcpServers": {
            "alias": {**required, "headers": {"Authorization": "LOCAL_AUTH_REFERENCE"}},
            "unmanaged": {"type": "http", "url": "https://other.test/mcp"},
        }}
        Path(self.manifest["mcp"]["target"]).write_text(json.dumps(config))
        return config

    def test_targeted_filter_covers_every_declared_integration_type(self):
        self.manifest.update({
            "packages": [{"name": "custom", "managed": True, "check": ["custom", "--version"], "install": []}],
            "marketplaces": [{"name": "team", "source": "owner/marketplace"}],
            "plugins": [{"id": "review@team", "scope": "user"}],
            "ompExtensions": ["~/extension.ts"], "ompRouting": {"private": "intent"},
            "prerequisites": [{"name": "git", "check": ["git", "--version"]}],
        })
        self.manifest["mcp"]["servers"] = {"docs": {"type": "http", "url": "https://example.test/mcp"}}
        cases = [("tool:custom", "packages"), ("skill:owner/repository", "skills"), ("plugin:review@team", "plugins"), ("marketplace:team", "marketplaces"), ("mcp:docs", "mcp"), ("extension:~/extension.ts", "ompExtensions")]
        original = json.dumps(self.manifest, sort_keys=True)
        for selector, selected in cases:
            with self.subTest(selector=selector):
                view = self.lifecycle.filter_manifest(self.manifest, [selector])
                self.assertEqual([{"name": "git", "check": ["git", "--version"]}], view["prerequisites"])
                self.assertIsNone(view["ompRouting"])
                for section in ("packages", "skills", "plugins", "marketplaces", "ompExtensions"):
                    self.assertEqual(1 if selected == section else 0, len(view[section]))
                self.assertEqual(1 if selected == "mcp" else 0, len(view["mcp"]["servers"]))
        self.assertEqual(original, json.dumps(self.manifest, sort_keys=True))

    def test_unknown_or_ambiguous_selectors_refuse_before_work(self):
        self.manifest["skills"].append({**self.skill, "agent": "pi"})
        for selector in ("skill:owner/repository", "unknown:any", "mcp:missing"):
            with self.subTest(selector=selector), self.assertRaises(runtime.DotAiError):
                self.lifecycle.filter_manifest(self.manifest, [selector])
        view = self.lifecycle.filter_manifest(self.manifest, ["skill:owner/repository@pi"])
        self.assertEqual("pi", view["skills"][0]["agent"])
        self.assertEqual([], self.runner.commands)

    def test_default_remove_forgets_declaration_without_removing_copy(self):
        folder = self.create_skill()
        self.save()
        self.assertEqual(0, self.dispatch("remove", "skill:owner/repository"))
        self.assertTrue(folder.is_dir())
        self.assertEqual([], json.loads(self.path.read_text())["skills"])
        self.assertEqual([], self.runner.commands)

    def test_uninstall_refuses_presence_without_ownership(self):
        folder = self.create_skill()
        self.save()
        original = self.path.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "skill:owner/repository", uninstall=True)
        self.assertEqual(original, self.path.read_bytes())
        self.assertTrue(folder.is_dir())
        self.assertEqual([], self.runner.commands)

    def test_explicit_adoption_allows_remove_without_other_agent_damage(self):
        folder = self.create_skill()
        other = self.create_skill(agent="pi")
        self.save()
        self.assertEqual(0, self.dispatch("adopt", "skill:owner/repository"))
        self.assertEqual(0, self.dispatch("remove", "skill:owner/repository", uninstall=True))
        self.assertFalse(folder.exists())
        self.assertTrue(other.is_dir())
        self.assertEqual([], self.runner.commands)

    def test_adopted_modified_selection_refuses_entire_deletion_set(self):
        first = self.create_skill()
        second = self.create_skill("other")
        self.skill["skills"] = ["review", "other"]
        self.skill["checkSkills"] = ["review", "other"]
        self.save()
        self.dispatch("adopt", "skill:owner/repository")
        (second / "SKILL.md").write_text("# Locally changed\n")
        original = self.path.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "skill:owner/repository", uninstall=True)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual(original, self.path.read_bytes())

    def test_adoption_does_not_accept_unrelated_source_record(self):
        self.create_skill()
        self.runner.output = lambda command: json.dumps([{"name": "review", "source": "unrelated/source", "scope": "global", "path": str(skills.skill_root(self.skill) / "review"), "agents": ["universal"]}])
        self.save()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("adopt", "skill:owner/repository")
        self.assertFalse((self.home / "state/component-receipts.json").exists())

    def test_adoption_receipt_is_honored_by_sync_and_detects_later_edits(self):
        folder = self.create_skill()
        self.save()
        self.dispatch("adopt", "skill:owner/repository")
        skills.reconcile_skills(self.manifest, self.runner, receipt_owned=self.lifecycle.skill_receipt_owned)
        self.assertEqual([], self.runner.commands)
        self.assertEqual([], self.runner.failures)
        (folder / "SKILL.md").write_text("# Modified after adoption\n")
        skills.reconcile_skills(self.manifest, self.runner, receipt_owned=self.lifecycle.skill_receipt_owned)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(self.runner.failures)

    def test_skill_disable_moves_only_owned_copy_out_of_active_root(self):
        folder = self.create_skill()
        other = self.create_skill(agent="pi")
        self.save()
        self.dispatch("adopt", "skill:owner/repository")
        self.assertEqual(0, self.dispatch("disable", "skill:owner/repository"))
        self.assertFalse(folder.exists())
        self.assertTrue(other.is_dir())
        self.assertFalse(json.loads(self.path.read_text())["skills"][0]["enabled"])
        self.assertEqual(0, self.dispatch("enable", "skill:owner/repository"))
        self.assertEqual("# review\n", (folder / "SKILL.md").read_text())
        self.assertTrue(other.is_dir())

    def test_uninstall_disabled_skill_removes_owned_storage_and_receipt(self):
        self.create_skill()
        self.save()
        self.dispatch("adopt", "skill:owner/repository")
        self.dispatch("disable", "skill:owner/repository")
        self.dispatch("remove", "skill:owner/repository", uninstall=True)
        self.assertEqual([], list((self.home / "state/disabled-skills").rglob("SKILL.md")))
        self.assertEqual({}, json.loads((self.home / "state/component-receipts.json").read_text())["components"])

    def test_declared_disabled_skill_cannot_remain_active_silently(self):
        folder = self.create_skill()
        self.save()
        self.dispatch("adopt", "skill:owner/repository")
        self.manifest["skills"][0]["enabled"] = False
        skills.reconcile_skills(self.manifest, self.runner, deactivate=self.lifecycle.ensure_disabled)
        self.assertFalse(folder.exists())
        self.assertEqual([], self.runner.failures)

    def test_absent_disabled_mcp_is_healthy_but_matching_active_alias_is_not(self):
        from dotai_app import mcp
        self.manifest["mcp"]["servers"] = {"docs": {"type": "http", "url": "https://example.test/mcp", "enabled": False}}
        self.assertTrue(mcp.mcp_status(self.manifest)[0])
        Path(self.manifest["mcp"]["target"]).write_text(json.dumps({"mcpServers": {
            "alias": {"type": "http", "url": "https://example.test/mcp"},
        }}))
        self.assertFalse(mcp.mcp_status(self.manifest)[0])

    def test_wildcard_cwd_requirement_protects_shared_alias_in_targeted_mutation(self):
        self.create_mcp()
        target = Path(self.manifest["mcp"]["target"])
        config = json.loads(target.read_text())
        config["mcpServers"]["alias"]["cwd"] = "/workspace"
        target.write_text(json.dumps(config))
        self.manifest["mcp"]["servers"]["docs"]["cwd"] = "/workspace"
        self.manifest["mcp"]["servers"]["wildcard"] = {"type": "http", "url": "https://example.test/mcp"}
        self.save()
        self.dispatch("adopt", "mcp:docs")
        original_config, original_manifest = target.read_bytes(), self.path.read_bytes()
        selected = self.lifecycle.filter_manifest(self.manifest, ["mcp:docs"])
        for action in ("disable", "uninstall"):
            with self.subTest(action=action), self.assertRaises(runtime.DotAiError):
                self.lifecycle.mutate_runtime("mcp", "docs", selected["mcp"]["servers"]["docs"], selected, self.runner, action)
        self.assertEqual(original_config, target.read_bytes())
        self.assertEqual(original_manifest, self.path.read_bytes())
        self.assertEqual([], list(self.home.rglob("*.bak*")))

    def test_redirected_parent_refuses_fresh_mcp_enable_and_sync_without_side_effects(self):
        from dotai_app import mcp
        external = self.home / "external"
        external.mkdir()
        sentinel = external / "provider.json"
        sentinel.write_text('{"keep":"provider"}\n')
        redirected = self.home / "redirected"
        redirected.symlink_to(external, target_is_directory=True)
        target = redirected / "not-created" / "mcp.json"
        self.manifest["skills"] = []
        self.manifest["mcp"] = {"target": str(target), "servers": {
            "docs": {"type": "http", "url": "https://example.test/mcp", "enabled": False},
        }}
        self.save()
        original = self.path.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("enable", "mcp:docs")
        self.assertEqual(original, self.path.read_bytes())
        self.manifest["mcp"]["servers"]["docs"]["enabled"] = True
        with self.assertRaises(runtime.DotAiError):
            mcp.sync_mcp(self.manifest, self.runner, record_installed=self.lifecycle.record_install)
        self.assertEqual('{"keep":"provider"}\n', sentinel.read_text())
        self.assertFalse((external / "not-created").exists())
        self.assertFalse((self.home / "state/component-receipts.json").exists())
        self.assertEqual([], list(self.home.rglob("*.bak*")))

    def test_mcp_disable_preserves_alias_auth_and_unmanaged_configuration(self):
        original = self.create_mcp()
        self.save()
        self.dispatch("adopt", "mcp:docs")
        self.assertEqual(0, self.dispatch("disable", "mcp:docs"))
        actual = json.loads(Path(self.manifest["mcp"]["target"]).read_text())
        self.assertFalse(actual["mcpServers"]["alias"]["enabled"])
        self.assertEqual(original["mcpServers"]["alias"]["headers"], actual["mcpServers"]["alias"]["headers"])
        self.assertEqual(original["mcpServers"]["unmanaged"], actual["mcpServers"]["unmanaged"])
        self.assertEqual(original["keep"], actual["keep"])
        self.assertNotIn("docs", actual["mcpServers"])
        self.assertEqual(0, self.dispatch("enable", "mcp:docs"))
        self.assertTrue(json.loads(Path(self.manifest["mcp"]["target"]).read_text())["mcpServers"]["alias"]["enabled"])

    def test_mcp_uninstall_requires_adoption_and_removes_only_owned_alias(self):
        original = self.create_mcp()
        self.save()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "mcp:docs", uninstall=True)
        self.dispatch("adopt", "mcp:docs")
        self.assertEqual(0, self.dispatch("remove", "mcp:docs", uninstall=True))
        actual = json.loads(Path(self.manifest["mcp"]["target"]).read_text())
        self.assertEqual({"unmanaged": original["mcpServers"]["unmanaged"]}, actual["mcpServers"])
        self.assertEqual(original["keep"], actual["keep"])

    def test_sync_cannot_overwrite_unknown_mcp_alias_auth_fields(self):
        from dotai_app import mcp
        self.create_mcp()
        self.manifest["mcp"]["servers"]["docs"]["headers"] = {"Authorization": "DIFFERENT_AUTH_REFERENCE"}
        target = Path(self.manifest["mcp"]["target"])
        original = target.read_bytes()
        mcp.sync_mcp(self.manifest, self.runner, ownership_check=self.lifecycle.check_update_ownership)
        self.assertEqual(original, target.read_bytes())
        self.assertTrue(self.runner.failures)

    def test_sync_declared_disabled_mcp_applies_owned_alias_disable(self):
        from dotai_app import mcp
        self.create_mcp()
        self.save()
        self.dispatch("adopt", "mcp:docs")
        self.manifest["mcp"]["servers"]["docs"]["enabled"] = False
        mcp.sync_mcp(self.manifest, self.runner, deactivate=self.lifecycle.ensure_disabled)
        actual = json.loads(Path(self.manifest["mcp"]["target"]).read_text())
        self.assertFalse(actual["mcpServers"]["alias"]["enabled"])
        self.assertEqual([], self.runner.failures)

    def test_external_provider_adoption_cannot_grant_broad_write_permission(self):
        self.create_mcp()
        target = Path(self.manifest["mcp"]["target"])
        target.unlink()
        external = self.home / ".cursor/mcp.json"
        external.parent.mkdir()
        external.write_text(json.dumps({"mcpServers": {"external": {"type": "http", "url": "https://example.test/mcp"}}}))
        self.save()
        original = external.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("adopt", "mcp:docs")
        self.assertEqual(original, external.read_bytes())
        self.assertFalse(target.exists())

    def test_shared_equivalent_mcp_requirement_refuses_disabling_alias(self):
        self.create_mcp()
        self.manifest["mcp"]["servers"]["other-name"] = dict(self.manifest["mcp"]["servers"]["docs"])
        self.save()
        self.dispatch("adopt", "mcp:docs")
        original = Path(self.manifest["mcp"]["target"]).read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("disable", "mcp:docs")
        self.assertEqual(original, Path(self.manifest["mcp"]["target"]).read_bytes())

    def test_dry_run_adoption_and_removal_never_write_receipts_or_files(self):
        folder = self.create_skill()
        self.save()
        original = self.path.read_bytes()
        self.dispatch("adopt", "skill:owner/repository", dry_run=True)
        self.assertFalse((self.home / "state").exists())
        self.dispatch("remove", "skill:owner/repository", dry_run=True)
        self.assertEqual(original, self.path.read_bytes())
        self.assertTrue(folder.is_dir())

    def test_extension_disable_unregisters_only_owned_path(self):
        extension = self.home / "extension.ts"
        extension.write_text("export {};\n")
        self.manifest["skills"] = []
        self.manifest["ompExtensions"] = [str(extension)]
        self.extensions = [str(extension), "/unmanaged.ts"]
        self.save()
        self.dispatch("adopt", f"extension:{extension}")
        self.dispatch("disable", f"extension:{extension}")
        self.assertEqual([["omp", "config", "set", "extensions", '["/unmanaged.ts"]']], self.runner.commands)
        self.assertTrue(extension.is_file())
        self.assertFalse(json.loads(self.path.read_text())["ompExtensions"][0]["enabled"])

    def create_tool(self, name):
        if os.name == "nt":
            self.runner.platform = "windows"
            binary = self.home / (name + ".cmd")
            binary.write_bytes(b"@echo off\r\necho 1.2.3\r\n")
        else:
            binary = self.home / name
            binary.write_bytes(b"#!/bin/sh\nprintf '1.2.3\\n'\n")
            binary.chmod(0o755)
        return binary

    def point_tool_launcher(self, launcher, binary):
        if os.name == "nt":
            launcher.write_text(f'@echo off\ncall "{binary}" %*\n', encoding="utf-8", newline="\r\n")
            launcher.with_suffix(".shim").write_text(f'path = "{binary}"\n', encoding="utf-8")
        else:
            launcher.unlink(missing_ok=True)
            launcher.symlink_to(binary)

    def test_tool_uninstall_refuses_unsupported_operation_even_after_adoption(self):
        binary = self.create_tool("example")
        self.manifest["skills"] = []
        self.manifest["packages"] = [{"name": "Example", "managed": True, "check": [str(binary), "--version"], "install": []}]
        self.save()
        self.dispatch("adopt", "tool:Example")
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "tool:Example", uninstall=True)
        self.assertTrue(binary.is_file())
        self.assertEqual([], self.runner.commands)

    def test_adopted_tool_launcher_detects_retargeted_binary_before_uninstall(self):
        binary = self.create_tool("actual-example")
        launcher = self.home / ("example.cmd" if os.name == "nt" else "example")
        self.point_tool_launcher(launcher, binary)
        self.manifest["skills"] = []
        self.manifest["packages"] = [{"name": "Example", "managed": True, "check": [str(launcher), "--version"], "install": [], "uninstall": [["reviewed-uninstaller", "Example"]]}]
        self.save()
        self.assertEqual(0, self.dispatch("adopt", "tool:Example"))
        other = self.create_tool("unmanaged-binary")
        self.point_tool_launcher(launcher, other)
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "tool:Example", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(binary.is_file())
        self.assertTrue(other.is_file())

    def test_tool_uninstall_refuses_binary_shared_by_another_enabled_declaration(self):
        binary = self.create_tool("example")
        self.manifest["skills"] = []
        tool = {"name": "Example", "managed": True, "check": [str(binary), "--version"], "install": [], "uninstall": [["reviewed-uninstaller", "Example"]]}
        self.manifest["packages"] = [tool, {**tool, "name": "Other"}]
        self.save()
        self.dispatch("adopt", "tool:Example")
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "tool:Example", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(binary.is_file())

    def create_plugin(self):
        self.manifest["skills"] = []
        self.manifest["plugins"] = [{"id": "review@team", "scope": "user"}]
        folder = self.home / ".omp/plugins/cache/plugins/team___review___1.2.3"
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": "review-plugin", "version": "1.2.3"}))
        (folder / "index.ts").write_text("export {};\n")
        registry = self.home / ".omp/plugins/installed_plugins.json"
        registry.write_text(json.dumps({"version": 2, "plugins": {"review@team": [{
            "scope": "user", "installPath": str(folder), "version": "1.2.3",
            "installedAt": "2026-01-01", "lastUpdated": "2026-01-01",
        }]}}))
        return folder

    def test_adopted_plugin_disable_uses_exact_scoped_upstream_toggle(self):
        self.create_plugin()
        self.save()
        self.dispatch("adopt", "plugin:review@team")
        self.dispatch("disable", "plugin:review@team")
        self.assertEqual([["omp", "plugin", "disable", "--scope", "user", "review@team"]], self.runner.commands)
        self.assertFalse(json.loads(self.path.read_text())["plugins"][0]["enabled"])

    def test_update_keeps_healthy_pinned_plugin_at_observed_exact_version(self):
        folder = self.create_plugin()
        self.manifest["plugins"][0].update({"version": "1.2.3", "updatePolicy": "pinned"})
        before = (folder / "package.json").read_bytes()
        omp.reconcile_plugins(self.manifest, self.runner, "update")
        self.assertEqual([], self.runner.failures)
        self.assertEqual([], self.runner.commands)
        self.assertEqual(before, (folder / "package.json").read_bytes())

    def test_fresh_user_plugin_cannot_overwrite_other_scope_cache(self):
        folder = self.create_plugin()
        self.save()
        user_registry = self.home / ".omp/plugins/installed_plugins.json"
        data = json.loads(user_registry.read_text())
        user_registry.unlink()
        data["plugins"]["review@team"][0]["scope"] = "project"
        invocation = self.home / "invocation"
        project_registry = invocation / ".omp/plugins/installed_plugins.json"
        project_registry.parent.mkdir(parents=True)
        project_registry.write_text(json.dumps(data))
        with patch.object(Path, "cwd", return_value=invocation):
            omp.reconcile_plugins(self.manifest, self.runner, "sync")
        self.assertEqual([], self.runner.commands)
        self.assertTrue(self.runner.failures)
        self.assertTrue(folder.is_dir())

    def test_modified_adopted_plugin_refuses_uninstall_before_upstream_command(self):
        folder = self.create_plugin()
        self.save()
        self.dispatch("adopt", "plugin:review@team")
        (folder / "index.ts").write_text("export const changed = true;\n")
        original = self.path.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "plugin:review@team", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertEqual(original, self.path.read_bytes())
        self.assertTrue(folder.is_dir())

    def test_retargeted_plugin_runtime_link_refuses_upstream_uninstall(self):
        folder = self.create_plugin()
        self.save()
        runtime_link = self.home / ".omp/plugins/node_modules/review-plugin"
        runtime_link.parent.mkdir(parents=True)
        runtime_link.symlink_to(folder, target_is_directory=True)
        self.dispatch("adopt", "plugin:review@team")
        runtime_link.unlink()
        unrelated = self.home / "unmanaged-plugin"
        unrelated.mkdir()
        runtime_link.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "plugin:review@team", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(folder.is_dir())
        self.assertTrue(runtime_link.samefile(unrelated))

    def test_shared_plugin_cache_refuses_uninstall_even_after_adoption(self):
        folder = self.create_plugin()
        self.save()
        self.dispatch("adopt", "plugin:review@team")
        # Use a separate invocation directory; never write checkout registry state.
        invocation = self.home / "invocation"
        project = invocation / ".omp/plugins/installed_plugins.json"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"version": 2, "plugins": {"review@team": [{
            "scope": "project", "installPath": str(folder), "version": "1.2.3",
            "installedAt": "2026-01-01", "lastUpdated": "2026-01-01",
        }]}}))
        with patch.object(Path, "cwd", return_value=invocation), self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "plugin:review@team", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(folder.exists())

    def test_marketplace_uninstall_refuses_installed_plugin_dependency(self):
        self.create_plugin()
        catalog = self.home / ".omp/plugins/cache/marketplaces/team/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"name": "team", "owner": {"name": "Team"}, "plugins": []}))
        registry = self.home / ".omp/marketplaces.json"
        registry.write_text(json.dumps({"version": 1, "marketplaces": [{
            "name": "team", "sourceType": "github", "sourceUri": "owner/marketplace",
            "catalogPath": str(catalog), "addedAt": "2026-01-01", "updatedAt": "2026-01-01",
        }]}))
        self.manifest["marketplaces"] = [{"name": "team", "source": "owner/marketplace"}]
        self.save()
        self.dispatch("adopt", "marketplace:team")
        with self.assertRaises(runtime.DotAiError):
            self.dispatch("remove", "marketplace:team", uninstall=True)
        self.assertEqual([], self.runner.commands)
        self.assertTrue(catalog.is_file())


    def test_list_and_show_explain_desired_observed_ownership_scope_and_source(self):
        self.create_skill()
        self.save()
        with redirect_stdout(io.StringIO()) as out:
            self.dispatch("show", "skill:owner/repository")
        prose = out.getvalue().lower()
        for field in ("desired", "observed", "ownership", "universal", "owner/repository"):
            self.assertIn(field, prose)
        self.assertNotIn('\"checkskills\"', prose)

@unittest.skipIf(os.name == "nt", "POSIX independent executable fixtures")
class ToolMutationOwnershipTests(unittest.TestCase):
    def setUp(self):
        from dotai_app import lifecycle, packages
        self.lifecycle, self.packages = lifecycle, packages
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.runner = runtime.Runner("macos")
        self.binary = self.home / "independent-tool"
        self.binary.write_text("#!/bin/sh\nprintf 'tool 1.0.0\\n'\n")
        self.binary.chmod(0o755)
        self.marker = self.home / "mutated"
        self.tool = {"name": "fixture", "managed": True, "check": [str(self.binary), "--version"],
                     "install": [[sys.executable, "-c", f"from pathlib import Path; Path({str(self.marker)!r}).write_text('install')"]],
                     "update": [[sys.executable, "-c", f"from pathlib import Path; Path({str(self.marker)!r}).write_text('update')"]]}
        self.manifest = {"version": 2, "prerequisites": [], "packages": [self.tool], "skills": [],
                         "plugins": [], "marketplaces": [], "ompExtensions": [],
                         "mcp": {"target": str(self.home / "mcp.json"), "servers": {}}}

    def reconcile(self, mode="update", force=False, *, callback=True):
        def check(value, current, runner, operation):
            self.lifecycle.check_update_ownership("tool", value["name"], value, current, runner, operation=operation)
        self.packages.reconcile_packages(self.manifest, self.runner, mode, force,
                                         ownership_check=check if callback else None)

    def adopt(self):
        self.lifecycle.record_install("tool", "fixture", self.tool, self.manifest, self.runner)

    def test_unowned_existing_update_is_refused_before_updater(self):
        self.reconcile()
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.runner.failures)

    def test_absent_callback_cannot_authorize_existing_force_replacement(self):
        self.reconcile("install", force=True, callback=False)
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.runner.failures)

    def test_failed_check_existing_independent_binary_cannot_be_overwritten(self):
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        previous = self.binary.read_bytes()
        self.tool["install"] = [[sys.executable, "-c",
                                f"from pathlib import Path; Path({str(self.binary)!r}).write_text('overwritten')"]]
        for callback in (True, False):
            with self.subTest(callback=callback):
                self.runner = runtime.Runner("macos")
                self.reconcile("install", callback=callback)
                self.assertEqual(previous, self.binary.read_bytes())
                self.assertTrue(self.runner.failures)

    def test_failed_check_modified_receipt_cannot_authorize_replacement(self):
        self.adopt()
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        previous = self.binary.read_bytes()
        self.reconcile("install", force=True)
        self.assertEqual(previous, self.binary.read_bytes())
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.runner.failures)

    def uv_tool(self):
        from dotai_app import catalog
        recipes = catalog.load_catalog()
        recipes["recipes"]["ruff"] = copy.deepcopy(recipes["recipes"]["graphify"])
        recipes["recipes"]["ruff"]["footprint"] = {"default": {"kind": "uv", "package": "ruff", "launcher": "ruff"}}
        definition = patch.object(catalog, "load_catalog", return_value=recipes)
        definition.start()
        self.addCleanup(definition.stop)
        root = self.home / "uv-tools/ruff"
        backend = root / "bin/ruff"
        backend.parent.mkdir(parents=True)
        backend.write_bytes(self.binary.read_bytes())
        backend.chmod(0o755)
        launcher = self.home / "uv-bin/ruff"
        launcher.parent.mkdir()
        launcher.symlink_to(backend)
        self.runner.env.update({"UV_TOOL_DIR": str(root.parent), "UV_TOOL_BIN_DIR": str(launcher.parent)})
        self.tool.update({"recipe": "ruff", "check": [str(launcher), "--version"]})
        self.tool.pop("install")
        self.tool.pop("update")
        return root

    def test_generated_uv_removal_refuses_user_added_backend_file(self):
        root = self.uv_tool()
        self.adopt()
        note = root / "personal-notes.txt"
        note.write_text("Do not remove my notes")
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertEqual("Do not remove my notes", note.read_text())
        self.assertTrue(self.lifecycle.read_receipts())

    def test_generated_uv_update_refuses_retargeted_environment_link(self):
        root = self.uv_tool()
        external = self.home / "external-python"
        external.write_text("outside environment")
        link = root / "bin/python"
        link.symlink_to(external)
        self.adopt()
        link.unlink()
        other = self.home / "different-python"
        other.write_text("different external content")
        link.symlink_to(other)
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.check_update_ownership("tool", "fixture", self.tool, self.manifest, self.runner)
        self.assertEqual("outside environment", external.read_text())
        self.assertEqual("different external content", other.read_text())

    def test_backend_hash_ignores_bytecode_and_does_not_follow_dependency_links(self):
        root = self.uv_tool()
        cache = root / "__pycache__"
        bytecode = cache / "generated.pyc"
        external = self.home / "external-python"
        external.write_text("outside environment")
        (root / "bin/python").symlink_to(external)
        self.adopt()
        cache.mkdir()
        bytecode.write_bytes(b"regenerated cache")
        external.write_text("updated system interpreter")
        self.lifecycle.check_update_ownership("tool", "fixture", self.tool, self.manifest, self.runner)
        self.assertEqual([], self.runner.failures)

    def test_backend_removal_refuses_another_declared_binary_inside_environment(self):
        root = self.uv_tool()
        sibling = root / "bin/other-cli"
        sibling.write_bytes(self.binary.read_bytes())
        sibling.chmod(0o755)
        self.manifest["packages"].append({"name": "other", "managed": True,
                                          "check": [str(sibling), "--version"], "install": []})
        self.adopt()
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertTrue(sibling.exists())

    def test_version_mismatch_still_requires_ownership_before_install(self):
        self.tool["version"] = "2.0.0"
        self.tool["update"] = []
        self.reconcile("install")
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.runner.failures)

    def test_locally_modified_owned_binary_refuses_update(self):
        self.adopt()
        self.binary.write_text("#!/bin/sh\nprintf 'tool 1.0.0 locally modified\\n'\n")
        self.reconcile()
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.runner.failures)

    def test_owned_independent_binary_update_succeeds(self):
        self.adopt()
        self.reconcile()
        self.assertEqual("update", self.marker.read_text())
        self.assertEqual([], self.runner.failures)

    def test_missing_independent_tool_installs_without_ownership_callback(self):
        source = self.binary.read_text()
        self.binary.unlink()
        self.tool["install"] = [[sys.executable, "-c",
                                f"from pathlib import Path; p=Path({str(self.binary)!r}); p.write_text({source!r}); p.chmod(0o755)"]]
        self.reconcile("install", callback=False)
        self.assertTrue(self.runner.succeeds(self.tool["check"]))
        self.assertEqual([], self.runner.failures)

    def test_real_custom_uninstall_retires_receipt_only_after_owned_binary_removed(self):
        self.tool["uninstall"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(self.binary)!r}).unlink()"]]
        self.adopt()
        self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertFalse(self.binary.exists())
        self.assertEqual({}, self.lifecycle.read_receipts())

    def test_noop_uninstall_retains_receipt_and_declaration(self):
        self.tool["uninstall"] = [[sys.executable, "-c", "pass"]]
        self.adopt()
        receipts = self.lifecycle.receipt_path().read_bytes()
        path = self.home / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        declaration = path.read_bytes()
        arguments = argparse.Namespace(command="remove", selector="tool:fixture", uninstall=True)
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.dispatch(arguments, self.manifest, path, self.runner)
        self.assertEqual(receipts, self.lifecycle.receipt_path().read_bytes())
        self.assertEqual(declaration, path.read_bytes())
        self.assertTrue(self.binary.exists())

    def test_noop_disable_retains_enabled_declaration_and_receipt(self):
        self.tool["uninstall"] = [[sys.executable, "-c", "pass"]]
        self.adopt()
        receipts = self.lifecycle.receipt_path().read_bytes()
        path = self.home / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        declaration = path.read_bytes()
        arguments = argparse.Namespace(command="disable", selector="tool:fixture")
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.dispatch(arguments, self.manifest, path, self.runner)
        self.assertEqual(receipts, self.lifecycle.receipt_path().read_bytes())
        self.assertEqual(declaration, path.read_bytes())

    def test_failed_check_does_not_retire_receipt_while_owned_binary_remains(self):
        self.adopt()
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        receipts = self.lifecycle.receipt_path().read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertEqual(receipts, self.lifecycle.receipt_path().read_bytes())
        self.assertTrue(self.binary.exists())

    def test_owned_foreign_recipe_check_cannot_remove_local_recipe_binary(self):
        local = self.home / ".local/bin/rtk"
        local.parent.mkdir(parents=True)
        local.write_bytes(self.binary.read_bytes())
        local.chmod(0o755)
        self.tool.update({"recipe": "rtk", "check": [str(self.binary), "--version"]})
        self.tool.pop("install")
        self.tool.pop("update")
        self.adopt()
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.check_update_ownership("tool", "fixture", self.tool, self.manifest,
                                                  self.runner, operation="install")
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertTrue(local.exists())
        self.assertTrue(self.binary.exists())
        self.assertTrue(self.lifecycle.read_receipts())

    def test_failed_unowned_check_cannot_silently_forget_explicit_uninstall(self):
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        path = self.home / "stack.json"
        path.write_text(json.dumps(self.manifest))
        before = path.read_bytes()
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.dispatch(argparse.Namespace(command="remove", selector="tool:fixture", uninstall=True),
                                    self.manifest, path, self.runner)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.binary.exists())

    def test_user_file_inside_generated_cache_prevents_backend_uninstall(self):
        root = self.uv_tool()
        self.adopt()
        cache = root / "__pycache__"
        cache.mkdir()
        note = cache / "personal-notes.txt"
        note.write_text("user content")
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertEqual(note.read_text(), "user content")

    def test_cache_link_without_symlink_flag_cannot_hide_retargeted_backend(self):
        root = self.uv_tool()
        first = self.home / "external-cache"
        second = self.home / "replacement-cache"
        for folder in (first, second):
            folder.mkdir()
            (folder / "notes").write_text("unmanaged content")
        cache = root / "__pycache__"
        cache.symlink_to(first, target_is_directory=True)
        is_symlink = Path.is_symlink
        # Windows junctions are directory redirects without a symlink flag.
        with patch.object(Path, "is_symlink", lambda path: False if path == cache else is_symlink(path)):
            self.adopt()
            cache.unlink()
            cache.symlink_to(second, target_is_directory=True)
            with self.assertRaises(runtime.DotAiError):
                self.lifecycle.check_update_ownership("tool", "fixture", self.tool, self.manifest, self.runner)
        self.assertEqual((first / "notes").read_text(), "unmanaged content")
        self.assertEqual((second / "notes").read_text(), "unmanaged content")

    def test_fresh_reviewed_scope_install_preserves_foreign_existing_program(self):
        from dotai_app import catalog
        foreign = self.home / "foreign-bin/dotai-scoped-helper"
        foreign.parent.mkdir()
        foreign.write_bytes(self.binary.read_bytes())
        foreign.chmod(0o755)
        before = foreign.read_bytes()
        installed = self.home / ".local/bin/dotai-scoped-helper"
        payload = "#!/bin/sh\nprintf 'helper 2.0.0\\n'\n"
        definition = {"check": ["dotai-scoped-helper", "--version"],
                      "install": [[sys.executable, "-c", f"from pathlib import Path; p=Path({str(installed)!r}); p.parent.mkdir(parents=True); p.write_text({payload!r}); p.chmod(0o755)"]],
                      "uninstall": [[sys.executable, "-c", f"from pathlib import Path; Path({str(installed)!r}).unlink()"]],
                      "footprint": {"default": {"kind": "standalone", "launcher": ".local/bin/dotai-scoped-helper"}}}
        self.tool = {"name": "fixture", "managed": True, "recipe": "isolated"}
        self.manifest["packages"] = [self.tool]
        self.runner.env["PATH"] = str(installed.parent) + os.pathsep + str(foreign.parent) + os.pathsep + self.runner.env["PATH"]
        with patch.object(catalog, "load_catalog", return_value={"recipes": {"isolated": definition}, "prerequisites": {}}):
            effective = catalog.materialize(self.manifest)
            self.packages.reconcile_packages(effective, self.runner, "install", force=True,
                                            ownership_check=lambda value, current, actor, operation: self.lifecycle.check_update_ownership("tool", "fixture", self.tool, self.manifest, actor, operation=operation),
                                            on_installed=lambda value, current, actor: self.lifecycle.record_install("tool", "fixture", self.tool, self.manifest, actor))
            self.assertEqual(self.runner.output([str(installed), "--version"]), "helper 2.0.0")
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertEqual(foreign.read_bytes(), before)
        self.assertFalse(installed.exists())
        self.assertTrue(self.packages.package_version_check({"version": "1.0.0"}, self.runner, ["dotai-scoped-helper", "--version"])[0])
        self.assertEqual(self.lifecycle.read_receipts(), {})
        self.assertEqual(self.runner.failures, [])

    def test_custom_installer_does_not_grant_generated_backend_removal_permission(self):
        root = self.uv_tool()
        note = root / "personal-notes.txt"
        note.write_text("preexisting user content")
        self.tool["install"] = []
        self.adopt()
        with self.assertRaises(runtime.DotAiError):
            self.lifecycle.mutate_runtime("tool", "fixture", self.tool, self.manifest, self.runner, "uninstall")
        self.assertEqual(note.read_text(), "preexisting user content")

    def test_tool_inventory_reports_actual_version_separately_from_requested_pin(self):
        self.tool["version"] = "2.0.0"
        with redirect_stdout(io.StringIO()) as captured:
            self.lifecycle.inventory("tool", "fixture", self.tool, self.manifest, self.runner)
        self.assertIn("1.0.0", captured.getvalue())
        self.assertIn("2.0.0", captured.getvalue())


@unittest.skipUnless(os.name == "nt", "Native Windows directory junctions")
class WindowsBackendOwnershipTests(unittest.TestCase):
    def test_backend_hash_does_not_traverse_current_junction_or_external_dependency(self):
        from dotai_app import lifecycle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "app"
            version = root / "1.0.0"
            version.mkdir(parents=True)
            binary = version / "app.exe"
            binary.write_bytes(b"managed application")
            outside = root.parent / "external"
            outside.mkdir()
            dependency = outside / "runtime"
            dependency.write_bytes(b"external runtime")
            cache = version / "__pycache__"
            cache.mkdir()
            for alias, target in ((root / "current", version), (version / "dependency", outside),
                                  (root / "__pycache__", outside), (cache / "runtime.pyc", outside)):
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            before = lifecycle.tool_backend_fingerprint(root)
            dependency.write_bytes(b"user-updated external runtime")
            self.assertEqual(lifecycle.tool_backend_fingerprint(root), before)
            binary.write_bytes(b"modified application")
            self.assertNotEqual(lifecycle.tool_backend_fingerprint(root), before)


if __name__ == "__main__":
    unittest.main()
