from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dotai as existing
from dotai_app import cli, manifest as manifests, routing, runtime, state, terminal


def minimal_manifest() -> dict:
    return {
        "version": 1, "packages": [], "skills": [], "marketplaces": [],
        "plugins": [], "ompExtensions": [], "mcp": {"target": "mcp.json", "servers": {}},
        "ompRouting": None,
    }


@contextlib.contextmanager
def partial_temporary_write(*args, **kwargs):
    with REAL_TEMPORARY_FILE(*args, **kwargs) as handle:
        original_write = handle.write

        def fail(payload):
            original_write(payload[:7])
            handle.flush()
            raise OSError("injected partial write")

        with mock.patch.object(handle, "write", side_effect=fail):
            yield handle


REAL_TEMPORARY_FILE = tempfile.NamedTemporaryFile

# This executable replaces only the external OMP boundary. Successful setters
# persist values; one failed setter leaves the corresponding value unchanged.
OMP_BOUNDARY = '''import json, os, pathlib, sys
root = pathlib.Path(os.environ["DOTAI_HOME"])
target = root / "omp-values.json"
args = sys.argv[1:]
if args == ["models", "--json"]:
    print((root / "omp-models.json").read_text())
elif args[:2] == ["config", "get"] and len(args) == 4 and args[3] == "--json":
    values = json.loads(target.read_text())
    print(json.dumps({"key": args[2], "value": values[args[2]]}))
elif args[:2] == ["config", "set"] and len(args) == 4:
    with (root / "omp-writes.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\\n")
    failure = root / "omp-fail-once"
    if failure.exists() and failure.read_text() == args[2]:
        failure.unlink()
        sys.exit(7)
    values = json.loads(target.read_text())
    try:
        value = json.loads(args[3])
    except json.JSONDecodeError:
        value = args[3]
    values[args[2]] = value
    target.write_text(json.dumps(values))
else:
    sys.exit(9)
'''


def install_omp_boundary(root: Path, selectors: list[str], values: dict) -> None:
    (root / "omp-boundary.py").write_text(OMP_BOUNDARY, encoding="utf-8")
    (root / "omp-models.json").write_text(
        json.dumps({"models": [{"selector": selector} for selector in selectors]}), encoding="utf-8"
    )
    (root / "omp-values.json").write_text(json.dumps(values), encoding="utf-8")


class PersistenceRoutingCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "stack.json"
        self.path.write_text(json.dumps(minimal_manifest(), indent=4) + "\n", encoding="utf-8")
        self.original = self.path.read_bytes()
        self.state_dir = self.root / "state"
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "DOTAI_HOME": str(self.root),
            "DOTAI_STATE_DIR": str(self.state_dir), "XDG_STATE_HOME": str(self.root / "xdg"),
            "GH_HOST": "github.com", "NO_COLOR": "1", "DOTAI_PLATFORM": "linux",
        })
        environment.start()
        self.addCleanup(environment.stop)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)
        original_argv = runtime.Runner.argv

        def boundary_argv(runner, command):
            if isinstance(command, list) and command[0] == "omp":
                return [sys.executable, str(self.root / "omp-boundary.py"), *command[1:]]
            return original_argv(runner, command)

        boundary = mock.patch.object(runtime.Runner, "argv", boundary_argv)
        boundary.start()
        self.addCleanup(boundary.stop)
        self.output = io.StringIO()
        stdout = contextlib.redirect_stdout(self.output)
        stderr = contextlib.redirect_stderr(self.output)
        stdout.__enter__()
        stderr.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)
        self.addCleanup(stderr.__exit__, None, None, None)

    def changed_manifest(self) -> dict:
        candidate = minimal_manifest()
        candidate["localOnly"] = {"retained": True}
        return candidate

    def state_file(self, name: str, payload: bytes) -> Path:
        self.state_dir.mkdir(exist_ok=True)
        target = self.state_dir / name
        target.write_bytes(payload)
        return target

    def test_successful_backup_keeps_complete_preimage(self) -> None:
        backup = manifests.write_manifest(self.path, self.changed_manifest(), backup=True)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(manifests.load_manifest(self.path)["localOnly"], {"retained": True})
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_partial_backup_failure_removes_incomplete_copy(self) -> None:
        def partial_copy(source, destination):
            destination.write(source.read(8))
            destination.flush()
            raise OSError("injected copy failure")

        with mock.patch.object(manifests.shutil, "copyfileobj", side_effect=partial_copy):
            with self.assertRaises(OSError):
                manifests.write_manifest(self.path, self.changed_manifest(), backup=True)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(set(self.root.iterdir()), {self.path})
        self.assertNotIn("[OK]", self.output.getvalue())

    def test_timestamp_collision_never_overwrites_existing_backup(self) -> None:
        instant = dt.datetime(2025, 3, 4, 5, 6, 7, 123456, tzinfo=dt.timezone.utc)
        collision = self.root / "stack.json.bak.20250304T050607123456Z"
        collision.write_bytes(b"existing complete backup")
        with mock.patch.object(manifests.dt, "datetime") as clock:
            clock.now.return_value = instant
            with self.assertRaises(FileExistsError):
                manifests.write_manifest(self.path, self.changed_manifest(), backup=True)
        self.assertEqual(collision.read_bytes(), b"existing complete backup")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(set(self.root.iterdir()), {self.path, collision})

    def test_invalid_candidate_with_backup_leaves_files_untouched(self) -> None:
        candidate = self.changed_manifest()
        candidate["plugins"] = [{"id": "invalid"}]
        with self.assertRaises(runtime.DotAiError):
            manifests.write_manifest(self.path, candidate, backup=True)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_partial_manifest_temp_write_preserves_original_and_cleans_temp(self) -> None:
        with mock.patch.object(manifests.tempfile, "NamedTemporaryFile", side_effect=partial_temporary_write):
            with self.assertRaises(OSError):
                manifests.write_manifest(self.path, self.changed_manifest())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_manifest_replace_failure_keeps_complete_backup_without_temp(self) -> None:
        with mock.patch.object(manifests.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                manifests.write_manifest(self.path, self.changed_manifest(), backup=True)
        backups = list(self.root.glob("stack.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(set(self.root.iterdir()), {self.path, backups[0]})

    def test_initialization_bad_template_creates_nothing_and_can_retry(self) -> None:
        example = self.root / "example.json"
        target = self.root / "new" / "stack.json"
        with mock.patch.object(manifests, "EXAMPLE_MANIFEST", example):
            for payload in (None, b"{", b"\xff"):
                with self.subTest(payload=payload):
                    if payload is not None:
                        example.write_bytes(payload)
                    self.assertEqual(cli.main(["--manifest", str(target), "init"]), 2)
                    self.assertFalse(target.parent.exists())
            example.write_bytes(self.original)
            self.assertEqual(cli.main(["--manifest", str(target), "init"]), 0)
        self.assertEqual(target.read_bytes(), self.original)

    def test_initialization_competing_creator_is_preserved(self) -> None:
        target = self.root / "competing.json"
        original_open = os.open
        winning_bytes = b'{"createdBy": "other process"}'

        def competing_open(path, flags, mode=0o777):
            if Path(path) == target:
                target.write_bytes(winning_bytes)
            return original_open(path, flags, mode)

        with (
            mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.path),
            mock.patch.object(manifests.os, "open", side_effect=competing_open),
        ):
            self.assertEqual(cli.main(["--manifest", str(target), "init"]), 2)
        self.assertEqual(target.read_bytes(), winning_bytes)
        self.assertNotIn("[OK]", self.output.getvalue())

    def test_initialization_partial_write_and_fsync_failure_remove_target_for_retry(self) -> None:
        real_fdopen = os.fdopen
        target = self.root / "new.json"

        @contextlib.contextmanager
        def broken_fdopen(*args, **kwargs):
            with real_fdopen(*args, **kwargs) as handle:
                write = handle.write

                def fail(payload):
                    write(payload[:7])
                    raise OSError("injected initial write failure")

                with mock.patch.object(handle, "write", side_effect=fail):
                    yield handle

        with mock.patch.object(manifests, "EXAMPLE_MANIFEST", self.path):
            for operation, replacement in (("fdopen", broken_fdopen), ("fsync", OSError("injected fsync failure"))):
                with self.subTest(operation=operation):
                    with mock.patch.object(manifests.os, operation, side_effect=replacement):
                        self.assertEqual(cli.main(["--manifest", str(target), "init"]), 2)
                    self.assertFalse(target.exists())
                    self.assertNotIn("[OK]", self.output.getvalue())
            self.assertEqual(cli.main(["--manifest", str(target), "init"]), 0)
        self.assertEqual(target.read_bytes(), self.original)

    def test_history_rejects_corrupt_wrong_shape_and_other_manifest_without_writes(self) -> None:
        for payload in (b"{", b"[]", b"null", b"\xff", b'{"manifest":"other","lastSuccess":"old"}'):
            with self.subTest(payload=payload):
                target = self.state_file("state.json", payload)
                self.assertEqual(state.read_state(self.path), {})
                self.assertEqual(target.read_bytes(), payload)

    def test_ownership_rejects_corrupt_and_wrong_shaped_history(self) -> None:
        key = os.path.normcase(str(self.path.resolve()))
        invalid = (b"{", b"[]", b"\xff", json.dumps({key: {"version": 1, "skills": ["not a skill"]}}).encode())
        for payload in invalid:
            with self.subTest(payload=payload):
                target = self.state_file("recommended-skills.json", payload)
                self.assertIsNone(state.managed_recommendations(self.path))
                self.assertEqual(target.read_bytes(), payload)

    def test_ownership_is_kept_separately_for_each_manifest(self) -> None:
        other = self.root / "other.json"
        first = [{"source": "owner/first", "skills": ["alpha"]}]
        second = [{"source": "owner/second", "skills": ["beta"]}]
        state.save_managed_recommendations(self.path, first)
        state.save_managed_recommendations(other, second)
        state.save_state(self.path, runtime.Runner("ubuntu"), "sync")
        self.assertEqual(state.managed_recommendations(self.path), first)
        self.assertEqual(state.managed_recommendations(other), second)
        self.assertEqual(state.read_state(self.path)["lastOperation"], "sync")
        self.assertEqual(state.read_state(other), {})

    def test_legacy_ownership_requires_matching_manifest_and_valid_skills(self) -> None:
        skills = [{"source": "owner/legacy", "skills": ["alpha"]}]
        for owner, entries, expected in (
            (str(self.path.resolve()), skills, skills),
            (str((self.root / "other.json").resolve()), skills, None),
            (str(self.path.resolve()), [{"source": 17}], None),
        ):
            with self.subTest(owner=owner, entries=entries):
                self.state_file("state.json", json.dumps({"manifest": owner, "managedRecommendedSkills": entries}).encode())
                self.assertEqual(state.managed_recommendations(self.path), expected)

    def test_unversioned_ownership_only_adopts_current_recommendation_sources(self) -> None:
        managed = {"source": "owner/current", "skills": ["alpha"]}
        retired = {"source": "owner/retired", "skills": ["beta"]}
        example = minimal_manifest()
        example["skills"] = [managed]
        example_path = self.root / "example.json"
        example_path.write_text(json.dumps(example), encoding="utf-8")
        self.state_file("recommended-skills.json", json.dumps({os.path.normcase(str(self.path.resolve())): [managed, retired]}).encode())
        with mock.patch.object(manifests, "EXAMPLE_MANIFEST", example_path):
            self.assertEqual(state.managed_recommendations(self.path), [managed])

    def test_dry_run_and_failed_reconcile_do_not_advance_success_or_ownership(self) -> None:
        old_history = json.dumps({"manifest": str(self.path.resolve()), "lastSuccess": "old", "lastOperation": "sync"}).encode()
        history = self.state_file("state.json", old_history)
        state.save_managed_recommendations(self.path, [{"source": "owner/old"}])
        ownership = self.state_dir / "recommended-skills.json"
        old_ownership = ownership.read_bytes()
        for dry_run, failures in ((True, []), (False, ["failed integration"])):
            with self.subTest(dry_run=dry_run):
                runner = runtime.Runner("ubuntu", dry_run=dry_run)
                runner.failures.extend(failures)
                state.save_state(self.path, runner, "update", [{"source": "owner/new"}])
                self.assertEqual(history.read_bytes(), old_history)
                self.assertEqual(ownership.read_bytes(), old_ownership)

    def test_partial_ownership_write_preserves_all_manifests_and_cleans_temp(self) -> None:
        state.save_managed_recommendations(self.path, [{"source": "owner/old"}])
        other = self.root / "other.json"
        state.save_managed_recommendations(other, [{"source": "owner/other"}])
        target = self.state_dir / "recommended-skills.json"
        original = target.read_bytes()
        with mock.patch.object(state.tempfile, "NamedTemporaryFile", side_effect=partial_temporary_write):
            with self.assertRaises(OSError):
                state.save_managed_recommendations(self.path, [{"source": "owner/new"}])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(set(self.state_dir.iterdir()), {target})

    def test_ownership_replace_failure_preserves_all_manifests_and_cleans_temp(self) -> None:
        state.save_managed_recommendations(self.path, [{"source": "owner/old"}])
        target = self.state_dir / "recommended-skills.json"
        original = target.read_bytes()
        with mock.patch.object(state.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                state.save_managed_recommendations(self.path, [{"source": "owner/new"}])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(set(self.state_dir.iterdir()), {target})

    def test_partial_success_history_write_does_not_destroy_previous_success(self) -> None:
        history = self.state_file("state.json", b'{"manifest":"old","lastSuccess":"old success"}')
        original = history.read_bytes()
        state.save_managed_recommendations(self.path, [{"source": "owner/current"}])
        real_path_write = Path.write_text

        def partial_history_write(path, payload, *args, **kwargs):
            if path == history:
                real_path_write(path, payload[:7], *args, **kwargs)
                raise OSError("injected history write failure")
            return real_path_write(path, payload, *args, **kwargs)

        # Covers the old direct-write path and a safe temporary-write path.
        temporary_writes = 0

        def history_temporary_file(*args, **kwargs):
            nonlocal temporary_writes
            temporary_writes += 1
            if temporary_writes == 1:
                return REAL_TEMPORARY_FILE(*args, **kwargs)
            return partial_temporary_write(*args, **kwargs)

        with (
            mock.patch.object(Path, "write_text", partial_history_write),
            mock.patch.object(state.tempfile, "NamedTemporaryFile", side_effect=history_temporary_file),
        ):
            with self.assertRaises(OSError):
                state.save_state(self.path, runtime.Runner("ubuntu"), "update", [{"source": "owner/current"}])
        self.assertEqual(history.read_bytes(), original)
        self.assertEqual(state.managed_recommendations(self.path), [{"source": "owner/current"}])
        self.assertEqual(set(self.state_dir.iterdir()), {history, self.state_dir / "recommended-skills.json"})

    def test_history_replace_failure_preserves_previous_success_and_cleans_temp(self) -> None:
        history = self.state_file("state.json", b'{"manifest":"old","lastSuccess":"old success"}')
        original = history.read_bytes()
        state.save_managed_recommendations(self.path, [{"source": "owner/current"}])
        real_replace = os.replace

        def fail_history(source, target):
            if Path(target) == history:
                raise OSError("injected history replace failure")
            return real_replace(source, target)

        with mock.patch.object(state.os, "replace", side_effect=fail_history):
            with self.assertRaises(OSError):
                state.save_state(self.path, runtime.Runner("ubuntu"), "update", [{"source": "owner/current"}])
        self.assertEqual(history.read_bytes(), original)
        self.assertEqual(state.managed_recommendations(self.path), [{"source": "owner/current"}])
        self.assertEqual(set(self.state_dir.iterdir()), {history, self.state_dir / "recommended-skills.json"})

    def test_recommendation_read_and_decode_failures_are_domain_errors(self) -> None:
        target = self.root / "recommendations.json"
        for payload in (None, b"{", b"\xff"):
            with self.subTest(payload=payload):
                if payload is not None:
                    target.write_bytes(payload)
                with self.assertRaises(runtime.DotAiError):
                    routing.load_routing_recommendations(target)

    def test_missing_supported_provider_and_unavailable_requested_primary_do_not_write(self) -> None:
        for selectors, requested in (([], None), (["github-copilot/interactive-model"], "anthropic")):
            with self.subTest(selectors=selectors, requested=requested):
                install_omp_boundary(self.root, selectors, {})
                arguments = ["--manifest", str(self.path), "configure", "omp-routing"]
                if requested:
                    arguments.extend(["--primary", requested])
                with mock.patch.object(routing, "load_routing_recommendations", return_value=existing.DotAiTests.routing_recommendations(None)):
                    self.assertEqual(cli.main(arguments), 2)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assertEqual(list(self.root.glob("stack.json.bak.*")), [])
                self.assertFalse((self.root / "omp-writes.jsonl").exists())

    def test_middle_omp_failure_retains_intent_then_retry_converges_without_backup(self) -> None:
        recommendations = existing.DotAiTests.routing_recommendations(None)
        selectors = ["openai-codex/interactive-model", "openai-codex/utility-model"]
        values = {
            "modelRoles": {"custom": "private/keep"},
            "retry.fallbackChains": {"custom": ["private/keep"]},
            "task.agentModelOverrides": {"reviewer": "@slow"},
            "retry.modelFallback": False, "retry.usageAwareFallback": False,
            "retry.usageReservePct": 0, "retry.usageReservePolicy": "confirm",
            "retry.fallbackRevertPolicy": "never",
        }
        install_omp_boundary(self.root, selectors, values)
        (self.root / "omp-fail-once").write_text("retry.fallbackChains", encoding="utf-8")
        with mock.patch.object(routing, "load_routing_recommendations", return_value=recommendations):
            self.assertEqual(cli.main(["--manifest", str(self.path), "configure", "omp-routing"]), 1)
            persisted = manifests.load_manifest(self.path)
            self.assertEqual(persisted["ompRouting"]["providers"], ["openai-codex"])
            self.assertEqual(persisted["ompRouting"]["primaryProvider"], "openai-codex")
            self.assertNotIn("roles", persisted["ompRouting"])
            backup = next(self.root.glob("stack.json.bak.*"))
            self.assertEqual(backup.read_bytes(), self.original)
            after_failure = json.loads((self.root / "omp-values.json").read_text())
            self.assertEqual(after_failure["modelRoles"]["default"], "openai-codex/interactive-model")
            self.assertEqual(after_failure["retry.fallbackChains"], {"custom": ["private/keep"]})
            self.assertEqual(after_failure["task.agentModelOverrides"]["task"], "@task")
            self.assertEqual(routing.omp_routing_status(persisted, runtime.Runner("ubuntu"))[0], "DRIFT")
            before_retry = (self.root / "omp-writes.jsonl").read_text().splitlines()
            self.assertEqual(cli.main(["--manifest", str(self.path), "configure", "omp-routing"]), 0)
            writes = (self.root / "omp-writes.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(row)[2] for row in writes[len(before_retry):]], ["retry.fallbackChains"])
            self.assertEqual(routing.omp_routing_status(manifests.load_manifest(self.path), runtime.Runner("ubuntu"))[0], "OK")
            self.assertEqual(cli.main(["--manifest", str(self.path), "configure", "omp-routing"]), 0)
            self.assertEqual((self.root / "omp-writes.jsonl").read_text().splitlines(), writes)
        final_values = json.loads((self.root / "omp-values.json").read_text())
        self.assertEqual(final_values["modelRoles"]["custom"], "private/keep")
        self.assertEqual(final_values["retry.fallbackChains"]["custom"], ["private/keep"])
        self.assertEqual(final_values["task.agentModelOverrides"]["reviewer"], "@slow")
        self.assertEqual(set(self.root.glob("stack.json.bak.*")), {backup})

    def test_manifest_persistence_failure_blocks_all_omp_setters(self) -> None:
        manifest, selectors, values = existing.DotAiTests.compact_status_case(existing.DotAiTests())
        install_omp_boundary(self.root, selectors, values)
        with (
            mock.patch.object(routing, "load_routing_recommendations", return_value=existing.DotAiTests.routing_recommendations(None)),
            mock.patch.object(manifests.os, "replace", side_effect=OSError("injected manifest persistence failure")),
        ):
            self.assertEqual(cli.main(["--manifest", str(self.path), "configure", "omp-routing"]), 2)
        self.assertFalse((self.root / "omp-writes.jsonl").exists())
        self.assertEqual(json.loads((self.root / "omp-values.json").read_text()), values)
        self.assertEqual(self.path.read_bytes(), self.original)
        backup = next(self.root.glob("stack.json.bak.*"))
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertNotIn("[OK]", self.output.getvalue())

    def test_wrong_scalar_types_report_drift_and_are_repaired(self) -> None:
        manifest, selectors, values = existing.DotAiTests.compact_status_case(existing.DotAiTests())
        self.path.write_text(json.dumps(manifest), encoding="utf-8")
        original = self.path.read_bytes()
        values["retry.modelFallback"] = 1
        values["retry.usageAwareFallback"] = 1
        values["retry.usageReservePct"] = 10.0
        install_omp_boundary(self.root, selectors, values)
        with mock.patch.object(routing, "load_routing_recommendations", return_value=existing.DotAiTests.routing_recommendations(None)):
            self.assertEqual(routing.omp_routing_status(manifest, runtime.Runner("ubuntu"))[0], "DRIFT")
            self.assertEqual(cli.main(["--manifest", str(self.path), "configure", "omp-routing"]), 0)
            self.assertEqual(routing.omp_routing_status(manifest, runtime.Runner("ubuntu"))[0], "OK")
        repaired = json.loads((self.root / "omp-values.json").read_text())
        self.assertIs(repaired["retry.modelFallback"], True)
        self.assertIs(repaired["retry.usageAwareFallback"], True)
        self.assertIs(type(repaired["retry.usageReservePct"]), int)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("stack.json.bak.*")), [])


if __name__ == "__main__":
    unittest.main()
