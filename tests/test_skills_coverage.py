from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import cli, manifest as manifests, recommendations, releases, runtime, skills, state, terminal


# External installer boundary: listing reflects actual files; removal changes them.
INSTALLER = '''import json, os, pathlib, shutil, sys
home = pathlib.Path(os.environ["DOTAI_HOME"])
control = home / "installer.json"
data = json.loads(control.read_text())
args = sys.argv[1:]
action = args[2]
if action == "list":
    data["lists"] = data.get("lists", 0) + 1
    control.write_text(json.dumps(data))
    malformed = data.get("malformed")
    if malformed == "initial" or (malformed == "verification" and data["lists"] > 1):
        print('{"unexpected": true}')
    else:
        print(json.dumps([entry for entry in data["entries"] if pathlib.Path(entry["path"]).exists()]))
elif action == "remove":
    if data.get("remove_fail"):
        sys.exit(7)
    agent = args[args.index("--agent") + 1] if "--agent" in args else "universal"
    names = args[3:args.index("--global")]
    for entry in data["entries"]:
        if entry["name"] in names and entry["agents"] == [agent]:
            shutil.rmtree(entry["path"])
elif action == "add":
    if data.get("add_fail"):
        sys.exit(9)
    (home / "fetched.json").write_text(json.dumps(args))
else:
    sys.exit(11)
'''


