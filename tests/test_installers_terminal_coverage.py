from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import catalog, packages, runtime, terminal


def recipe_package(recipe: str, name: str) -> dict:
    return catalog.materialize({
        "version": 2, "prerequisites": [], "packages": [
            {"name": name, "recipe": recipe, "managed": True},
        ], "skills": [], "marketplaces": [], "plugins": [], "ompExtensions": [],
        "mcp": {"target": "mcp.json", "servers": {}},
    }, "linux")["packages"][0]


class RtkShellFixture:
    """Real shell/tools, with only architecture, download, and success verification controlled."""

    def __init__(self, root: Path, architecture: str = "x86_64", *, custom_dir: bool = True):
        self.root = root
        self.bin = root / "tools"
        self.home = root / "home"
        self.install_dir = root / "custom bin" if custom_dir else self.home / ".local" / "bin"
        self.tmp = root / "tmp"
        for directory in (self.bin, self.home, self.tmp):
            directory.mkdir()
        self.env = {
            "PATH": str(self.install_dir) + os.pathsep + str(self.bin),
            "HOME": str(self.home),
            "DOTAI_HOME": str(self.home),
            "DOTAI_CONFIG_DIR": str(root / "config"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "DOTAI_STATE_DIR": str(root / "state"),
            "XDG_STATE_HOME": str(root / "xdg"),
            "GH_HOST": "github.invalid",
            "TMPDIR": str(self.tmp),
            "FIXTURE_ROOT": str(root),
            "FIXTURE_ARCH": architecture,
            "LC_ALL": "C",
        }
        if custom_dir:
            self.env["RTK_INSTALL_DIR"] = str(self.install_dir)
        for name in ("mktemp", "rm", "mkdir", "install", "sort", "sed", "sha256sum", "gzip"):
            tool = shutil.which(name)
            if not tool:
                raise RuntimeError(f"Required Linux fixture tool is absent: {name}")
            (self.bin / name).symlink_to(tool)
        self.script("uname", "import os, sys\nassert sys.argv[1:] == ['-m']\nprint(os.environ['FIXTURE_ARCH'])\n")
        self.script("curl", """import os, shutil, sys
from pathlib import Path
root = Path(os.environ['FIXTURE_ROOT'])
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith('https://'))
destination = args[args.index('-o') + 1]
targets = ('x86_64-unknown-linux-musl', 'aarch64-unknown-linux-gnu')
assert url.startswith('https://github.com/rtk-ai/rtk/releases/download/v')
target = next(target for target in targets if url.endswith('/rtk-' + target + '.tar.gz'))
(root / 'downloaded-target').write_text(target)
if os.environ.get('FIXTURE_DOWNLOAD_FAIL') == '1':
    Path(destination).write_bytes(b'partial download')
    sys.exit(22)
shutil.copyfile(root / (target + '.tar.gz'), destination)
""")
        real_tar = shutil.which("tar")
        if not real_tar:
            raise RuntimeError("Required Linux fixture tool is absent: tar")
        self.script("tar", f"""import os, sys
from pathlib import Path
Path(os.environ['FIXTURE_ROOT'], 'extracted').touch()
os.execv({real_tar!r}, [{real_tar!r}, *sys.argv[1:]])
""")
        for target in ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-gnu"):
            payload = (
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) printf 'rtk 0.50.0\\n';;\n"
                f"  --fixture-target) printf '{target}\\n';;\n"
                "  *) exit 2;;\n"
                "esac\n"
            ).encode()
            with tarfile.open(root / (target + ".tar.gz"), "w:gz") as archive:
                member = tarfile.TarInfo("rtk")
                member.size = len(payload)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(payload))

    def script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)

    def accept_checksum(self) -> None:
        # Upstream digest verification is real in mismatch tests. Success models
        # only the verifier's successful boundary, without repinning the manifest.
        (self.bin / "sha256sum").unlink()
        self.script("sha256sum", r"""import re, sys
from pathlib import Path
assert sys.argv[1:] == ['-c', '-']
line = sys.stdin.read().rstrip('\n')
digest, path = line.split('  ', 1)
assert re.fullmatch('[0-9a-f]{64}', digest)
assert Path(path).read_bytes()[:2] == b'\x1f\x8b'
""")

    def existing(self, version: str = "0.40.0") -> bytes:
        self.install_dir.mkdir(parents=True, exist_ok=True)
        binary = self.install_dir / "rtk"
        binary.write_text(f"#!/bin/sh\nprintf 'rtk {version}\\n'\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary.read_bytes()

    def run(self, operation: str) -> subprocess.CompletedProcess[str]:
        command = recipe_package("rtk", "RTK")[operation]["linux"][0]
        return subprocess.run(["/bin/sh", "-c", command], env=self.env, capture_output=True, text=True, timeout=15)


@unittest.skipUnless(sys.platform.startswith("linux"), "RTK commands require Linux/GNU tools")
class InstallerShellTests(unittest.TestCase):
    def assert_fail_closed(self, fixture: RtkShellFixture, operation: str, before: bytes) -> None:
        result = fixture.run(operation)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((fixture.install_dir / "rtk").read_bytes(), before)
        self.assertFalse((fixture.root / "extracted").exists())
        self.assertEqual(list(fixture.tmp.iterdir()), [])

    def test_checksum_mismatch_preserves_existing_binary_before_extraction(self) -> None:
        for operation in ("install", "update"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                fixture = RtkShellFixture(Path(directory))
                self.assert_fail_closed(fixture, operation, fixture.existing())
                self.assertTrue((fixture.root / "downloaded-target").exists())

    def test_download_failure_preserves_existing_binary_before_extraction(self) -> None:
        for operation in ("install", "update"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                fixture = RtkShellFixture(Path(directory))
                fixture.env["FIXTURE_DOWNLOAD_FAIL"] = "1"
                self.assert_fail_closed(fixture, operation, fixture.existing())
                self.assertTrue((fixture.root / "downloaded-target").exists())

    def test_unsupported_architecture_never_downloads_or_replaces_binary(self) -> None:
        for operation in ("install", "update"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                fixture = RtkShellFixture(Path(directory), "riscv64")
                self.assert_fail_closed(fixture, operation, fixture.existing())
                self.assertFalse((fixture.root / "downloaded-target").exists())

    def test_success_installs_executable_for_selected_architecture(self) -> None:
        for operation in ("install", "update"):
            for architecture, target in (
                ("x86_64", "x86_64-unknown-linux-musl"),
                ("amd64", "x86_64-unknown-linux-musl"),
                ("aarch64", "aarch64-unknown-linux-gnu"),
                ("arm64", "aarch64-unknown-linux-gnu"),
            ):
                with self.subTest(operation=operation, architecture=architecture), tempfile.TemporaryDirectory() as directory:
                    fixture = RtkShellFixture(Path(directory), architecture)
                    if operation == "update":
                        fixture.existing()
                    fixture.accept_checksum()
                    result = fixture.run(operation)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    binary = fixture.install_dir / "rtk"
                    actual = subprocess.run([str(binary), "--fixture-target"], env=fixture.env, capture_output=True, text=True, check=True)
                    self.assertEqual(actual.stdout.strip(), target)
                    self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
                    self.assertEqual(list(fixture.tmp.iterdir()), [])

    def test_install_uses_home_local_bin_when_override_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = RtkShellFixture(Path(directory), custom_dir=False)
            fixture.accept_checksum()
            result = fixture.run("install")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            actual = subprocess.run([str(fixture.home / ".local" / "bin" / "rtk"), "--version"], env=fixture.env, capture_output=True, text=True, check=True)
            self.assertEqual(actual.stdout.strip(), "rtk 0.50.0")

    def test_current_or_newer_update_is_noop_without_network(self) -> None:
        for version in ("0.50.0", "0.51.0", "1.0.0"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                fixture = RtkShellFixture(Path(directory), "unsupported")
                before = fixture.existing(version)
                result = fixture.run("update")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((fixture.install_dir / "rtk").read_bytes(), before)
                self.assertFalse((fixture.root / "downloaded-target").exists())
                self.assertFalse((fixture.root / "extracted").exists())
                self.assertEqual(list(fixture.tmp.iterdir()), [])

    def test_omp_update_advances_version_and_enables_privacy_preserving_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = RtkShellFixture(Path(directory))
            state = fixture.root / "omp-state.json"
            state.write_text(json.dumps({"version": "1.0.0", "custom": "keep", "secrets.enabled": False}))
            fixture.script("omp", """import json, os, sys
from pathlib import Path
path = Path(os.environ['FIXTURE_ROOT'], 'omp-state.json')
state = json.loads(path.read_text())
args = sys.argv[1:]
if args == ['--version']:
    print('omp ' + state['version'])
elif args == ['update']:
    state['version'] = '1.1.0'
    path.write_text(json.dumps(state))
elif args == ['config', 'set', 'secrets.enabled', 'true']:
    state['secrets.enabled'] = True
    path.write_text(json.dumps(state))
else:
    sys.exit(2)
""")
            runner = runtime.Runner("ubuntu")
            runner.env = fixture.env
            with contextlib.redirect_stdout(io.StringIO()):
                packages.reconcile_packages({"packages": [recipe_package("omp", "Oh My Pi")]}, runner, "update")
            self.assertEqual(runner.failures, [])
            self.assertEqual(json.loads(state.read_text()), {"version": "1.1.0", "custom": "keep", "secrets.enabled": True})
            self.assertFalse((fixture.root / "downloaded-target").exists())


class TerminalRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        previous = terminal._COLOR_ENABLED
        self.addCleanup(setattr, terminal, "_COLOR_ENABLED", previous)

    def test_color_policy_renders_precedence(self) -> None:
        cases = (
            ("auto", {}, False, False),
            ("auto", {}, True, True),
            ("auto", {"NO_COLOR": ""}, True, False),
            ("auto", {"FORCE_COLOR": "0"}, False, False),
            ("auto", {"FORCE_COLOR": "0"}, True, False),
            ("auto", {"FORCE_COLOR": "1"}, False, True),
            ("auto", {"FORCE_COLOR": "2"}, False, True),
            ("auto", {"TERM": "dumb"}, True, False),
            ("auto", {"TERM": "dumb", "FORCE_COLOR": "1"}, True, False),
            ("auto", {"NO_COLOR": "", "FORCE_COLOR": "1"}, True, False),
            ("always", {"NO_COLOR": "", "FORCE_COLOR": "0", "TERM": "dumb"}, False, True),
            ("never", {"FORCE_COLOR": "1"}, True, False),
        )
        for mode, environment, tty, enabled in cases:
            with self.subTest(mode=mode, environment=environment, tty=tty):
                with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(sys.stdout, "isatty", return_value=tty):
                    terminal.configure_color(mode)
                    rendered = terminal.badge("OK")
                self.assertEqual(rendered, "\033[32;1m[OK]\033[0m" if enabled else "[OK]")

    def test_label_colors_and_headings_render_without_color_leak(self) -> None:
        terminal.configure_color("always")
        for label, code in (
            ("OK", "32"), ("RUN", "36"), ("INACTIVE", "33"),
            ("DRIFT", "33"), ("UNVERIFIED", "33"), ("UPDATE", "33"),
            ("MISSING", "31"), ("FAIL", "31"), ("CUSTOM", "34"),
        ):
            with self.subTest(label=label):
                self.assertEqual(terminal.badge(label) + " detail", f"\033[{code};1m[{label}]\033[0m detail")
        self.assertEqual(terminal.heading("Stack") + " next", "\033[34;1mStack\033[0m next")
        self.assertEqual(terminal.styled("plain"), "plain")
        terminal.configure_color("never")
        self.assertEqual(terminal.heading("Stack"), "Stack")
        self.assertEqual(terminal.badge("FAIL"), "[FAIL]")

    def test_windows_console_enables_ansi_without_clearing_existing_flags(self) -> None:
        class Console:
            mode = 0x0003

            def GetStdHandle(self, which):
                return 123

            def GetConsoleMode(self, handle, pointer):
                pointer._obj.value = self.mode
                return 1

            def SetConsoleMode(self, handle, mode):
                self.mode = mode
                return 1

        console = Console()
        with mock.patch.object(terminal.os, "name", "nt"), mock.patch.object(ctypes, "windll", type("Windll", (), {"kernel32": console})(), create=True):
            terminal.configure_color("always")
        self.assertEqual(console.mode, 0x0007)
        self.assertEqual(terminal.badge("RUN"), "\033[36;1m[RUN]\033[0m")

    @unittest.skipUnless(os.name == "nt", "Native Windows redirected-console boundary")
    def test_native_windows_redirected_output_keeps_explicit_color(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from dotai_app import terminal; terminal.configure_color('always'); print(terminal.badge('OK'))"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "\033[32;1m[OK]\033[0m")


if __name__ == "__main__":
    unittest.main()
