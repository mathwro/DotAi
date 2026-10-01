from __future__ import annotations

import contextlib
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
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import mcp, runtime


class McpCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.target = self.home / ".omp" / "agent" / "mcp.json"
        self.target.parent.mkdir(parents=True)
        self.environment = {
            "HOME": str(self.home),
            "DOTAI_HOME": str(self.home),
            "DOTAI_STATE_DIR": str(self.home / "state"),
            "XDG_STATE_HOME": str(self.home / "xdg-state"),
            "GH_HOST": "fixtures.invalid",
        }
        environment = mock.patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)
        repository = mock.patch.object(runtime, "ROOT", self.home / "repository")
        repository.start()
        self.addCleanup(repository.stop)

    def manifest(self, servers: dict) -> dict:
        return {
            "version": 1,
            "packages": [],
            "skills": [],
            "marketplaces": [],
            "plugins": [],
            "ompExtensions": [],
            "mcp": {"target": str(self.target), "servers": servers},
        }

    def sync(self, manifest: dict, dry_run: bool = False) -> tuple[bool, runtime.Runner]:
        runner = runtime.Runner("ubuntu", dry_run=dry_run)
        with contextlib.redirect_stdout(io.StringIO()):
            changed = mcp.sync_mcp(manifest, runner)
        return changed, runner

    def assert_preserved(self, original: bytes) -> None:
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(list(self.target.parent.glob("mcp.json.bak.*")), [])

    def assert_invalid_target(self, payload: bytes) -> None:
        self.target.write_bytes(payload)
        manifest = self.manifest({"managed": {"type": "http", "url": "https://managed.example.test/mcp"}})
        manifest_path = self.home / "explicit-stack.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                command = [
                    sys.executable, str(ROOT / "dotai.py"), "--color", "never",
                    "--platform", "ubuntu", "--manifest", str(manifest_path), "update",
                ]
                if dry_run:
                    command.append("--dry-run")
                result = subprocess.run(
                    command, cwd=self.home, env=os.environ.copy(), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn("Traceback", result.stdout)
                self.assertIn(str(self.target), result.stdout)
                self.assert_preserved(payload)
                self.assertFalse((self.home / "state").exists())
        healthy, detail = mcp.mcp_status(manifest)
        self.assertFalse(healthy, detail)
        self.assertIn(str(self.target), detail)
        self.assert_preserved(payload)

    def test_nonobject_target_container_is_rejected_without_coercion(self) -> None:
        for container in (None, [], [["personal", {"command": "keep"}]], "", "servers", 3, False):
            with self.subTest(container=container):
                self.assert_invalid_target(json.dumps({"mcpServers": container}).encode("utf-8"))

    def test_malformed_target_root_json_and_utf8_fail_without_mutation(self) -> None:
        for payload in (b"null", b"[]", b'"config"', b"3", b"false", b'{"mcpServers":', b"\xff"):
            with self.subTest(payload=payload):
                self.assert_invalid_target(payload)

    def test_malformed_target_disabled_metadata_is_not_treated_as_enabled(self) -> None:
        for disabled in (None, "managed", {"managed": True}, 3, [None], [{}], [3]):
            with self.subTest(disabled=disabled):
                self.assert_invalid_target(json.dumps({
                    "disabledServers": disabled,
                    "mcpServers": {"managed": {"url": "https://managed.example.test/mcp"}},
                }).encode("utf-8"))

    def test_unreadable_target_fails_actionably_without_mutation(self) -> None:
        original = b'{"mcpServers":{"personal":{"url":"https://personal.example.test/mcp"}}}'
        self.target.write_bytes(original)
        manifest = self.manifest({"managed": {"type": "http", "url": "https://managed.example.test/mcp"}})
        original_read_text = Path.read_text

        def read_text(path: Path, *args, **kwargs):
            if path == self.target:
                raise PermissionError("fixture read denied")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", new=read_text):
            with self.subTest(operation="status"):
                healthy, detail = mcp.mcp_status(manifest)
                self.assertFalse(healthy, detail)
                self.assertIn(str(self.target), detail)
            for dry_run in (True, False):
                with self.subTest(operation="sync", dry_run=dry_run):
                    with self.assertRaises(runtime.DotAiError) as failure:
                        self.sync(manifest, dry_run)
                    self.assertIn(str(self.target), str(failure.exception))
        self.assert_preserved(original)

    def test_bad_provider_files_do_not_block_healthy_discovery_or_get_rewritten(self) -> None:
        self.target.write_bytes(b'{"mcpServers":{"invalid":null,"bad-args":{"command":"python3","args":null}}}')
        original = self.target.read_bytes()
        healthy_provider = self.home / ".config" / "opencode" / "opencode.json"
        healthy_provider.parent.mkdir(parents=True)
        healthy_provider.write_text(json.dumps({"mcp": {"healthy-alias": {
            "type": "remote", "url": "https://healthy.example.test/mcp",
            "headers": {"Authorization": "HEALTHY_AUTH_FROM_ENV"},
        }}}), encoding="utf-8")
        healthy_bytes = healthy_provider.read_bytes()
        broken = self.home / ".cursor" / "mcp.json"
        broken.parent.mkdir(parents=True)
        bad_payloads = [b'{"mcpServers":', b"[]", b"null", b"\xff"]
        bad_payloads.extend(json.dumps({
            "disabledServers": disabled,
            "mcpServers": {"broken-alias": {"url": "https://broken.example.test/mcp"}},
        }).encode("utf-8") for disabled in (None, "broken-alias", {}, 3, [None], [{}], [3]))
        bad_payloads.extend(json.dumps({"mcpServers": container}).encode("utf-8")
                            for container in (None, [], "servers"))
        manifest = self.manifest({"managed": {"type": "http", "url": "https://healthy.example.test/mcp"}})
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                broken.write_bytes(payload)
                healthy, detail = mcp.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                for dry_run in (True, False):
                    changed, runner = self.sync(manifest, dry_run)
                    self.assertFalse(changed)
                    self.assertEqual(runner.failures, [])
                    self.assert_preserved(original)
                    self.assertEqual(broken.read_bytes(), payload)
                    self.assertEqual(healthy_provider.read_bytes(), healthy_bytes)
                unhealthy = self.manifest({"broken": {"type": "http", "url": "https://broken.example.test/mcp"}})
                self.assertFalse(mcp.mcp_status(unhealthy)[0])

    def test_ambiguous_writable_stdio_aliases_conflict_without_mutation(self) -> None:
        self.target.write_text(json.dumps({"mcpServers": {
            "first-alias": {"command": "python3", "args": ["-m", "fixture_server"], "timeout": 5},
            "second-alias": {"command": "python3", "args": ["-m", "fixture_server"], "timeout": 10},
        }, "customTopLevel": {"owner": "user"}}), encoding="utf-8")
        before = self.target.read_bytes()
        manifest = self.manifest({"managed": {
            "type": "stdio", "command": "python3", "args": ["-m", "fixture_server"], "timeout": 30,
        }})
        self.assertFalse(mcp.mcp_status(manifest)[0])
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                changed, runner = self.sync(manifest, dry_run)
                self.assertFalse(changed)
                self.assertTrue(runner.failures)
                self.assert_preserved(before)

    def test_required_cwd_selects_only_matching_writable_alias(self) -> None:
        original = {"mcpServers": {
            "first-alias": {
                "command": "python3", "args": ["-m", "fixture_server"],
                "cwd": "/workspace/first", "timeout": 5,
                "env": {"TOKEN": "FIRST_TOKEN_FROM_ENV"},
            },
            "second-alias": {
                "command": "python3", "args": ["-m", "fixture_server"],
                "cwd": "/workspace/second", "timeout": 10,
                "env": {"TOKEN": "OLD_TOKEN_FROM_ENV", "EXTRA": "EXTRA_TOKEN_FROM_ENV"},
                "providerOptions": {"owner": "user"},
            },
            "invalid": None,
        }, "customTopLevel": {"owner": "user"}}
        self.target.write_text(json.dumps(original), encoding="utf-8")
        before = self.target.read_bytes()
        manifest = self.manifest({"managed": {
            "type": "stdio", "command": "python3", "args": ["-m", "fixture_server"],
            "cwd": "/workspace/second", "timeout": 30, "env": {"TOKEN": "REQUIRED_TOKEN_FROM_ENV"},
        }})
        self.assertFalse(mcp.mcp_status(manifest)[0])
        changed, runner = self.sync(manifest, True)
        self.assertTrue(changed)
        self.assertEqual(runner.failures, [])
        self.assert_preserved(before)
        changed, runner = self.sync(manifest)
        self.assertTrue(changed)
        self.assertEqual(runner.failures, [])
        updated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(set(updated["mcpServers"]), {"first-alias", "second-alias", "invalid"})
        self.assertEqual(updated["mcpServers"]["first-alias"], original["mcpServers"]["first-alias"])
        self.assertEqual(updated["mcpServers"]["second-alias"], {
            "type": "stdio", "command": "python3", "args": ["-m", "fixture_server"],
            "cwd": "/workspace/second", "timeout": 30,
            "env": {"TOKEN": "REQUIRED_TOKEN_FROM_ENV", "EXTRA": "EXTRA_TOKEN_FROM_ENV"},
            "providerOptions": {"owner": "user"},
        })
        self.assertIsNone(updated["mcpServers"]["invalid"])
        self.assertEqual(updated["customTopLevel"], {"owner": "user"})
        self.assertEqual(next(self.target.parent.glob("mcp.json.bak.*")).read_bytes(), before)
        self.assertTrue(mcp.mcp_status(manifest)[0])
        converged = self.target.read_bytes()
        self.assertFalse(self.sync(manifest)[0])
        self.assertEqual(self.target.read_bytes(), converged)
        self.assertEqual(len(list(self.target.parent.glob("mcp.json.bak.*"))), 1)

    def test_required_credentials_are_corrected_preserving_extra_references(self) -> None:
        for section, transport, identity in (
            ("headers", "http", {"url": "https://managed.example.test/mcp"}),
            ("env", "stdio", {"command": "python3", "args": ["-m", "fixture_server"]}),
        ):
            for current in ({"EXTRA": "EXTRA_TOKEN_FROM_ENV"},
                            {"TOKEN": "OLD_TOKEN_FROM_ENV", "EXTRA": "EXTRA_TOKEN_FROM_ENV"}):
                with self.subTest(section=section, current=current):
                    for backup in self.target.parent.glob("mcp.json.bak.*"):
                        backup.unlink()
                    self.target.write_text(json.dumps({"mcpServers": {"provider-alias": {
                        **identity, section: current, "providerOptions": {"owner": "user"},
                    }}}), encoding="utf-8")
                    before = self.target.read_bytes()
                    manifest = self.manifest({"managed": {
                        **identity, "type": transport, section: {"TOKEN": "REQUIRED_TOKEN_FROM_ENV"},
                    }})
                    self.assertFalse(mcp.mcp_status(manifest)[0])
                    changed, runner = self.sync(manifest, True)
                    self.assertTrue(changed)
                    self.assertEqual(runner.failures, [])
                    self.assert_preserved(before)
                    changed, runner = self.sync(manifest)
                    self.assertTrue(changed)
                    self.assertEqual(runner.failures, [])
                    updated = json.loads(self.target.read_text(encoding="utf-8"))
                    self.assertEqual(updated["mcpServers"], {"provider-alias": {
                        **identity, "type": transport,
                        section: {"TOKEN": "REQUIRED_TOKEN_FROM_ENV", "EXTRA": "EXTRA_TOKEN_FROM_ENV"},
                        "providerOptions": {"owner": "user"},
                    }})
                    self.assertEqual(next(self.target.parent.glob("mcp.json.bak.*")).read_bytes(), before)
                    self.assertTrue(mcp.mcp_status(manifest)[0])
                    converged = self.target.read_bytes()
                    self.assertFalse(self.sync(manifest)[0])
                    self.assertEqual(self.target.read_bytes(), converged)
                    self.assertEqual(len(list(self.target.parent.glob("mcp.json.bak.*"))), 1)

    def test_external_only_required_credential_drift_remains_a_conflict(self) -> None:
        provider = self.home / ".config" / "opencode" / "opencode.json"
        provider.parent.mkdir(parents=True)
        for section, transport, identity in (
            ("headers", "http", {"url": "https://managed.example.test/mcp"}),
            ("env", "stdio", {"command": "python3", "args": ["-m", "fixture_server"]}),
        ):
            for current in ({"EXTRA": "EXTRA_TOKEN_FROM_ENV"},
                            {"TOKEN": "OLD_TOKEN_FROM_ENV", "EXTRA": "EXTRA_TOKEN_FROM_ENV"}):
                with self.subTest(section=section, current=current):
                    self.target.write_bytes(b'{"mcpServers":{"personal":{"url":"https://personal.example.test/mcp"}}}')
                    before = self.target.read_bytes()
                    provider.write_text(json.dumps({"mcp": {"external-alias": {
                        **identity, section: current,
                    }}}), encoding="utf-8")
                    provider_bytes = provider.read_bytes()
                    manifest = self.manifest({"managed": {
                        **identity, "type": transport, section: {"TOKEN": "REQUIRED_TOKEN_FROM_ENV"},
                    }})
                    self.assertFalse(mcp.mcp_status(manifest)[0])
                    for dry_run in (True, False):
                        changed, runner = self.sync(manifest, dry_run)
                        self.assertFalse(changed)
                        self.assertTrue(runner.failures)
                        self.assert_preserved(before)
                        self.assertEqual(provider.read_bytes(), provider_bytes)
                        self.assertFalse(mcp.mcp_status(manifest)[0])


if __name__ == "__main__":
    unittest.main()
