"""Execute isolated recipe fixtures at destructive cutover boundaries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotai_app import catalog, packages, portable, runtime


class ScopedRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "HOME": str(self.root), "DOTAI_HOME": str(self.root),
                    "XDG_STATE_HOME": str(self.root / "state"), "SCOOP": str(self.root / "scoop"),
                    "PATH": str(self.bin) + os.pathsep + os.defpath}
        for key in ("PI_INSTALL_DIR", "RTK_INSTALL_DIR", "BUN_INSTALL", "BUN_INSTALL_GLOBAL_DIR", "MISE_DATA_DIR"):
            self.env.pop(key, None)

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_step(self, step):
        runner = runtime.Runner("linux")
        return subprocess.run(runner.argv(step), env=self.env, text=True, capture_output=True)

    def test_inactive_recipe_intent_does_not_block_selected_components(self):
        intent = {"packages": [
            {"name": "Disabled", "recipe": "missing", "managed": True, "enabled": False},
            {"name": "User owned", "recipe": "omp", "managed": False, "version": "1.2.3"},
            {"name": "Selected", "recipe": "graphify", "managed": True},
        ]}
        effective = catalog.materialize(intent, "linux")
        self.assertEqual(effective["packages"][:2], intent["packages"][:2])
        self.assertTrue(effective["packages"][2]["install"])

    @unittest.skipUnless(os.name != "nt", "POSIX standalone removal")
    def test_uninstall_lookup_ignores_unsupported_desired_install_pin(self):
        target = self.root / ".local" / "bin" / "omp"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"selected application")
        unrelated = target.parent / "unrelated"
        unrelated.write_bytes(b"preserved")
        package = catalog.resolve_uninstall({"name": "OMP", "recipe": "omp", "managed": True, "version": "1.2.3"}, "linux")
        result = self.run_step(runtime.selected(package["uninstall"], "linux")[0])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(unrelated.read_bytes(), b"preserved")

    @unittest.skipUnless(os.name != "nt", "POSIX standalone release")
    def test_wrong_omp_release_digest_preserves_previous_binary(self):
        target = self.root / ".local" / "bin" / "omp"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous standalone application")
        facts = {"version": "18.8.7", "release": "v18.8.7", "artifact": "omp-linux-x64",
                 "url": "https://github.com/can1357/oh-my-pi/releases/download/v18.8.7/omp-linux-x64",
                 "sha256": "0" * 64}
        with patch.dict(os.environ, self.env, clear=True), patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(b"damaged")):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                packages.install_native_release(facts, ".local/bin/omp", "omp")
        self.assertEqual(target.read_bytes(), b"previous standalone application")

    @unittest.skipUnless(os.name != "nt", "POSIX standalone release")
    def test_verified_omp_release_is_staged_before_exact_cutover(self):
        payload = b"#!/bin/sh\nprintf 'omp 18.8.7\\n'\n"
        facts = {"version": "18.8.7", "release": "v18.8.7", "artifact": "omp-linux-x64",
                 "url": "https://github.com/can1357/oh-my-pi/releases/download/v18.8.7/omp-linux-x64",
                 "sha256": hashlib.sha256(payload).hexdigest()}
        target = self.root / ".local" / "bin" / "omp"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous standalone application")
        with patch.dict(os.environ, self.env, clear=True), patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            packages.install_native_release(facts, ".local/bin/omp", "omp")
        self.assertEqual(target.read_bytes(), payload)

    @unittest.skipUnless(os.name != "nt", "POSIX subprocess home smoke")
    def test_recipe_subprocess_uses_dotai_home_without_mutating_process_home(self):
        other = self.root / "real-home"
        selected = self.root / "selected-home"
        for home in (other, selected):
            target = home / ".local/bin/omp"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"home-owned application")
        before = (other / ".local/bin/omp").read_bytes()
        with patch.dict(os.environ, {**self.env, "HOME": str(other), "USERPROFILE": str(other), "DOTAI_HOME": str(selected)}, clear=True):
            runner = runtime.Runner("linux")
            package = catalog.resolve_uninstall({"name": "OMP", "recipe": "omp", "managed": True}, "linux")
            runtime.run_steps(runtime.selected(package["uninstall"], "linux"), runner, "Isolated home removal")
        self.assertFalse((selected / ".local/bin/omp").exists())
        self.assertEqual((other / ".local/bin/omp").read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX executable shim fixture")
    def test_scoop_shim_retargeting_refuses_owned_backend_uninstall(self):
        from dotai_app import lifecycle
        shims = self.root / "scoop/shims"
        backend = self.root / "scoop/apps/dotai-omp/current/omp.exe"
        shims.mkdir(parents=True)
        backend.parent.mkdir(parents=True)
        backend.write_bytes(b"owned binary")
        launcher = shims / "omp.exe"
        launcher.write_text("#!/bin/sh\nprintf 'omp 18.8.7\\n'\n")
        launcher.chmod(0o755)
        shim = launcher.with_suffix(".shim")
        shim.write_text(f'path = "{backend}"\n')
        intent = json.loads((Path(__file__).resolve().parents[1] / "stack.example.json").read_text())
        tool = {"name": "OMP", "recipe": "omp", "managed": True, "check": [str(launcher), "--version"]}
        intent["packages"] = [tool]
        with patch.dict(os.environ, {**self.env, "DOTAI_STATE_DIR": str(self.root / "state")}, clear=True):
            runner = runtime.Runner("windows")
            lifecycle.record_install("tool", "OMP", tool, intent, runner)
            foreign = self.root / "foreign.exe"
            foreign.write_bytes(b"foreign application")
            shim.write_text(f'path = "{foreign}"\n')
            with self.assertRaises(RuntimeError):
                lifecycle.mutate_runtime("tool", "OMP", tool, intent, runner, "uninstall")
        self.assertEqual(foreign.read_bytes(), b"foreign application")
        self.assertEqual(backend.read_bytes(), b"owned binary")

    @unittest.skipUnless(os.name != "nt" and shutil.which("sha256sum"), "POSIX archive/checksum tools")
    def test_bad_linux_rtk_archive_digest_preserves_previous_binary(self):
        target = self.root / ".local" / "bin" / "rtk"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous RTK")
        (self.bin / "sha256sum").symlink_to(shutil.which("sha256sum"))
        self.executable("curl", f'''#!{sys.executable}
import pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes(b'corrupt release archive')
''')
        steps = runtime.selected(catalog.resolve_package({"name": "RTK", "recipe": "rtk", "managed": True}, "linux")["install"], "linux")
        result = self.run_step(steps[0])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", (result.stdout + result.stderr).lower())
        self.assertEqual(target.read_bytes(), b"previous RTK")

    @unittest.skipUnless(os.name != "nt", "POSIX ownership fixtures")
    def test_native_updater_rejects_manager_link_without_changing_owner(self):
        manager = self.root / "Cellar" / "omp" / "18.8.7" / "bin" / "omp"
        manager.parent.mkdir(parents=True)
        shutil.copyfile(sys.executable, manager)
        manager.chmod(0o755)
        before = manager.read_bytes()
        (self.bin / "omp").symlink_to(manager)
        result = self.native_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(manager.read_bytes(), before)
        self.assertTrue((self.bin / "omp").is_symlink())

    @unittest.skipUnless(os.name != "nt", "POSIX ownership fixtures")
    def test_native_updater_accepts_regular_standalone_binary(self):
        path = self.bin / "omp"
        shutil.copyfile(sys.executable, path)
        path.chmod(0o755)
        result = self.native_probe()
        self.assertEqual(result.returncode, 0, result.stderr)

    def native_probe(self):
        return subprocess.run([sys.executable, "-c", "from dotai_app.portable import require_native_omp_update; require_native_omp_update()"],
                              cwd=Path(__file__).resolve().parents[1], env=self.env, text=True, capture_output=True)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell.exe"), "PowerShell interpreter unavailable")
class ScoopCutoverTests(unittest.TestCase):
    def setUp(self):
        ScopedRecipeTests.setUp(self)
        self.payload = self.root / "release.bin"
        self.payload.write_bytes(b"verified replacement RTK")
        self.env["FIXTURE_PAYLOAD"] = str(self.payload)
        self.current = self.root / "scoop" / "apps" / "rtk" / "current"
        self.current.mkdir(parents=True)
        (self.current / "rtk.exe").write_bytes(b"old RTK")
        self.manifest = {"version": "0.50.0", "url": "https://github.com/rtk-ai/rtk/releases/download/v0.50.0/rtk-x86_64-pc-windows-msvc.zip",
                         "hash": hashlib.sha256(self.payload.read_bytes()).hexdigest(), "bin": "rtk.exe"}
        (self.current / "manifest.json").write_text(json.dumps(self.manifest))
        persist = self.root / "scoop" / "persist" / "rtk"
        persist.mkdir(parents=True)
        (persist / "user-data").write_text("preserved")
        other = self.root / "scoop" / "apps" / "unrelated"
        other.mkdir(parents=True)
        (other / "user-data").write_text("preserved")

    def scoop_run(self, manifest=None):
        script = portable.scoop_command("rtk", manifest or self.manifest, replace=True)
        fixture = '''
function scoop {
    $operation = $args[0]
    $global:LASTEXITCODE = 0
    switch ($operation) {
        'download' {
            if ($args -notcontains '--no-update-scoop') { throw 'Manager self-update would be possible' }
            $m = Get-Content -LiteralPath $args[1] -Raw | ConvertFrom-Json
            if ((Get-FileHash -LiteralPath $env:FIXTURE_PAYLOAD -Algorithm SHA256).Hash.ToLowerInvariant() -ne $m.hash) {
                $global:LASTEXITCODE = 2; return
            }
            Set-Content -LiteralPath (Join-Path $env:HOME 'download-verified') -Value verified
        }
        'uninstall' {
            if (!(Test-Path -LiteralPath (Join-Path $env:HOME 'download-verified'))) { throw 'Unverified destructive cutover' }
            if ($args[1] -ne 'rtk' -or $args.Count -ne 2) { throw 'Unscoped or purging uninstall' }
            Remove-Item -LiteralPath (Join-Path $env:SCOOP 'apps/rtk') -Recurse -Force
            Set-Content -LiteralPath (Join-Path $env:HOME 'uninstalled') -Value removed
        }
        'install' {
            if ($args -notcontains '--no-update-scoop' -or $args -notcontains '--independent') { throw 'Manager or runtime mutation' }
            if ($env:FIXTURE_INSTALL_FAIL -eq '1') { $global:LASTEXITCODE = 3; return }
            $m = Get-Content -LiteralPath $args[1] -Raw | ConvertFrom-Json
            $allowed = @('version', 'url', 'hash', 'bin', 'architecture', 'description', 'homepage', 'license')
            foreach ($p in $m.PSObject.Properties.Name) { if ($p -notin $allowed) { throw 'Unreviewed install behavior' } }
            $dir = Join-Path $env:SCOOP 'apps/rtk/current'
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            Copy-Item -LiteralPath $env:FIXTURE_PAYLOAD -Destination (Join-Path $dir 'rtk.exe')
        }
        default { throw 'Unexpected Scoop operation' }
    }
}
'''
        file = self.root / "cutover.ps1"
        file.write_text(fixture + "\n" + script, encoding="utf-8")
        executable = shutil.which("pwsh") or shutil.which("powershell.exe")
        return subprocess.run([executable, "-NoProfile", "-File", str(file)], env=self.env, text=True, capture_output=True)

    def assert_preserved_other_state(self):
        self.assertEqual((self.root / "scoop/persist/rtk/user-data").read_text(), "preserved")
        self.assertEqual((self.root / "scoop/apps/unrelated/user-data").read_text(), "preserved")

    def test_bad_download_digest_preserves_existing_application(self):
        bad = {**self.manifest, "hash": "0" * 64}
        result = self.scoop_run(bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.current / "rtk.exe").read_bytes(), b"old RTK")
        self.assertFalse((self.root / "uninstalled").exists())
        self.assert_preserved_other_state()

    def test_existing_manifest_hook_is_rejected_before_uninstall(self):
        existing = {**self.manifest, "pre_uninstall": "Remove-Item unrelated -Recurse"}
        (self.current / "manifest.json").write_text(json.dumps(existing))
        result = self.scoop_run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "uninstalled").exists())
        self.assertEqual((self.current / "rtk.exe").read_bytes(), b"old RTK")

    def test_foreign_binary_alias_is_rejected_before_uninstall(self):
        existing = {**self.manifest, "bin": [["rtk.exe", "foreign-tool"]]}
        (self.current / "manifest.json").write_text(json.dumps(existing))
        result = self.scoop_run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "uninstalled").exists())
        self.assert_preserved_other_state()

    def test_verified_cutover_replaces_only_rtk_and_keeps_persisted_data(self):
        result = self.scoop_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.current / "rtk.exe").read_bytes(), b"verified replacement RTK")
        self.assert_preserved_other_state()

    def test_failure_after_uninstall_is_truthfully_partial(self):
        self.env["FIXTURE_INSTALL_FAIL"] = "1"
        result = self.scoop_run()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "uninstalled").exists())
        self.assertFalse((self.current / "rtk.exe").exists())
        self.assertIn("uninstalled", result.stderr.lower())
        self.assert_preserved_other_state()


if __name__ == "__main__":
    unittest.main()
