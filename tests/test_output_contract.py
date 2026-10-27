from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import manifest as manifests, recommendations, runtime, skills, terminal


class OutputContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        patch = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "DOTAI_HOME": str(self.home), "DOTAI_STATE_DIR": str(self.home / "state"),
            "XDG_STATE_HOME": str(self.home / "xdg"), "NO_COLOR": "1",
        })
        patch.start()
        self.addCleanup(patch.stop)
        color = mock.patch.object(terminal, "_COLOR_ENABLED", False)
        color.start()
        self.addCleanup(color.stop)

    def child(self, code: str) -> list[str]:
        script = self.home / "child.py"
        script.write_text(code, encoding="utf-8")
        return [sys.executable, str(script)]

    def test_real_child_streams_remain_separate_and_noninteractive_input_is_closed(self) -> None:
        command = self.child("import sys\nprint('{}')\nprint('warning', file=sys.stderr)\nassert sys.stdin.read() == ''\n")
        for capture in (False, True):
            with self.subTest(capture=capture), contextlib.redirect_stdout(io.StringIO()) as report:
                result = runtime.Runner("linux").run(command, "Read protocol", capture=capture)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "{}\n")
                self.assertEqual(result.stderr, "warning\n")
                self.assertNotIn("warning", report.getvalue())
                self.assertNotIn(str(command), report.getvalue())

    def test_outer_process_captures_no_inherited_chatter(self) -> None:
        command = self.child("import sys\nprint('upstream-out')\nprint('upstream-err', file=sys.stderr)\n")
        bootstrap = (
            "from dotai_app.runtime import Runner; "
            f"Runner('linux').run({command!r}, 'Install fixture')"
        )
        result = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, env=os.environ.copy(),
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install fixture", result.stdout)
        self.assertNotIn("upstream-", result.stdout + result.stderr)
        self.assertNotIn(str(command[-1]), result.stdout)

    def test_progress_is_flushed_before_child_starts(self) -> None:
        gate = self.home / "flushed"
        command = self.child(f"from pathlib import Path\nassert Path({str(gate)!r}).exists()\n")

        class Progress(io.StringIO):
            def flush(inner_self) -> None:
                if "Install fixture" in inner_self.getvalue():
                    gate.touch()
                super().flush()

        with contextlib.redirect_stdout(Progress()):
            result = runtime.Runner("linux").run(command, "Install fixture")
        self.assertEqual(result.returncode, 0)

    def test_failure_reports_exit_cause_and_next_step_without_protocol_or_credentials(self) -> None:
        secret = secrets.token_urlsafe(32)
        command = self.child(
            "import os, sys\nprint('{\"private\": \"config-value\"}')\n"
            "print('Permission denied: ' + os.environ['OUTPUT_API_TOKEN'], file=sys.stderr)\n"
            "sys.exit(7)\n"
        )
        for required in (False, True):
            runner = runtime.Runner("linux")
            runner.env["OUTPUT_API_TOKEN"] = secret
            with contextlib.redirect_stdout(io.StringIO()) as report:
                result = runner.run(command, "Install fixture", required=required)
            self.assertEqual(result.returncode, 7)
            self.assertIn(secret, result.stderr)
            self.assertEqual(len(runner.failures), int(required))
            text = report.getvalue() + "\n".join(runner.failures)
            self.assertIn("exit 7", text)
            self.assertIn("Permission denied", text)
            self.assertIn("permissions", text.lower())
            self.assertNotIn(secret, text)
            self.assertNotIn("config-value", text)

    def test_verbose_diagnostics_are_bounded_redacted_and_keep_return_streams_raw(self) -> None:
        secret = secrets.token_urlsafe(32)
        command = self.child(
            "import os, sys\nprint(os.environ['OUTPUT_API_TOKEN'])\n"
            "print('{\"private\": \"config-value\"}')\n"
            "print('x' * 20000)\nprint('diagnostic-warning', file=sys.stderr)\n"
        )
        runner = runtime.Runner("linux", verbose=True)
        runner.env["OUTPUT_API_TOKEN"] = secret
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Install fixture")
        self.assertIn(secret, result.stdout)
        self.assertIn("config-value", result.stdout)
        self.assertIn("diagnostic-warning", report.getvalue())
        self.assertNotIn(secret, report.getvalue())
        self.assertNotIn("config-value", report.getvalue())
        self.assertLess(len(report.getvalue()), 6000)

    def test_command_credentials_are_redacted_when_echoed_without_labels(self) -> None:
        secret = secrets.token_urlsafe(32)
        command = self.child("import sys\nprint(sys.argv[-1])\nsys.exit(3)\n") + ["--api-key", secret]
        runner = runtime.Runner("linux", verbose=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Authentication probe")
        self.assertIn(secret, result.stdout)
        self.assertNotIn(secret, report.getvalue() + "".join(runner.failures))

    def test_redaction_handles_references_headers_json_assignments_and_url_credentials(self) -> None:
        secret = secrets.token_urlsafe(32)
        cases = [f"--token {secret}", f"api_key={secret}",
                 f"Authorization: Bearer {secret}", f"Cookie: session={secret}",
                 f"https://user:{secret}@example.invalid/path", json.dumps({"password": secret}),
                 f"https://example.invalid/?access_token={secret}&page=2"]
        for text in cases:
            with self.subTest(text=text):
                self.assertNotIn(secret, terminal.redact(text))
        with mock.patch.dict(os.environ, {"OUTPUT_API_TOKEN": secret}):
            self.assertNotIn(secret, terminal.redact(secret))
            self.assertEqual(terminal.redact("OUTPUT_API_TOKEN"), "OUTPUT_API_TOKEN")

    def test_failure_uses_stdout_cause_when_stderr_is_blank(self) -> None:
        command = self.child("import sys\nprint('package not found')\nprint('  ', file=sys.stderr)\nsys.exit(4)\n")
        runner = runtime.Runner("linux")
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.run(command, "Install fixture")
        self.assertEqual(result.returncode, 4)
        self.assertIn("package not found", report.getvalue())
        self.assertIn("exit 4", runner.failures[0])

    def test_header_and_environment_arguments_protect_unlabeled_echoes(self) -> None:
        secret = secrets.token_urlsafe(32)
        for flag, payload in (("--header", f"X-Custom: {secret}"), ("--env", f"CUSTOM={secret}")):
            command = self.child("import sys\nprint(sys.argv[-1].replace('X-Custom: ', '').replace('CUSTOM=', ''))\nsys.exit(2)\n") + [flag, payload]
            with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()) as report:
                result = runtime.Runner("linux", verbose=True).run(command, "Echo fixture")
            self.assertIn(secret, result.stdout)
            self.assertNotIn(secret, report.getvalue())

    def test_structured_command_arguments_are_not_dumped_even_when_verbose(self) -> None:
        command = ["omp", "config", "set", "extensions", json.dumps(["private-extension-value"])]
        runner = runtime.Runner("linux", dry_run=True, verbose=True)
        with contextlib.redirect_stdout(io.StringIO()) as report:
            self.assertIsNone(runner.run(command, "Configure extensions"))
        self.assertNotIn("private-extension-value", report.getvalue())
        self.assertEqual(runner.failures, [])
        self.assertFalse((self.home / "state").exists())

    def test_explicit_interaction_preserves_input_and_cannot_capture(self) -> None:
        command = self.child("print('answer=' + input())\n")
        bootstrap = (
            "from dotai_app.runtime import Runner; "
            f"Runner('linux').run({command!r}, 'Interactive fixture', interactive=True)"
        )
        result = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, env=os.environ.copy(),
                                input="accepted\n", capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("answer=accepted", result.stdout)
        with self.assertRaises(ValueError):
            runtime.Runner("linux").run(command, "Invalid", capture=True, interactive=True)

    def test_dry_run_never_executes_mutating_child(self) -> None:
        marker = self.home / "mutated"
        command = self.child(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with contextlib.redirect_stdout(io.StringIO()):
            result = runtime.Runner("linux", dry_run=True).run(command, "Planned mutation")
        self.assertIsNone(result)
        self.assertFalse(marker.exists())

    def test_verbose_probe_keeps_stdout_protocol_and_sanitizes_stderr(self) -> None:
        secret = secrets.token_urlsafe(32)
        command = self.child("import os, sys\nprint('{\"value\":\"private-config\"}')\nprint(os.environ['OUTPUT_API_TOKEN'], file=sys.stderr)\n")
        runner = runtime.Runner("linux", verbose=True)
        runner.env["OUTPUT_API_TOKEN"] = secret
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = runner.output(command)
        self.assertEqual(json.loads(result), {"value": "private-config"})
        self.assertNotIn("private-config", report.getvalue())
        self.assertNotIn(secret, report.getvalue())
        self.assertEqual(runner.failures, [])

    def test_recommendation_preview_names_selections_without_dumping_manifest_values(self) -> None:
        secret = secrets.token_urlsafe(32)
        before = {"source": "owner/recommended", "agent": "universal", "skills": ["old"], "checkSkills": ["old"], "extra": secret}
        after = {"source": "owner/recommended", "agent": "universal", "skills": ["new"], "checkSkills": ["new"]}
        custom = {"source": "owner/custom", "agent": "universal", "skills": ["custom"], "checkSkills": ["custom"]}
        manifest = {
            "version": 1, "packages": [], "skills": [before, custom], "marketplaces": [], "plugins": [],
            "mcp": {"target": str(self.home / "mcp.json"), "servers": {"example": {
                "type": "http", "url": "https://example.invalid/mcp", "headers": {"API_TOKEN": "OUTPUT_API_TOKEN"},
            }}},
        }
        path = self.home / "stack.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        original = path.read_bytes()
        example = self.home / "example.json"
        example.write_text(json.dumps({**manifest, "skills": [after]}), encoding="utf-8")
        with mock.patch.object(manifests, "EXAMPLE_MANIFEST", example), contextlib.redirect_stdout(io.StringIO()) as report:
            proposed, _ = recommendations.review_recommended_skills(manifest, path, runtime.Runner("linux", dry_run=True), enforce=True)
        self.assertEqual(proposed["skills"], [after])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.home.glob("stack.json.bak.*")))
        self.assertFalse((self.home / "state").exists())
        for expected in ("owner/recommended", "old", "new", "owner/custom", "conditional"):
            self.assertIn(expected, report.getvalue())
        self.assertNotIn(secret, report.getvalue())
        self.assertNotIn("OUTPUT_API_TOKEN", report.getvalue())
        self.assertNotIn('"skills":', report.getvalue())

    def test_migration_preview_shows_agent_change_without_unrelated_config(self) -> None:
        manifest = {"skills": [{"source": "owner/skills", "agent": "pi", "skills": ["review"], "notes": "private-config"}]}
        path = self.home / "stack.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        original = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as report:
            result = skills.fix_legacy_skills(manifest, path, runtime.Runner("linux", dry_run=True))
        self.assertEqual(result, 0)
        self.assertEqual(path.read_bytes(), original)
        for expected in ("owner/skills", "review", "pi", "universal"):
            self.assertIn(expected, report.getvalue())
        self.assertNotIn("private-config", report.getvalue())


if __name__ == "__main__":
    unittest.main()
