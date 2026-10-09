"""Regression boundaries for conservative synchronization and component lifecycle."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
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

    def test_sync_does_not_force_install_healthy_plugins(self):
        registry = self.home / ".omp/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"plugins": {"review@team": [{"scope": "user", "version": "1.2.3"}]}}))
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "sync")
        self.assertEqual([], runner.commands)

    def test_missing_plugin_install_does_not_force(self):
        runner = RecordingRunner()
        omp.reconcile_plugins({"marketplaces": [], "plugins": [{"id": "review@team", "scope": "user"}]}, runner, "sync")
        self.assertEqual([["omp", "plugin", "install", "--scope", "user", "review@team"]], runner.commands)

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


if __name__ == "__main__":
    unittest.main()