def manifest(entries):
    return {"version": 1, "packages": [], "skills": entries, "marketplaces": [],
            "plugins": [], "ompExtensions": [], "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}}}


def source(names, *, agent="universal", checks=None):
    entry = {"source": "owner/repo", "agent": agent, "skills": names}
    if checks is not None:
        entry["checkSkills"] = checks
    return entry


class SkillCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.path = self.home / "stack.json"
        self.example = self.home / "example.json"
        self.history = self.home / "state" / "recommended-skills.json"
        env = {"HOME": str(self.home), "DOTAI_HOME": str(self.home),
               "DOTAI_STATE_DIR": str(self.home / "state"), "XDG_STATE_HOME": str(self.home / "xdg"),
               "GH_HOST": "github.com", "NO_COLOR": "1", "DOTAI_PLATFORM": "linux"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.example)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(releases, "latest_release_version", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        patch.start()
        self.addCleanup(patch.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        binary = self.home / ".local" / "bin" / "npx"
        binary.parent.mkdir(parents=True)
        binary.write_text(f"#!{sys.executable}\n" + INSTALLER, encoding="utf-8")
        binary.chmod(0o755)
        real_run = subprocess.run
        def external_boundary(command, **kwargs):
            if isinstance(command, list) and command[0] == "npx":
                command = [sys.executable, str(binary), *command[1:]]
            return real_run(command, **kwargs)
        patch = mock.patch.object(subprocess, "run", side_effect=external_boundary)
        patch.start()
        self.addCleanup(patch.stop)
        self.control = {"entries": []}
        self.configure()

    def configure(self, **options):
        self.control.update(options)
        (self.home / "installer.json").write_text(json.dumps(self.control), encoding="utf-8")

    def installed(self, name, *, repo="owner/repo", agent="universal"):
        folder = (self.home / ".agents" / "skills" if agent == "universal" else self.home / ".pi" / "agent" / "skills") / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("fixture skill\n", encoding="utf-8")
        self.control["entries"].append({"name": name, "source": repo, "path": str(folder), "agents": [agent]})
        self.configure()
        return folder

    def prepare(self, before, desired, baseline=None):
        original = manifest(before)
        self.path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        self.example.write_text(json.dumps(manifest(desired)), encoding="utf-8")
        if baseline is not None:
            state.save_managed_recommendations(self.path, baseline)
        return original

    def review(self, original, answer="a", enforce=False):
        runner = runtime.Runner("linux")
        with mock.patch("builtins.input", return_value=answer):
            updated, baseline = recommendations.review_recommended_skills(original, self.path, runner, enforce)
        return updated, baseline, runner

    def test_normalized_named_retirement_removes_only_selected_agent_and_source(self):
        before = source(["ALPHA", "..Review / Code!!.."])
        removed = [self.installed("alpha"), self.installed("review-code")]
        survivors = [self.installed("other", repo="custom/repo"), self.installed("alpha", agent="pi"), self.installed("unselected")]
        original = self.prepare([before], [], [before])
        updated, baseline, runner = self.review(original)
        self.assertEqual(updated["skills"], [])
        self.assertEqual(baseline, [])
        self.assertEqual(runner.failures, [])
        self.assertTrue(all(not folder.exists() for folder in removed))
        self.assertTrue(all((folder / "SKILL.md").is_file() for folder in survivors))

    def test_wildcard_narrowing_retains_normalized_selection(self):
        before, after = source(["*"]), source(["ALPHA", "Review / Code"])
        kept = [self.installed("alpha"), self.installed("review-code")]
        retired = self.installed("retired")
        original = self.prepare([before], [after], [before])
        updated, baseline, runner = self.review(original)
        self.assertEqual((updated["skills"], baseline, runner.failures), ([after], [after], []))
        self.assertTrue(all(folder.exists() for folder in kept))
        self.assertFalse(retired.exists())

    def test_explicit_check_names_are_retirement_authority_without_normalization(self):
        before = source(["ALPHA"], checks=["External.Name"])
        retired = self.installed("External.Name")
        unrelated = self.installed("alpha")
        original = self.prepare([before], [], [before])
        _, _, runner = self.review(original)
        self.assertEqual(runner.failures, [])
        self.assertFalse(retired.exists())
        self.assertTrue(unrelated.exists())

    def test_wildcard_narrowing_preserves_explicit_check_directory(self):
        before, after = source(["*"]), source(["ALPHA"], checks=["External.Name"])
        kept, retired = self.installed("External.Name"), self.installed("alpha")
        original = self.prepare([before], [after], [before])
        self.review(original)
        self.assertTrue(kept.exists())
        self.assertFalse(retired.exists())

    def test_failed_retirements_preserve_manifest_history_and_pending_change(self):
        for failure in ("initial", "verification", "command"):
            with self.subTest(failure=failure):
                self.configure(lists=0, malformed=failure if failure != "command" else None, remove_fail=failure == "command")
                before = source(["old"])
                folder = self.home / ".agents" / "skills" / "old"
                if not folder.exists():
                    self.installed("old")
                original = self.prepare([before], [], [before])
                original_bytes, history_bytes = self.path.read_bytes(), self.history.read_bytes()
                updated, baseline, runner = self.review(original)
                self.assertTrue(runner.failures)
                self.assertEqual((updated, baseline), (original, [before]))
                self.assertEqual(self.path.read_bytes(), original_bytes)
                self.assertEqual(self.history.read_bytes(), history_bytes)
                _, pending, _ = recommendations.recommended_skill_plan(updated, self.path)
                self.assertEqual([(item["kind"], item["source"]) for item in pending], [("remove", "owner/repo")])
                if failure != "verification":
                    self.assertTrue(folder.exists())

    def test_locally_modified_owned_source_is_preserved_when_upstream_changes_or_removes(self):
        before, local, after = source(["alpha"]), source(["personal"]), source(["new"])
        folder = self.installed("personal")
        for desired in ([after], []):
            with self.subTest(desired=desired):
                original = self.prepare([local], desired, [before])
                original_bytes, history_bytes = self.path.read_bytes(), self.history.read_bytes()
                updated, baseline, runner = self.review(original)
                self.assertEqual((updated, baseline, runner.failures), (original, [before], []))
                self.assertEqual(self.path.read_bytes(), original_bytes)
                self.assertEqual(self.history.read_bytes(), history_bytes)
                self.assertTrue(folder.exists())
                _, changes, conflicts = recommendations.recommended_skill_plan(original, self.path)
                self.assertEqual((changes, conflicts), ([], ["owner/repo"]))
                # Enforce proposes the conflict, but never applies without acceptance.
                _, changes, conflicts = recommendations.recommended_skill_plan(original, self.path, True)
                self.assertEqual([item["kind"] for item in changes], ["update" if desired else "remove"])
                self.review(original, answer="n", enforce=True)
                self.assertEqual(self.path.read_bytes(), original_bytes)
                self.assertEqual(self.history.read_bytes(), history_bytes)
                self.assertTrue(folder.exists())

    def test_unknown_source_without_ownership_history_cannot_be_retired(self):
        local = source(["personal"])
        folder = self.installed("personal")
        original = self.prepare([local], [])
        updated, baseline, runner = self.review(original, enforce=True)
        self.assertEqual((updated, baseline, runner.failures), (original, [], []))
        self.assertTrue(folder.exists())
        self.assertFalse(self.history.exists())

    def test_failed_accepted_install_retains_accepted_configuration_for_retry(self):
        added = source(["alpha"], checks=["alpha"])
        original = self.prepare([], [added], [])
        self.configure(add_fail=True)
        with mock.patch("builtins.input", return_value="a"):
            result = cli.main(["--manifest", str(self.path), "sync", "--recommended-skills"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(self.path.read_text())["skills"], [added])
        self.assertEqual(state.managed_recommendations(self.path), [added])
        self.assertFalse((self.home / "state" / "state.json").exists())
        self.assertFalse((self.home / ".agents" / "skills" / "alpha").exists())

    def test_failed_accepted_migration_retains_saved_target_and_backup_for_retry(self):
        before = source(["alpha"], agent="pi", checks=["alpha"])
        old_files = self.installed("alpha", agent="pi")
        self.prepare([before], [])
        original_bytes = self.path.read_bytes()
        self.configure(add_fail=True)
        with mock.patch("builtins.input", return_value="y"):
            result = cli.main(["--manifest", str(self.path), "fix"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(self.path.read_text())["skills"], [{**before, "agent": "universal"}])
        backups = list(self.home.glob("stack.json.bak.*"))
        self.assertEqual([path.read_bytes() for path in backups], [original_bytes])
        self.assertTrue(old_files.exists())
        self.assertFalse((self.home / ".agents" / "skills" / "alpha").exists())

    def owned_tree(self):
        folder = self.installed("alpha")
        (folder / "run.sh").write_bytes(b"#!/bin/sh\nprintf fixture\\n\n")
        (folder / "run.sh").chmod(0o755)
        (folder / "nested").mkdir()
        (folder / "nested" / "data.txt").write_bytes(b"nested content\n")
        # Git itself supplies an independent expected tree, not the production helper.
        git_dir = self.home / "git-fixture"
        subprocess.run(["git", "init", "--quiet", "--object-format=sha1", str(git_dir)], check=True, capture_output=True)
        shutil.copytree(folder, git_dir, dirs_exist_ok=True)
        subprocess.run(["git", "-C", str(git_dir), "-c", "core.filemode=true", "-c", "core.autocrlf=false", "add", "--all"], check=True, capture_output=True)
        digest = subprocess.run(["git", "-C", str(git_dir), "write-tree"], check=True, text=True, capture_output=True).stdout.strip()
        entry = {"source": "owner/repo", "sourceType": "github", "sourceUrl": "https://github.com/owner/repo.git",
                 "ref": None, "skillFolderHash": digest}
        lock = {"version": 3, "skills": {"alpha": entry}}
        lock_path = self.home / "xdg" / "skills" / ".skill-lock.json"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        return folder, lock_path, lock

    def refresh(self, selected=None):
        runner = runtime.Runner("linux")
        skills.reconcile_skills(manifest([selected or source(["ALPHA"], checks=["alpha"])]), runner)
        self.assertEqual(runner.failures, [])
        return (self.home / "fetched.json").exists()

    @unittest.skipIf(os.name == "nt", "POSIX executable mode contract")
    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_executable_nested_tree_skips_refresh_until_mode_changes(self):
        folder, _, _ = self.owned_tree()
        self.assertFalse(self.refresh())
        (folder / "run.sh").chmod(0o644)
        self.assertTrue(self.refresh())

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_symlink_tree_conservatively_refreshes(self):
        folder, _, _ = self.owned_tree()
        link = folder / "linked"
        link.symlink_to(folder / "SKILL.md")
        self.assertTrue(self.refresh())

    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_unreadable_tree_conservatively_refreshes(self):
        folder, _, _ = self.owned_tree()
        original = Path.read_bytes
        def unreadable(path):
            if path == folder / "nested" / "data.txt":
                raise PermissionError("deterministic fixture read failure")
            return original(path)
        with mock.patch.object(Path, "read_bytes", unreadable):
            self.assertTrue(self.refresh())

    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_missing_or_malformed_xdg_lock_never_borrows_valid_home_lock(self):
        _, lock_path, lock = self.owned_tree()
        (self.home / ".agents" / ".skill-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        lock_path.unlink()
        self.assertTrue(self.refresh())
        (self.home / "fetched.json").unlink()
        lock_path.write_text("{invalid", encoding="utf-8")
        self.assertTrue(self.refresh())

    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_enterprise_host_requires_explicit_public_source(self):
        self.owned_tree()
        with mock.patch.dict(os.environ, {"GH_HOST": "github.enterprise.invalid"}):
            self.assertTrue(self.refresh())
            (self.home / "fetched.json").unlink()
            explicit = source(["ALPHA"], checks=["alpha"])
            explicit["source"] = "https://github.com/owner/repo"
            self.assertFalse(self.refresh(explicit))

    @unittest.skipUnless(shutil.which("git"), "Git independently verifies fixture trees")
    def test_wrong_source_url_and_one_invalid_member_refresh_entire_source(self):
        _, lock_path, lock = self.owned_tree()
        lock["skills"]["alpha"]["sourceUrl"] = "https://github.com/foreign/repo"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.assertTrue(self.refresh())
        (self.home / "fetched.json").unlink()
        lock["skills"]["alpha"]["sourceUrl"] = "https://github.com/owner/repo"
        self.installed("beta")
        lock["skills"]["beta"] = {**lock["skills"]["alpha"], "skillFolderHash": "not-a-hash"}
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.assertTrue(self.refresh(source(["alpha", "beta"], checks=["alpha", "beta"])))


if __name__ == "__main__":
    unittest.main()
