"""Accepted recommendation candidates resolve before touching personal state."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotai_app import lifecycle, locking, manifest, recommendations, runtime, skills, state


INSTALLER = '''import json, os, pathlib, sys
root = pathlib.Path(os.environ["DOTAI_HOME"])
args = sys.argv[1:]
if args[2] == "list":
    records = json.loads((root / "records.json").read_text())
    print(json.dumps([row for row in records if pathlib.Path(row["path"]).exists()]))
elif args[2] == "add":
    revision = args[3].rsplit("/", 1)[-1]
    content = {"a" * 40: b"accepted source content\\n",
               "c" * 40: b"newer source content\\n"}[revision]
    for index, value in enumerate(args):
        if value == "--skill":
            folder = root / ".agents" / "skills" / args[index + 1]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_bytes(content)
else:
    sys.exit(8)
'''


def entry(name, source="fixture/recommended"):
    return {"source": source, "agent": "universal", "skills": [name], "checkSkills": [name]}


def stack(entries):
    checks = [{"name": name, "check": [sys.executable, "-c", "print('24.0.0')"]}
              for name in ("node", "npx", "git", "uv", "curl", "python")]
    return {"version": 2, "prerequisites": checks, "packages": [], "skills": entries,
            "marketplaces": [], "plugins": [], "ompExtensions": [], "ompRouting": None,
            "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}}}


def one_file_tree(content):
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).digest()
    tree = b"100644 SKILL.md\0" + blob
    return hashlib.sha1(b"tree " + str(len(tree)).encode() + b"\0" + tree).hexdigest()


class RecommendationPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "stack.json"
        self.example = self.root / "recommendations.json"
        self.node = "v24.0.0"
        self.revision = "a" * 40
        self.requests = []
        self.metadata_failure = None
        self.desired_tree = one_file_tree(b"accepted source content\n")
        environment = {"DOTAI_HOME": str(self.root), "HOME": str(self.root),
                       "USERPROFILE": str(self.root), "DOTAI_STATE_DIR": str(self.root / "state"),
                       "XDG_STATE_HOME": str(self.root / "xdg"), "GH_HOST": "github.com"}
        environment_patch = patch.dict(os.environ, environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        recommendation_patch = patch.object(manifest, "SKILL_RECOMMENDATIONS", self.example)
        recommendation_patch.start()
        self.addCleanup(recommendation_patch.stop)
        self.installer = self.root / "installer.py"
        self.installer.write_text(INSTALLER)
        self.records = []
        self.original = self.configure([entry("old")], [entry("new")])
        self.old = self.install_existing(entry("old"))
        real_run = subprocess.run

        def process_boundary(command, **kwargs):
            if command == ["node", "--version"]:
                command = [sys.executable, "-c", f"print({self.node!r})"]
            elif isinstance(command, list) and command[0] == "npx":
                command = [sys.executable, str(self.installer), *command[1:]]
            return real_run(command, **kwargs)

        process_patch = patch.object(subprocess, "run", side_effect=process_boundary)
        process_patch.start()
        self.addCleanup(process_patch.stop)
        metadata_patch = patch.object(locking, "_fetch_json", side_effect=self.metadata)
        metadata_patch.start()
        self.addCleanup(metadata_patch.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def configure(self, before, desired, packages=None):
        original = stack(before)
        original["packages"] = packages or []
        self.path.write_text(json.dumps(original, indent=2) + "\n")
        self.example.write_text(json.dumps(stack(desired)))
        state.save_managed_recommendations(self.path, before)
        return original

    def install_existing(self, skill):
        name = skill["checkSkills"][0]
        folder = self.root / ".agents" / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_bytes(b"original local copy\n")
        self.records.append({"name": name, "source": skill["source"], "scope": "global",
                             "path": str(folder), "agents": []})
        (self.root / "records.json").write_text(json.dumps(self.records))
        owner_path = self.root / "xdg" / "skills" / ".skill-lock.json"
        owners = json.loads(owner_path.read_text()) if owner_path.exists() else {"version": 3, "skills": {}}
        owners["skills"][name] = {"source": skill["source"], "sourceType": "github",
                                   "sourceUrl": "https://github.com/" + skill["source"],
                                   "skillPath": f"skills/{name}/SKILL.md", "ref": None,
                                   "skillFolderHash": one_file_tree(b"original local copy\n")}
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(json.dumps(owners))
        return folder

    def metadata(self, url):
        self.requests.append(url)
        if self.metadata_failure and self.metadata_failure in url:
            raise runtime.DotAiError("Synthetic metadata unavailable; no latest fallback")
        if url.startswith("https://registry.npmjs.org/skills/"):
            return {"version": "1.7.1", "engines": {"node": ">=20.0.0"},
                    "dist": {"integrity": "sha512-fixture"}}
        if "/commits/" in url:
            return {"sha": self.revision, "commit": {"tree": {"sha": "b" * 40}}}
        if "/git/trees/" in url:
            desired = json.loads(self.example.read_text())["skills"]
            return {"truncated": False, "tree": [
                {"type": "tree", "path": "skills/" + name, "sha": self.desired_tree}
                for skill in desired for name in skill["checkSkills"]]}
        if url.startswith("https://metadata.example.invalid/"):
            return {"version": "2.3.4", "requires_python": ">=999.0"}
        raise AssertionError(f"Unexpected metadata URL: {url}")

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def review(self, original=None, answers=("all",), **kwargs):
        runner = runtime.Runner("linux", dry_run=kwargs.pop("dry_run", False))
        with patch("builtins.input", side_effect=answers):
            updated, accepted = recommendations.review_recommended_skills(
                original or self.original, self.path, runner, **kwargs)
        return updated, accepted, runner

    def test_accepted_source_metadata_failure_precedes_backup_retirement_and_state(self):
        for failure in ("registry.npmjs.org", "/commits/", "/git/trees/"):
            with self.subTest(failure=failure):
                self.metadata_failure = failure
                before = self.snapshot()
                updated, accepted, runner = self.review()
                self.assertTrue(runner.failures)
                self.assertEqual(updated, self.original)
                self.assertEqual(accepted, [entry("old")])
                self.assertEqual(self.snapshot(), before)

    def test_incompatible_installer_node_precedes_old_directory_removal(self):
        self.node = "v18.0.0"
        before = self.snapshot()
        updated, _, runner = self.review()
        self.assertTrue(runner.failures)
        self.assertEqual(updated, self.original)
        self.assertEqual(self.snapshot(), before)

    def test_incompatible_package_python_precedes_recommendation_removal(self):
        package = {"name": "Fixture Python tool", "managed": True, "version": "2.3.4",
                   "check": [sys.executable, "-c", "raise SystemExit(1)"],
                   "install": [[sys.executable, "-c", "raise SystemExit(99)"]],
                   "pinInstall": [[sys.executable, "-c", "print('{version}')"]],
                   "versionMetadata": {"exact": "https://metadata.example.invalid/{version}",
                                       "versionPath": ["version"],
                                       "pythonRequirementPath": ["requires_python"]}}
        original = self.configure([entry("old")], [], packages=[package])
        before = self.snapshot()
        updated, _, runner = self.review(original)
        self.assertTrue(runner.failures)
        self.assertEqual(updated, original)
        self.assertEqual(self.snapshot(), before)

    def test_execution_lease_conflict_precedes_manifest_and_retirement(self):
        guard = self.root / "stack.lock.json.write"
        guard.write_text("another writer")
        before = self.snapshot()
        updated, _, runner = self.review()
        self.assertTrue(runner.failures)
        self.assertEqual(updated, self.original)
        self.assertEqual(self.snapshot(), before)

    def test_accepted_candidate_revision_is_frozen_for_later_consumer(self):
        updated, _, runner = self.review()
        self.assertFalse(runner.failures)
        self.assertFalse(self.old.exists())
        requests = list(self.requests)
        self.revision = "c" * 40
        effective = locking.prepared(updated, self.path, runner, "sync")
        self.assertIsNotNone(effective)
        with locking.execution(self.path, runner):
            skills.reconcile_skills(effective, runner, refresh_sources={"fixture/recommended"},
                                    receipt_owned=lifecycle.skill_receipt_owned)
        self.assertEqual(self.requests, requests)
        self.assertEqual((self.root / ".agents" / "skills" / "new" / "SKILL.md").read_bytes(),
                         b"accepted source content\n")

    def test_replacing_source_cannot_adopt_colliding_old_owned_directory(self):
        original = self.configure([entry("old")], [entry("old", "fixture/replacement")])
        before = self.snapshot()
        updated, _, runner = self.review(original)
        self.assertTrue(runner.failures)
        self.assertEqual(updated, original)
        self.assertEqual(self.snapshot(), before)

    def test_declined_cleanup_and_recommendations_do_not_resolve_or_mutate(self):
        original = self.configure([entry("old", "fixture/user")], [entry("new")])
        state.save_managed_recommendations(self.path, [])
        before = self.snapshot()
        updated, _, runner = self.review(original, answers=("no", "none"), enforce=True)
        self.assertFalse(runner.failures)
        self.assertEqual(updated, original)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_resolves_conditional_candidate_without_writes(self):
        original = self.configure([entry("old", "fixture/user")], [entry("new")])
        state.save_managed_recommendations(self.path, [])
        before = self.snapshot()
        updated, _, runner = self.review(original, answers=(), enforce=True, dry_run=True)
        self.assertFalse(runner.failures)
        self.assertEqual(updated["skills"], [entry("new")])
        self.assertIsNotNone(locking.prepared(updated, self.path, runner, "sync"))
        self.assertEqual(self.snapshot(), before)

    def test_direct_apply_keeps_existing_signature_and_preflights_candidate(self):
        selected = [{"kind": "update", "source": "fixture/recommended",
                     "before": entry("old"), "after": entry("new")}]
        self.metadata_failure = "/commits/"
        before = self.snapshot()
        runner = runtime.Runner("linux")
        updated, accepted = recommendations._apply_skill_changes(
            self.original, self.path, runner, [entry("old")], selected)
        self.assertTrue(runner.failures)
        self.assertEqual(updated, self.original)
        self.assertEqual(accepted, [entry("old")])
        self.assertEqual(self.snapshot(), before)

    def test_update_skills_is_forwarded_before_cleanup_and_acceptance(self):
        user = entry("old", "fixture/user")
        current = entry("existing")
        self.install_existing(current)
        desired = {**current, "skills": ["existing", "new"], "checkSkills": ["existing", "new"]}
        original = self.configure([user, current], [desired])
        state.save_managed_recommendations(self.path, [current])
        before = self.snapshot()
        self.metadata_failure = "registry.npmjs.org"
        updated, _, runner = self.review(original, answers=("yes",), enforce=True, update_skills=True)
        self.assertTrue(runner.failures)
        self.assertEqual(updated, original)
        self.assertEqual(self.snapshot(), before)

    def locked_existing_candidate(self):
        current = {**entry("existing", "fixture/unchanged"), "enabled": True}
        self.install_existing(current)
        original = self.configure([entry("old"), current], [entry("new"), current])
        provenance = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = {"intent": provenance, "platform": "linux", "source": "fixture/unchanged",
                  "revision": "d" * 40, "installerVersion": "1.7.1",
                  "trees": {"existing": one_file_tree(b"original local copy\n")},
                  "sourcePaths": {"existing": "skills/existing"}}
        (self.root / "stack.lock.json").write_text(json.dumps({
            "version": 1, "packages": {}, "plugins": {},
            "skills": {"fixture/unchanged|universal": record}}))
        return original

    def test_accepted_change_does_not_refresh_unrelated_healthy_latest_source(self):
        original = self.locked_existing_candidate()
        updated, _, runner = self.review(original)
        self.assertFalse(runner.failures)
        effective = locking.prepared(updated, self.path, runner, "sync")
        self.assertIsNotNone(effective)
        unchanged = next(skill for skill in effective["skills"] if skill["source"] == "fixture/unchanged")
        self.assertEqual(unchanged["revision"], "d" * 40)
        self.assertFalse(any("fixture/unchanged" in url for url in self.requests))
        with locking.execution(self.path, runner):
            skills.reconcile_skills(effective, runner, refresh_sources={"fixture/recommended"},
                                    receipt_owned=lifecycle.skill_receipt_owned)
        self.assertEqual((self.root / ".agents" / "skills" / "existing" / "SKILL.md").read_text(),
                         "original local copy\n")

    def test_update_skills_checks_unrelated_source_before_accepted_change(self):
        original = self.locked_existing_candidate()
        before = self.snapshot()
        self.node = "v18.0.0"
        updated, _, runner = self.review(original, update_skills=True)
        self.assertTrue(runner.failures)
        self.assertEqual(updated, original)
        self.assertEqual(self.snapshot(), before)

    def test_declined_user_cleanup_is_excluded_from_accepted_execution_plan(self):
        user = entry("external", "unsupported-local-source")
        original = self.configure([user], [entry("new")])
        state.save_managed_recommendations(self.path, [])
        updated, accepted, runner = self.review(original, answers=("no", "all"), enforce=True)
        self.assertFalse(runner.failures)
        self.assertEqual(updated["skills"], [user, entry("new")])
        self.assertEqual(accepted, [entry("new")])
        selected = copy.deepcopy(updated)
        selected["skills"] = [entry("new")]
        effective = locking.prepared(selected, self.path, runner, "sync", provenance_manifest=updated)
        self.assertIsNotNone(effective)
        self.assertEqual([skill["source"] for skill in effective["skills"]], ["fixture/recommended"])


if __name__ == "__main__":
    unittest.main()
