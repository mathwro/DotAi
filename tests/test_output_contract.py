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

from dotai_app import recommendations, routing, runtime, terminal


class OutputContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        patch = mock.patch.dict(os.environ, {
            "DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state"),
            "HOME": str(self.home), "USERPROFILE": str(self.home), "NO_COLOR": "1",
        })
        patch.start()
        self.addCleanup(patch.stop)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)
        self.platform = "windows" if os.name == "nt" else "linux"

    def script(self, code: str) -> list[str]:
        path = self.home / "tool.py"
        path.write_text(code, encoding="utf-8")
        return [sys.executable, str(path)]

    def harness(self, command: list[str], *, verbose: bool = False, interactive: bool = False) -> list[str]:
        code = (
            "from dotai_app.runtime import Runner; "
            f"r=Runner({self.platform!r}, verbose={verbose!r}); "
            f"r.run({command!r}, 'Install isolated component', interactive={interactive!r})"
        )
        return [sys.executable, "-c", code]

    def test_success_captures_both_streams_without_external_chatter(self) -> None:
        # Inheriting either stream leaks installer output; returned streams remain usable.
        command = self.script("import sys; print('npx-stdout-noise'); print('npx-stderr-noise', file=sys.stderr)\n")
        runner = runtime.Runner(self.platform)
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            result = runner.run(command, "Install example")
        self.assertEqual(result.stdout.strip(), "npx-stdout-noise")
        self.assertEqual(result.stderr.strip(), "npx-stderr-noise")
        self.assertIn("Install example", report.getvalue())
        self.assertIn("completed", report.getvalue().lower())
        self.assertNotIn("npx-", report.getvalue())
        observed = subprocess.run(self.harness(command), cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertNotIn("npx-", observed.stdout + observed.stderr)

    def test_progress_is_flushed_before_subprocess_completes(self) -> None:
        release = self.home / "release"
        command = self.script(
            "import time\nfrom pathlib import Path\n"
            f"p=Path({str(release)!r})\n"
            "deadline=time.monotonic()+3\n"
            "while not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "raise SystemExit(0 if p.exists() else 9)\n"
        )
        child = subprocess.Popen(self.harness(command), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = child.stdout.readline()
            self.assertIn("Install isolated component", first)
            release.write_text("continue", encoding="utf-8")
            rest, errors = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, rest + errors)
            self.assertNotIn("exit 9", rest)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

    def test_failure_retains_cause_exit_and_next_step_without_secret(self) -> None:
        command = self.script("import os,sys; print('request log'); print('Permission denied '+os.environ['API_TOKEN'], file=sys.stderr); sys.exit(7)\n")
        runner = runtime.Runner(self.platform)
        runner.env["API_TOKEN"] = "synthetic-secret-value"
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            result = runner.run(command, "Install example")
        self.assertEqual(result.returncode, 7)
        self.assertIn("synthetic-secret-value", result.stderr)
        self.assertEqual(len(runner.failures), 1)
        combined = report.getvalue() + runner.failures[0]
        self.assertIn("Permission denied", combined)
        self.assertIn("exit 7", combined)
        self.assertIn("Next step", combined)
        self.assertNotIn("synthetic-secret-value", combined)

    def test_optional_failure_remains_visible_and_accounted(self) -> None:
        command = self.script("raise SystemExit(3)\n")
        runner = runtime.Runner(self.platform)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            runner.run(command, "Optional probe", required=False)
            runner.summary()
        self.assertEqual(runner.failures, [])
        self.assertIn("FAIL", report.getvalue())
        self.assertIn("1 failed", report.getvalue().lower())

    def test_verbose_redacts_env_command_header_and_json_diagnostics(self) -> None:
        command = self.script(
            "import os,sys\nprint('diagnostic context')\n"
            "print(os.environ['ACCESS_TOKEN'])\n"
            "print('Authorization: Bearer header-secret')\n"
            "print('{\"models\":[{\"apiKey\":\"json-secret\"}]}')\n"
        ) + ["--password", "argument-secret", "--header", "X-Api-Key: custom-header-secret"]
        runner = runtime.Runner(self.platform, verbose=True)
        runner.env["ACCESS_TOKEN"] = "environment-secret"
        with contextlib.redirect_stdout(io.StringIO()) as report:
            runner.run(command, "Configure example", capture=True)
        text = report.getvalue()
        self.assertIn("diagnostic context", text)
        self.assertIn("Command", text)
        for secret in ("environment-secret", "argument-secret", "header-secret", "custom-header-secret", "json-secret"):
            self.assertNotIn(secret, text)
        self.assertNotIn('{"models"', text)

    def test_known_command_secret_is_redacted_when_echoed_without_key(self) -> None:
        command = self.script("import sys; print(sys.argv[2]); print('connection failed', file=sys.stderr); sys.exit(1)\n") + ["--token", "echo-secret"]
        runner = runtime.Runner(self.platform, verbose=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Authenticate")
        self.assertIn("echo-secret", result.stdout)
        self.assertNotIn("echo-secret", report.getvalue() + " ".join(runner.failures))

    def test_dry_run_never_executes_and_does_not_show_commands_normally(self) -> None:
        marker = self.home / "marker"
        runner = runtime.Runner(self.platform, dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], "Install example")
            runner.summary()
        self.assertIsNone(result)
        self.assertFalse(marker.exists())
        self.assertIn("Would", report.getvalue())
        self.assertNotIn("open(", report.getvalue())
        self.assertNotIn("1 changed", report.getvalue())

    def test_interactive_execution_is_explicit_and_inherits_streams(self) -> None:
        command = self.script("print('interactive prompt output')\n")
        observed = subprocess.run(self.harness(command, interactive=True), cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertIn("interactive prompt output", observed.stdout)
        self.assertIn("interactive", observed.stdout.lower())

    def test_launch_failure_is_actionable_and_sanitized(self) -> None:
        runner = runtime.Runner(self.platform)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run([str(self.home / "missing-tool")], "Start missing tool")
        self.assertIsNone(result)
        self.assertIn("Next step", report.getvalue())
        self.assertIn("missing tool", runner.failures[0])

    def test_outcome_summary_reports_partial_completion_without_false_success(self) -> None:
        runner = runtime.Runner(self.platform)
        runner.record_outcome("One", "changed")
        runner.record_outcome("Two", "unchanged")
        runner.record_outcome("Three", "skipped", "disabled")
        runner.record_outcome("Four", "failed", "permission denied")
        with contextlib.redirect_stdout(io.StringIO()) as report:
            runner.summary()
        for phrase in ("1 changed", "1 unchanged", "1 skipped", "1 failed", "partial"):
            self.assertIn(phrase, report.getvalue().lower())

    def test_change_preview_describes_fields_without_disclosing_values(self) -> None:
        before = {"packages": [{"name": "Example", "enabled": True}], "mcp": {"servers": {}}}
        after = {"packages": [{"name": "Example", "enabled": False}], "mcp": {"servers": {"remote": {
            "type": "http", "url": "https://example.invalid?token=query-secret",
            "headers": {"Authorization": "literal-secret"}, "env": {"API_TOKEN": "env-secret"},
        }}}}
        text = terminal.describe_changes(before, after, self.home / "stack.json")
        for label in ("Example", "enabled", "remote", "stack.json"):
            self.assertIn(label, text)
        for secret in ("literal-secret", "env-secret", "query-secret"):
            self.assertNotIn(secret, text)
        self.assertNotIn('"packages"', text)
        self.assertEqual(terminal.describe_changes(before, before, "stack.json"), "")

    def test_redaction_preserves_names_but_hides_supported_credential_forms(self) -> None:
        text = terminal.redact(
            "API_TOKEN=env-secret --password=arg-secret Authorization: Bearer header-secret "
            "https://user:url-secret@example.invalid?api_key=query-secret "
            "Cookie: session=cookie-secret\n{\"clientSecret\":\"json-secret\"} known-secret",
            secrets=("known-secret",),
        )
        for secret in ("env-secret", "arg-secret", "header-secret", "url-secret", "query-secret", "cookie-secret", "json-secret", "known-secret"):
            self.assertNotIn(secret, text)
        self.assertIn("API_TOKEN", text)
        self.assertIn("example.invalid", text)

    def test_routing_preview_has_roles_and_actions_not_protocol_or_unmanaged_values(self) -> None:
        catalog = {"version": 1, "agentModelOverrides": {"worker": "@task"}, "providers": {
            "anthropic": {"roles": {role: ["anthropic/model"] for role in routing.ROUTING_ROLES}}
        }}
        manifest = {"ompRouting": None}
        records = {"modelRoles": {"custom": "unmanaged-secret"}, "retry.fallbackChains": {}, "task.agentModelOverrides": {}}
        values = {**records, "retry.modelFallback": False, "retry.usageAwareFallback": False,
                  "retry.usageReservePct": 0, "retry.usageReservePolicy": "auto", "retry.fallbackRevertPolicy": "cooldown-expiry"}
        runner = runtime.Runner(self.platform, dry_run=True)
        def output(command):
            if command == ["omp", "models", "--json"]:
                return json.dumps({"models": [{"selector": "anthropic/model"}]})
            return json.dumps({"value": values[command[3]]})
        with mock.patch.object(routing, "load_routing_recommendations", return_value=catalog), \
             mock.patch.object(runner, "output", side_effect=output), \
             contextlib.redirect_stdout(io.StringIO()) as report:
            status = routing.configure_omp_routing(manifest, self.home / "stack.json", runner)
        self.assertEqual(status, 0)
        self.assertIn("anthropic/model", report.getvalue())
        self.assertIn("no manifest or OMP changes applied", report.getvalue())
        self.assertNotIn("omp config set", report.getvalue())
        self.assertNotIn("unmanaged-secret", report.getvalue())
        self.assertNotIn('{"', report.getvalue())

    def test_shell_command_diagnostics_hide_headers_env_and_embedded_configuration(self) -> None:
        runner = runtime.Runner(self.platform, verbose=True)
        command = """tool -H 'X-Trace: header-secret' --env 'CUSTOM_VALUE=env-secret' --config '{"ordinary":"private-value"}'"""
        text = runner.display_command(command)
        for secret in ("header-secret", "env-secret", "private-value"):
            self.assertNotIn(secret, text)
        self.assertNotIn('{"', text)

    def test_verbose_omits_pretty_protocol_after_text_but_retains_bracketed_errors(self) -> None:
        runner = runtime.Runner(self.platform, verbose=True)
        command = self.script(
            "print('protocol follows')\n"
            "print('{\\n  \"internal_record\": \"private-value\"\\n}')\n"
            "print('[ERROR] Permission denied')\n"
            "raise SystemExit(4)\n"
        )
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Configure example")
        self.assertEqual(result.returncode, 4)
        self.assertIn("private-value", result.stdout)
        self.assertIn("Permission denied", report.getvalue())
        self.assertNotIn("internal_record", report.getvalue())
        self.assertNotIn("private-value", report.getvalue())

    def test_malformed_protocol_never_exposes_unknown_fields_in_failure_or_verbose_output(self) -> None:
        command = self.script(
            "import sys\n"
            "print('[ERROR] Permission denied', file=sys.stderr)\n"
            "print('{\\n  \"value\": \"private-response-data\"', file=sys.stderr)\n"
            "raise SystemExit(8)\n"
        )
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                runner = runtime.Runner(self.platform, verbose=verbose)
                with contextlib.redirect_stdout(io.StringIO()) as report:
                    result = runner.run(command, "Configure example")
                self.assertEqual(result.returncode, 8)
                self.assertTrue(runner.failures)
                self.assertIn("Permission denied", report.getvalue())
                self.assertNotIn("private-response-data", report.getvalue() + " ".join(runner.failures))
                self.assertNotIn('"value"', report.getvalue())

    def test_malformed_array_preserves_following_bracketed_error(self) -> None:
        command = self.script(
            "import sys\n"
            "print('[{\\n  \"value\": \"private-array-data\"', file=sys.stderr)\n"
            "print('[ERROR] Target refused', file=sys.stderr)\n"
            "raise SystemExit(8)\n"
        )
        runner = runtime.Runner(self.platform, verbose=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            runner.run(command, "Configure example")
        self.assertTrue(runner.failures)
        self.assertIn("Target refused", report.getvalue())
        self.assertNotIn("private-array-data", report.getvalue() + " ".join(runner.failures))

    def test_truncated_json_string_does_not_turn_embedded_log_label_into_prose(self) -> None:
        command = self.script(
            "import sys\n"
            "print('{\"value\": \"\\n[ERROR] private-string-data', file=sys.stderr)\n"
            "raise SystemExit(8)\n"
        )
        runner = runtime.Runner(self.platform, verbose=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Configure example")
        self.assertEqual(result.returncode, 8)
        self.assertTrue(runner.failures)
        self.assertNotIn("private-string-data", report.getvalue() + " ".join(runner.failures))

    def test_structured_failure_explains_available_error_without_dumping_payload(self) -> None:
        runner = runtime.Runner(self.platform)
        command = self.script(
            "import json,sys\n"
            "print(json.dumps({'error': {'message': 'Permission denied to target', 'token': 'json-secret'}}), file=sys.stderr)\n"
            "raise SystemExit(8)\n"
        )
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Configure example")
        self.assertEqual(result.returncode, 8)
        self.assertIn("Permission denied to target", report.getvalue())
        self.assertNotIn("json-secret", report.getvalue())
        self.assertNotIn('{"error"', report.getvalue())

    def test_url_command_password_remains_redacted_when_echoed_without_url(self) -> None:
        runner = runtime.Runner(self.platform, verbose=True)
        command = self.script(
            "from urllib.parse import urlsplit\nimport sys\n"
            "print(urlsplit(sys.argv[2]).password)\n"
        ) + ["--url", "https://user:url-secret@example.invalid/path"]
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Download component")
        self.assertIn("url-secret", result.stdout)
        self.assertNotIn("url-secret", report.getvalue())


if __name__ == "__main__":
    unittest.main()
