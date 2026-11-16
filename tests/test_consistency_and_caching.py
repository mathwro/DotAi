from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import cli, integrations, manifest as manifests, releases, runtime, terminal


def minimal_manifest() -> dict:
    return {
        "version": 1,
        "packages": [],
        "skills": [],
        "marketplaces": [],
        "plugins": [],
        "ompExtensions": [],
        "ompRouting": None,
        "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}},
    }


class ConsistencyAndCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.state_dir = self.root / "state"
        self.home.mkdir()
        self.path = self.root / "stack.json"
        patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "DOTAI_HOME": str(self.home),
                "DOTAI_STATE_DIR": str(self.state_dir),
                "XDG_STATE_HOME": str(self.root / "xdg"),
                "GH_HOST": "github.com",
                "NO_COLOR": "1",
                "DOTAI_PLATFORM": "linux",
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("DOTAI_NO_UPDATE_CHECK", None)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)

    def test_add_backs_up_existing_manifest_privately_and_skips_redundant_backup_when_unchanged(self) -> None:
        initial = minimal_manifest()
        initial["plugins"] = [{"id": "old@team", "scope": "user"}]
        original_bytes = (json.dumps(initial, indent=4) + "\n").encode("utf-8")
        self.path.write_bytes(original_bytes)
        if os.name != "nt":
            self.path.chmod(0o644)

        first_out = io.StringIO()
        with contextlib.redirect_stdout(first_out):
            self.assertEqual(cli.main(["--manifest", str(self.path), "add", "plugin", "review@team"]), 0)
        self.assertIn("[OK]", first_out.getvalue())
        backups = list(self.root.glob("stack.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_bytes)
        if os.name != "nt":
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

        saved_bytes = self.path.read_bytes()
        second_out = io.StringIO()
        with contextlib.redirect_stdout(second_out):
            self.assertEqual(cli.main(["--manifest", str(self.path), "add", "plugin", "review@team"]), 0)
        self.assertIn("[OK]", second_out.getvalue())
        self.assertEqual(list(self.root.glob("stack.json.bak.*")), backups)
        self.assertEqual(self.path.read_bytes(), saved_bytes)

    def test_add_integration_writes_new_manifest_without_backup_when_path_absent(self) -> None:
        target = self.root / "new-stack.json"
        candidate = minimal_manifest()
        args = cli.build_parser().parse_args(["add", "marketplace", "team", "owner/market"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(integrations.add_integration(args, candidate, target), 0)
        self.assertTrue(target.exists())
        self.assertEqual(list(self.root.glob("new-stack.json.bak.*")), [])
        self.assertIn("[OK]", out.getvalue())

    def test_parse_platform_commands_supports_unix_platform_and_add_tool(self) -> None:
        self.path.write_text(json.dumps(minimal_manifest()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main([
                    "--manifest", str(self.path), "add", "tool", "Sample",
                    "--check", "sample --version",
                    "--install", "unix=sh install.sh",
                    "--update", "unix=sh update.sh",
                ]),
                0,
            )
        saved = manifests.load_manifest(self.path)
        self.assertEqual(saved["packages"][0]["install"], {"unix": ["sh install.sh"]})
        self.assertEqual(saved["packages"][0]["update"], {"unix": ["sh update.sh"]})
        self.assertEqual(runtime.selected(saved["packages"][0]["install"], "macos"), ["sh install.sh"])

    def test_plugin_ids_align_between_runtime_validator_and_schema(self) -> None:
        schema = json.loads((ROOT / "stack.schema.json").read_text(encoding="utf-8"))
        pattern = schema["properties"]["plugins"]["items"]["properties"]["id"]["pattern"]
        self.assertEqual(pattern, "^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
        self.path.write_text(json.dumps(minimal_manifest()), encoding="utf-8")
        for valid_id in ("Review_Plugin-1.0@Team_Market-2", "review@team", "UPPER_CASE@ORG_NAME"):
            with self.subTest(valid_id=valid_id), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(["--manifest", str(self.path), "add", "plugin", valid_id]),
                    0,
                )
        for invalid_id in ("bad", "space name@team", "plugin@two@at", "@team", "plugin@", "plugin@team\n"):
            with self.subTest(invalid_id=invalid_id), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli.main(["--manifest", str(self.path), "add", "plugin", invalid_id]),
                    2,
                )

    def test_mcp_server_type_defaults_to_http_when_url_present_and_rejects_ambiguous_servers(self) -> None:
        candidate = minimal_manifest()
        candidate["mcp"]["servers"] = {
            "remote-default": {"url": "https://example.test/mcp", "headers": {"X-Test": "TOKEN_REF"}},
            "stdio-default": {"command": "npx", "args": ["-y", "pkg"]},
        }
        validated = manifests.validate_manifest(candidate)
        self.assertEqual(validated["mcp"]["servers"]["remote-default"]["url"], "https://example.test/mcp")

        invalid_cases = {
            "empty": {},
            "ambiguous-both-without-type": {"command": "npx", "url": "https://example.test/mcp"},
            "explicit-stdio-with-url-no-command": {"type": "stdio", "url": "https://example.test/mcp"},
            "explicit-http-with-command-no-url": {"type": "http", "command": "npx"},
            "omitted-type-bad-url": {"url": "not a url"},
        }
        for case, server in invalid_cases.items():
            with self.subTest(case=case):
                broken = minimal_manifest()
                broken["mcp"]["servers"] = {"broken": server}
                with self.assertRaises(runtime.DotAiError):
                    manifests.validate_manifest(broken)

        schema = json.loads((ROOT / "stack.schema.json").read_text(encoding="utf-8"))
        mcp_branches = schema["definitions"]["mcpServer"]["oneOf"]
        remote_branch = next(branch for branch in mcp_branches if "url" in branch["required"])
        stdio_branch = next(branch for branch in mcp_branches if "command" in branch["required"])
        self.assertNotIn("type", remote_branch["required"])
        self.assertEqual(stdio_branch.get("not"), {"required": ["url"]})
        self.assertEqual(remote_branch.get("not"), {"required": ["command"]})

    def test_add_tool_supports_minimum_version_and_configure_commands(self) -> None:
        self.path.write_text(json.dumps(minimal_manifest()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main([
                    "--manifest", str(self.path), "add", "tool", "RtkLike",
                    "--check", "rtk --version",
                    "--minimum-version", "0.43.1",
                    "--install", "linux=sh install.sh",
                    "--configure", "default=rtk init -g --agent pi",
                    "--configure", "unix=rtk completion",
                ]),
                0,
            )
        saved = manifests.load_manifest(self.path)
        self.assertEqual(
            saved["packages"],
            [{
                "name": "RtkLike",
                "check": "rtk --version",
                "minimumVersion": "0.43.1",
                "install": {"linux": ["sh install.sh"]},
                "configure": {
                    "default": ["rtk init -g --agent pi"],
                    "unix": ["rtk completion"],
                },
            }],
        )
        before = self.path.read_bytes()
        backups_before = list(self.root.glob("stack.json.bak.*"))
        for invalid in (
            ["--minimum-version", "v1.2.3", "--install", "linux=sh i.sh"],
            ["--minimum-version", "01.2", "--install", "linux=sh i.sh"],
            ["--minimum-version", "", "--install", "linux=sh i.sh"],
            ["--configure", "unknown=cmd", "--install", "linux=sh i.sh"],
            ["--configure", "linux=", "--install", "linux=sh i.sh"],
        ):
            with self.subTest(invalid=invalid), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli.main(["--manifest", str(self.path), "add", "tool", "Bad", "--check", "bad", *invalid]),
                    2,
                )
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.root.glob("stack.json.bak.*")), backups_before)

    def test_add_mcp_supports_cwd_and_timeout_with_transport_validation(self) -> None:
        self.path.write_text(json.dumps(minimal_manifest()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main([
                    "--manifest", str(self.path), "add", "mcp", "local-srv",
                    "--command", "npx", "--arg=-y", "--arg", "pkg",
                    "--cwd", "/srv/mcp", "--timeout", "30",
                ]),
                0,
            )
            self.assertEqual(
                cli.main([
                    "--manifest", str(self.path), "add", "mcp", "http-srv",
                    "--url", "https://example.test/mcp", "--timeout", "0",
                ]),
                0,
            )
            self.assertEqual(
                cli.main([
                    "--manifest", str(self.path), "add", "mcp", "sse-srv",
                    "--url", "https://example.test/sse", "--transport", "sse", "--timeout", "45",
                ]),
                0,
            )
        servers = manifests.load_manifest(self.path)["mcp"]["servers"]
        self.assertEqual(
            servers["local-srv"],
            {"type": "stdio", "command": "npx", "args": ["-y", "pkg"], "cwd": "/srv/mcp", "timeout": 30},
        )
        self.assertEqual(
            servers["http-srv"],
            {"type": "http", "url": "https://example.test/mcp", "timeout": 0},
        )
        self.assertEqual(
            servers["sse-srv"],
            {"type": "sse", "url": "https://example.test/sse", "timeout": 45},
        )

        before = self.path.read_bytes()
        backups_before = list(self.root.glob("stack.json.bak.*"))
        for invalid in (
            ["add", "mcp", "bad", "--url", "https://example.test/mcp", "--cwd", "/srv/mcp"],
            ["add", "mcp", "bad", "--url", "https://example.test/mcp", "--cwd", ""],
            ["add", "mcp", "bad", "--command", "npx", "--timeout", "-1"],
            ["add", "mcp", "bad", "--url", "https://example.test/mcp", "--timeout", "-5"],
        ):
            with self.subTest(invalid=invalid), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--manifest", str(self.path), *invalid]), 2)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.root.glob("stack.json.bak.*")), backups_before)

    def test_release_notice_routes_through_terminal_say(self) -> None:
        with (
            mock.patch.object(releases, "VERSION", "1.0.0"),
            mock.patch.object(releases, "latest_release_version", return_value="1.1.0") as fetch,
            mock.patch.object(terminal, "say") as say_mock,
        ):
            releases.print_release_notice()
        say_mock.assert_called_once()
        self.assertIn("1.1.0", say_mock.call_args.args[0])
        fetch.assert_called_once()

    def test_release_notice_honors_opt_out_even_with_cached_update(self) -> None:
        for opt_out in ("1", "true", "TRUE", " 1 "):
            with (
                self.subTest(opt_out=opt_out),
                mock.patch.dict(os.environ, {"DOTAI_NO_UPDATE_CHECK": opt_out}),
                mock.patch.object(releases, "VERSION", "1.0.0"),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network reached despite opt-out")),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(["--color", "never", "version"]), 0)
            self.assertNotIn("[UPDATE]", out.getvalue())
            self.assertFalse((self.state_dir / "release-check.json").exists())

        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "release-check.json").write_text(
            json.dumps({"checkedAt": 9_999_999_999.0, "latestVersion": "2.0.0"}), encoding="utf-8"
        )
        with (
            mock.patch.dict(os.environ, {"DOTAI_NO_UPDATE_CHECK": "1"}),
            mock.patch.object(releases, "VERSION", "1.0.0"),
            contextlib.redirect_stdout(io.StringIO()) as cached_out,
        ):
            releases.print_release_notice()
        self.assertEqual(cached_out.getvalue(), "")

    def test_release_notice_caches_for_24h_refreshes_after_ttl_and_skips_dry_run_writes(self) -> None:
        cache_path = self.state_dir / "release-check.json"
        response_v1 = mock.MagicMock()
        response_v1.__enter__.return_value = response_v1
        response_v1.read.return_value = b'{"tag_name": "v2.0.0"}'

        with (
            mock.patch.object(releases, "VERSION", "1.5.0"),
            mock.patch("time.time", return_value=1_700_000_000.0),
            mock.patch("urllib.request.urlopen", return_value=response_v1) as urlopen_mock,
            contextlib.redirect_stdout(io.StringIO()) as first_out,
        ):
            releases.print_release_notice()
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertIn("2.0.0", first_out.getvalue())
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["latestVersion"], "2.0.0")
        self.assertIn("checkedAt", cached)

        # Fresh cache (< 24h) avoids network calls.
        with (
            mock.patch.object(releases, "VERSION", "1.5.0"),
            mock.patch("time.time", return_value=1_700_000_000.0 + 3600.0),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("fresh cache should skip network")),
            contextlib.redirect_stdout(io.StringIO()) as second_out,
        ):
            releases.print_release_notice()
        self.assertIn("2.0.0", second_out.getvalue())

        # Expired cache (>= 24h) refreshes from network.
        response_v2 = mock.MagicMock()
        response_v2.__enter__.return_value = response_v2
        response_v2.read.return_value = b'{"tag_name": "v2.1.0"}'
        with (
            mock.patch.object(releases, "VERSION", "1.5.0"),
            mock.patch("time.time", return_value=1_700_000_000.0 + 86_400.0),
            mock.patch("urllib.request.urlopen", return_value=response_v2) as refresh_mock,
            contextlib.redirect_stdout(io.StringIO()) as third_out,
        ):
            releases.print_release_notice()
        self.assertEqual(refresh_mock.call_count, 1)
        self.assertIn("2.1.0", third_out.getvalue())
        self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["latestVersion"], "2.1.0")

        # Dry-run CLI commands never mutate files or create release-check.json.
        dry_state = self.root / "dry-state"
        self.path.write_text(json.dumps(minimal_manifest()), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(dry_state)}),
            mock.patch.object(releases, "VERSION", "1.5.0"),
            mock.patch("urllib.request.urlopen", return_value=response_v2),
            contextlib.redirect_stdout(io.StringIO()) as dry_out,
        ):
            self.assertEqual(cli.main(["--manifest", str(self.path), "sync", "--dry-run"]), 0)
        self.assertIn("2.1.0", dry_out.getvalue())
        self.assertFalse(dry_state.exists())

        # 'dotai version' always checks fresh upstream and updates the cache.
        response_v3 = mock.MagicMock()
        response_v3.__enter__.return_value = response_v3
        response_v3.read.return_value = b'{"tag_name": "v2.2.0"}'
        with (
            mock.patch.object(releases, "VERSION", "1.5.0"),
            mock.patch("time.time", return_value=1_700_000_000.0 + 86_500.0),
            mock.patch("urllib.request.urlopen", return_value=response_v3) as version_mock,
            contextlib.redirect_stdout(io.StringIO()) as version_out,
        ):
            self.assertEqual(cli.main(["--color", "never", "version"]), 0)
        self.assertEqual(version_mock.call_count, 1)
        self.assertIn("2.2.0", version_out.getvalue())
        self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["latestVersion"], "2.2.0")

    def test_release_cache_tolerates_corrupt_files_and_write_failures_atomically(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.state_dir / "release-check.json"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"tag_name": "v1.4.0"}'

        for bad_payload in (b"{invalid", b"[]", b"\xff", b'{"checkedAt":"bad","latestVersion":"1.4.0"}', b'{"checkedAt":9999999999,"latestVersion":"bad"}'):
            with self.subTest(bad_payload=bad_payload):
                cache_path.write_bytes(bad_payload)
                with (
                    mock.patch.object(releases, "VERSION", "1.0.0"),
                    mock.patch("urllib.request.urlopen", return_value=response),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    releases.print_release_notice()
                self.assertIn("1.4.0", out.getvalue())
                self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["latestVersion"], "1.4.0")

        original_cache = cache_path.read_bytes()
        with (
            mock.patch.object(releases, "VERSION", "1.0.0"),
            mock.patch("time.time", return_value=9_999_999_999.0),
            mock.patch("urllib.request.urlopen", return_value=response),
            mock.patch("os.replace", side_effect=PermissionError("read-only state")),
            contextlib.redirect_stdout(io.StringIO()) as fail_out,
        ):
            releases.print_release_notice()
        self.assertIn("1.4.0", fail_out.getvalue())
        self.assertEqual(cache_path.read_bytes(), original_cache)
        self.assertEqual(set(self.state_dir.iterdir()), {cache_path})

    def test_cli_platform_override_accepts_unix(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["--platform", "unix", "platform"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "unix")


if __name__ == "__main__":
    unittest.main()
