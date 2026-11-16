from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import manifest as manifests, omp, runtime, skills


BEGIN = b"# BEGIN DotAi skill environment\n"
END = b"# END DotAi skill environment\n"


class SkillEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.target = self.home / ".omp" / "agent" / ".env"
        patch = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "DOTAI_HOME": str(self.home),
            "DOTAI_STATE_DIR": str(self.home / "state"),
            "XDG_STATE_HOME": str(self.home / "xdg"), "GH_HOST": "github.com",
        })
        patch.start()
        self.addCleanup(patch.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def write_env(self, content):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(content)

    def selected(self, source="owner/privacy", environment=None):
        value = {"source": source, "agent": "universal", "skills": ["alpha"], "checkSkills": ["alpha"]}
        if environment is not None:
            value["ompEnvironment"] = environment
        return value

    def reconcile(self, entries, runner=None, **kwargs):
        runner = runner or runtime.Runner("linux")
        skills.reconcile_skills({"skills": entries}, runner, **kwargs)
        return runner

    def installer(self, selected):
        # A real isolated child observes both the effective installer env and file ordering.
        script = (
            "import json, os, pathlib, sys; "
            "home=pathlib.Path(os.environ['DOTAI_HOME']); "
            "target=home/'.omp'/'agent'/'.env'; "
            "record={'source':sys.argv[1], 'privacy':os.environ.get('DISABLE_TELEMETRY'), "
            "'other':os.environ.get('DOTAI_TEST_SETTING'), "
            "'dotenv':target.read_text() if target.exists() else None}; "
            "out=home/'children.jsonl'; "
            "out.open('a', encoding='utf-8').write(json.dumps(record)+'\\n')"
        )
        return [sys.executable, "-c", script, selected["source"]]

    def children(self):
        path = self.home / "children.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_creates_private_persistent_defaults_without_changing_parent_environment(self):
        runner = runtime.Runner("linux")
        original = dict(os.environ)
        self.assertTrue(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertEqual(self.target.read_bytes(), BEGIN + b"DISABLE_TELEMETRY=1\n" + END)
        self.assertEqual(os.environ, original)
        self.assertEqual(runner.failures, [])
        self.assertEqual(list(self.target.parent.glob(".env.bak.*")), [])
        if os.name != "nt":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_preserves_unmanaged_multiline_and_non_utf8_bytes_with_private_backup(self):
        original = b'# unrelated\r\nTOKEN="first\r\nsecond"\r\nRAW=\xff\r\nNO_NEWLINE=kept'
        self.write_env(original)
        runner = runtime.Runner("linux")
        self.assertTrue(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertEqual(self.target.read_bytes(), original + b"\n" + BEGIN + b"DISABLE_TELEMETRY=1\n" + END)
        backups = list(self.target.parent.glob(".env.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_updates_only_terminal_owned_block_and_is_idempotent(self):
        prefix = b"USER_SETTING=kept\r\nMULTILINE='one\ntwo'\n"
        self.write_env(prefix + BEGIN + b"STALE=old\nDISABLE_TELEMETRY=0\n" + END)
        runner = runtime.Runner("linux")
        for _ in range(2):
            self.assertTrue(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertEqual(self.target.read_bytes(), prefix + BEGIN + b"DISABLE_TELEMETRY=1\n" + END)
        self.assertEqual(len(list(self.target.parent.glob(".env.bak.*"))), 1)

    def test_removes_stale_owned_defaults_without_removing_unmanaged_content(self):
        original = b"UNMANAGED=yes\n" + BEGIN + b"OLD=1\n" + END
        self.write_env(original)
        runner = runtime.Runner("linux")
        self.assertTrue(omp.reconcile_skill_environment({}, runner))
        self.assertEqual(self.target.read_bytes(), b"UNMANAGED=yes\n")
        self.assertEqual(len(list(self.target.parent.glob(".env.bak.*"))), 1)

    def test_rejects_malformed_or_nonterminal_blocks_without_writes_or_installation(self):
        fixtures = [
            BEGIN + b"DISABLE_TELEMETRY=1\n",
            END,
            BEGIN + b"DISABLE_TELEMETRY=1\n" + END + b"AFTER=user\n",
            BEGIN + b"DISABLE_TELEMETRY=1\n" + END + BEGIN + b"OTHER=1\n" + END,
            BEGIN + b"DISABLE_TELEMETRY='1'\n" + END,
            BEGIN + b"DISABLE_TELEMETRY=1\nDISABLE_TELEMETRY=0\n" + END,
            BEGIN + b"INVALID-NAME=1\n" + END,
        ]
        for original in fixtures:
            with self.subTest(original=original):
                self.write_env(original)
                with mock.patch.object(skills, "skill_command", side_effect=self.installer):
                    runner = self.reconcile([self.selected(environment={"DISABLE_TELEMETRY": "1"})])
                self.assertTrue(runner.failures)
                self.assertEqual(self.target.read_bytes(), original)
                self.assertEqual(self.children(), [])
                self.assertEqual(list(self.target.parent.glob(".env.bak.*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_refuses_symlinked_dotenv_without_replacing_link_or_target(self):
        self.target.parent.mkdir(parents=True)
        destination = self.home / "external.env"
        destination.write_bytes(b"EXTERNAL=keep\n")
        self.target.symlink_to(destination)
        runner = runtime.Runner("linux")
        self.assertFalse(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(destination.read_bytes(), b"EXTERNAL=keep\n")
        self.assertTrue(runner.failures)

    def test_backup_failure_prevents_replacement_and_installation(self):
        original = b"UNMANAGED=yes\n"
        self.write_env(original)
        with (
            mock.patch.object(manifests, "backup_manifest", side_effect=PermissionError("backup denied")),
            mock.patch.object(skills, "skill_command", side_effect=self.installer),
        ):
            runner = self.reconcile([self.selected(environment={"DISABLE_TELEMETRY": "1"})])
        self.assertEqual(self.target.read_bytes(), original)
        self.assertTrue(runner.failures)
        self.assertEqual(self.children(), [])

    def test_atomic_replace_failure_preserves_original_and_removes_temporary_file(self):
        original = b"USER_SETTING=kept\n"
        self.write_env(original)
        with mock.patch.object(omp.os, "replace", side_effect=PermissionError("replace denied")):
            runner = runtime.Runner("linux")
            self.assertFalse(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertEqual(self.target.read_bytes(), original)
        backups = list(self.target.parent.glob(".env.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(set(self.target.parent.iterdir()), {self.target, *backups})
        self.assertTrue(runner.failures)

    def test_dry_runs_preserve_existing_tree_and_do_not_create_absent_parent(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.write_env(b"USER_SETTING=kept\n")
                before = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
                runner = runtime.Runner("linux", dry_run=True)
                prior_env = runner.env.copy()
                with mock.patch.object(skills, "skill_command", side_effect=self.installer):
                    self.reconcile([self.selected(environment={"DISABLE_TELEMETRY": "1"})], runner)
                after = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
                self.assertEqual(runner.env, prior_env)
                self.assertEqual(runner.failures, [])
                if not existing:
                    self.assertFalse(self.target.parent.exists())

    def test_installer_receives_defaults_after_persistence_without_leaking_to_next_skill(self):
        runner = runtime.Runner("linux")
        runner.env.pop("DISABLE_TELEMETRY", None)
        original = runner.env.copy()
        entries = [self.selected(environment={"DISABLE_TELEMETRY": "1"}), self.selected("owner/other")]
        with mock.patch.object(skills, "skill_command", side_effect=self.installer):
            self.reconcile(entries, runner)
        records = self.children()
        self.assertEqual([record["privacy"] for record in records], ["1", None])
        self.assertTrue(all("DISABLE_TELEMETRY=1\n" in record["dotenv"] for record in records))
        self.assertEqual(runner.env, original)
        self.assertEqual(runner.failures, [])

    def test_installer_declarations_override_inherited_values_and_restore_them(self):
        for inherited, effective in (("0", "1"), ("", "1")):
            with self.subTest(inherited=inherited):
                record_path = self.home / "children.jsonl"
                record_path.unlink(missing_ok=True)
                runner = runtime.Runner("linux")
                runner.env["DISABLE_TELEMETRY"] = inherited
                with mock.patch.object(skills, "skill_command", side_effect=self.installer):
                    self.reconcile([self.selected(environment={"DISABLE_TELEMETRY": "1"})], runner)
                self.assertEqual(self.children()[0]["privacy"], effective)
                self.assertEqual(runner.env["DISABLE_TELEMETRY"], inherited)
                self.assertIn(b"DISABLE_TELEMETRY=1\n", self.target.read_bytes())

    def test_installer_failure_restores_defaults_and_preserves_unrelated_runner_changes(self):
        runner = runtime.Runner("linux")
        runner.env.pop("DISABLE_TELEMETRY", None)
        def fail(command, label):
            self.assertEqual(runner.env["DISABLE_TELEMETRY"], "1")
            runner.env["PATH"] = "refreshed-path"
            raise RuntimeError("installer interrupted")
        with mock.patch.object(runner, "run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "installer interrupted"):
                self.reconcile([self.selected(environment={"DISABLE_TELEMETRY": "1"})], runner)
        self.assertNotIn("DISABLE_TELEMETRY", runner.env)
        self.assertEqual(runner.env["PATH"], "refreshed-path")

    def test_conflicting_declarations_fail_before_any_persistence_or_installation(self):
        entries = [self.selected(environment={"DISABLE_TELEMETRY": "1"}),
                   self.selected("owner/other", {"DISABLE_TELEMETRY": "0"})]
        with mock.patch.object(skills, "skill_command", side_effect=self.installer):
            runner = self.reconcile(entries)
        self.assertTrue(runner.failures)
        self.assertIn("DISABLE_TELEMETRY", " ".join(runner.failures))
        self.assertFalse(self.target.parent.exists())
        self.assertEqual(self.children(), [])

    def test_matching_shared_declarations_and_generic_names_are_merged(self):
        runner = runtime.Runner("linux")
        runner.env.pop("DOTAI_TEST_SETTING", None)
        entries = [self.selected(environment={"DOTAI_TEST_SETTING": "enabled"}),
                   self.selected("owner/other", {"DOTAI_TEST_SETTING": "enabled", "SECOND": "path/value:ok"})]
        with mock.patch.object(skills, "skill_command", side_effect=self.installer):
            self.reconcile(entries, runner)
        self.assertEqual([record["other"] for record in self.children()], ["enabled", "enabled"])
        self.assertEqual(self.target.read_bytes(), BEGIN + b"DOTAI_TEST_SETTING=enabled\nSECOND=path/value:ok\n" + END)
        self.assertEqual(runner.failures, [])

    def test_selective_sync_retains_inactive_defaults_without_applying_new_inactive_declarations(self):
        self.write_env(BEGIN + b"OLD_SETTING=keep\n" + END)
        active = self.selected(environment={"DISABLE_TELEMETRY": "1"})
        inactive = self.selected("owner/inactive", {"NEW_INACTIVE": "ignored"})
        with (
            mock.patch.object(manifests, "load_manifest", return_value={"skills": [active]}),
            mock.patch.object(skills, "skill_command", side_effect=self.installer),
        ):
            runner = self.reconcile([active, inactive], recommended_only=True)
        self.assertEqual(self.target.read_bytes(), BEGIN + b"DISABLE_TELEMETRY=1\nOLD_SETTING=keep\n" + END)
        self.assertEqual([record["source"] for record in self.children()], ["owner/privacy"])
        self.assertEqual(runner.failures, [])

    def test_healthy_owned_skill_receives_environment_even_when_install_is_skipped(self):
        folder = self.home / ".agents" / "skills" / "alpha"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_bytes(b"# Alpha\n")
        lock = self.home / "xdg" / "skills" / ".skill-lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"version": 3, "skills": {"alpha": {
            "source": "owner/privacy", "sourceType": "github",
            "sourceUrl": "https://github.com/owner/privacy.git", "ref": None,
            "skillFolderHash": "0ae35bdd602b22221c7503baa29f72fd9f115298",
        }}}), encoding="utf-8")
        selected = self.selected(environment={"DISABLE_TELEMETRY": "1"})
        with mock.patch.object(skills, "skill_command", side_effect=self.installer):
            runner = self.reconcile([selected])
        self.assertIn(b"DISABLE_TELEMETRY=1\n", self.target.read_bytes())
        self.assertEqual(self.children(), [])
        self.assertEqual(runner.failures, [])

    def test_skill_health_reports_missing_or_changed_persistent_environment(self):
        folder = self.home / ".agents" / "skills" / "alpha"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_bytes(b"# Alpha\n")
        selected = self.selected(environment={"DISABLE_TELEMETRY": "1"})
        installed, detail = skills.skill_status(selected)
        self.assertFalse(installed)
        self.assertIn("environment", detail.lower())
        self.write_env(BEGIN + b"DISABLE_TELEMETRY=0\n" + END)
        self.assertFalse(skills.skill_status(selected)[0])
        runner = runtime.Runner("linux")
        self.assertTrue(omp.reconcile_skill_environment({"DISABLE_TELEMETRY": "1"}, runner))
        self.assertTrue(skills.skill_status(selected)[0])
        self.write_env(BEGIN + b"DISABLE_TELEMETRY='1'\n" + END)
        self.assertFalse(skills.skill_status(selected)[0])


if __name__ == "__main__":
    unittest.main()
