"""Portable paths and scoped, reviewed application execution boundaries."""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
from pathlib import Path
from . import runtime


def default_manifest_path(platform_name: str | None = None) -> Path:
    override = os.environ.get("DOTAI_CONFIG_DIR")
    if override:
        base = Path(override).expanduser()
    elif platform_name == "windows" or (platform_name is None and os.name == "nt"):
        appdata = os.environ.get("APPDATA")
        base = (Path(appdata) if appdata else runtime.home_dir() / "AppData" / "Roaming") / "DotAi"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg) if xdg else runtime.home_dir() / ".config") / "dotai"
    return base / "stack.json"


def lock_path(manifest_path: str | Path) -> Path:
    path = Path(manifest_path)
    return path.with_suffix(".lock.json")


def require_native_omp_update() -> None:
    """Reject launchers whose upstream updater could invoke a package manager."""
    executable = shutil.which("omp")
    if not executable:
        raise runtime.DotAiError("OMP native updater: active executable is unavailable")
    path = Path(executable)
    if path.is_symlink():
        raise runtime.DotAiError("OMP native updater refuses a manager or symlink launcher; use explicit install --force for a separate standalone copy")
    # Scoop's native shim forwards to the release executable; inspect that target,
    # not the shim implementation. No bucket or installed script is executed.
    shim = path.with_suffix(".shim")
    if os.name == "nt" and shim.exists():
        try:
            match = re.search(r'^path\s*=\s*"([^"]+)"\s*$', shim.read_text(encoding="utf-8-sig"), re.MULTILINE)
            if not match:
                raise ValueError("unrecognized Scoop shim")
            path = Path(match.group(1))
            scoop = Path(os.environ.get("SCOOP", runtime.home_dir() / "scoop")).resolve()
            relative = path.resolve().relative_to(scoop / "apps")
            if relative.parts[0] != "dotai-omp" or relative.name != "omp.exe":
                raise ValueError("foreign Scoop launcher")
        except (OSError, ValueError) as exc:
            raise runtime.DotAiError(f"OMP native updater refuses an unverified manager launcher: {exc}") from exc
    resolved = path.resolve()
    parts = {part.lower() for part in resolved.parts}
    if parts & {"cellar", "node_modules"} or str(resolved).startswith("/nix/store/"):
        raise runtime.DotAiError("OMP native updater refuses a manager-owned installation")
    mise_root = Path(os.environ.get("MISE_DATA_DIR") or (
        Path(os.environ["LOCALAPPDATA"]) / "mise" if os.name == "nt" and os.environ.get("LOCALAPPDATA")
        else Path(os.environ.get("XDG_DATA_HOME", runtime.home_dir() / ".local" / "share")) / "mise"
    )).resolve()
    if resolved.is_relative_to(mise_root):
        raise runtime.DotAiError("OMP native updater refuses a mise manager installation")
    probe_env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1"}
    for manager, commands in (
        ("brew", [["--prefix", "can1357/tap/omp"], ["--prefix", "omp"]]),
        ("mise", [["bin-paths", "github:can1357/oh-my-pi"]]),
    ):
        program = shutil.which(manager)
        if not program:
            continue
        for arguments in commands:
            try:
                result = subprocess.run([program, *arguments], env=probe_env, text=True, capture_output=True, timeout=15)
            except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                raise runtime.DotAiError(f"OMP native updater cannot verify {manager} manager ownership: {exc}") from exc
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    prefix = Path(line.strip()).resolve()
                    if line.strip() and resolved.is_relative_to(prefix):
                        raise runtime.DotAiError(f"OMP native updater refuses a {manager} manager-owned installation")
    if (resolved.parent.parent / "manager.json").exists() or (resolved.parent / "omp.bunx").exists():
        raise runtime.DotAiError("OMP native updater refuses a manager-owned installation")
    try:
        with resolved.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise runtime.DotAiError(f"OMP native updater cannot inspect the executable: {exc}") from exc
    native_headers = {b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                      b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
    if magic not in native_headers and magic[:2] != b"MZ":
        raise runtime.DotAiError("OMP native updater refuses a script or package-manager launcher")


def scoop_command(app: str, manifest: dict, *, replace: bool = False, uninstall: bool = False) -> str:
    """Use only a transient script-free manifest, never buckets or Scoop update."""
    repositories = {"rtk": "rtk-ai/rtk", "dotai-omp": "can1357/oh-my-pi"}
    binaries = {"rtk": "rtk.exe", "dotai-omp": "omp.exe"}
    if app not in repositories:
        raise runtime.DotAiError("Unsupported reviewed Scoop application")
    binary = binaries[app]
    allowed = {"version", "description", "homepage", "license", "url", "hash", "bin", "architecture"}
    if not isinstance(manifest, dict) or set(manifest) - allowed or manifest.get("bin") != binary:
        raise runtime.DotAiError("Scoop requires a minimal script-free application manifest")
    version = manifest.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise runtime.DotAiError("Scoop requires a reviewed exact release version")
    artifacts = manifest.get("architecture", {"default": manifest})
    if not isinstance(artifacts, dict) or not artifacts:
        raise runtime.DotAiError("Scoop requires reviewed architecture artifacts")
    for architecture, artifact in artifacts.items():
        if architecture not in {"default", "64bit", "arm64"} or not isinstance(artifact, dict):
            raise runtime.DotAiError("Unsupported Scoop architecture")
        if architecture != "default" and set(artifact) - {"url", "hash"}:
            raise runtime.DotAiError("Scoop architecture may contain only release URL and SHA-256")
        url, digest = artifact.get("url"), artifact.get("hash")
        prefix = f"https://github.com/{repositories[app]}/releases/download/v{version}/"
        if (not isinstance(url, str) or not url.startswith(prefix)
                or not re.fullmatch(r"[A-Za-z0-9._-]+(?:#/[A-Za-z0-9._-]+)?", url[len(prefix):])
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise runtime.DotAiError("Scoop requires a trusted release binary URL and SHA-256")
    encoded = json.dumps(manifest, separators=(",", ":")).replace("'", "''")
    script = r"""
$ErrorActionPreference = 'Stop'
$removed = $false
$temporary = $null
$app = '__APP__'
$binary = '__BINARY__'
$scoopRoot = if ($env:SCOOP) { $env:SCOOP } else { Join-Path $HOME 'scoop' }
$appsRoot = Join-Path $scoopRoot 'apps'
$appRoot = Join-Path $appsRoot $app
function Assert-PlainTree($directory) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($directory)
    while ($pending.Count -gt 0) {
        $next = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $next -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Refusing redirected files in existing Scoop application'
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}
function Assert-InstalledApplication {
    foreach ($root in @($scoopRoot, $appsRoot, $appRoot)) {
        if ((Test-Path -LiteralPath $root) -and ((Get-Item -LiteralPath $root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing redirected Scoop application root'
        }
    }
    if (!(Test-Path -LiteralPath $appRoot)) { return }
    if (!(Test-Path -LiteralPath (Join-Path $appRoot 'current/manifest.json'))) {
        throw 'Existing Scoop application has no verifiable current manifest'
    }
    foreach ($directory in Get-ChildItem -LiteralPath $appRoot -Directory -Force) {
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($directory.Name -ne 'current') { throw 'Refusing redirected Scoop version directory' }
            $targets = @($directory.Target)
            if ($targets.Count -ne 1) { throw 'Unverifiable Scoop current target' }
            $target = [IO.Path]::GetFullPath($targets[0])
            if ((Split-Path -Parent $target) -ne [IO.Path]::GetFullPath($appRoot)) {
                throw 'Scoop current points outside its application'
            }
            if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Scoop current points to another redirected directory'
            }
        } else { Assert-PlainTree $directory.FullName }
        $installed = Get-Content -LiteralPath (Join-Path $directory.FullName 'manifest.json') -Raw | ConvertFrom-Json
        $allowed = @('version', 'description', 'homepage', 'license', 'url', 'hash', 'bin', 'architecture', 'checkver', 'autoupdate', 'suggest')
        foreach ($property in $installed.PSObject.Properties.Name) {
            if ($property -notin $allowed) { throw 'Existing Scoop manifest has hooks or unreviewed uninstall behavior' }
        }
        if ($installed.bin -isnot [string] -or $installed.bin -ne $binary) {
            throw 'Existing Scoop application has foreign or unverifiable binary aliases'
        }
        if ($installed.architecture) {
            foreach ($entry in $installed.architecture.PSObject.Properties) {
                foreach ($property in $entry.Value.PSObject.Properties.Name) {
                    if ($property -notin @('url', 'hash', 'extract_dir')) {
                        throw 'Existing Scoop architecture contains hooks or unreviewed behavior'
                    }
                }
            }
        }
    }
}
try {
    Assert-InstalledApplication
    __OPERATION__
} catch {
    if ($removed) { [Console]::Error.WriteLine('Previous application was uninstalled; replacement failed. Persisted data was not purged. Retry the scoped install.') }
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) {
        Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
"""
    remove = r"""
    if (Test-Path -LiteralPath $appRoot) {
        & scoop uninstall $app
        if ($LASTEXITCODE -ne 0) { throw 'Scoped Scoop uninstall failed; existing state may be partially removed' }
        $removed = $true
        if (Test-Path -LiteralPath (Join-Path $appRoot 'current')) { throw 'Scoop left the previous application installed' }
    }
"""
    if uninstall:
        operation = remove
    else:
        operation = r"""
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('dotai-scoop-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $localManifest = Join-Path $temporary ($app + '.json')
    [IO.File]::WriteAllText($localManifest, '__MANIFEST__', (New-Object Text.UTF8Encoding($false)))
    & scoop download $localManifest --no-update-scoop
    if ($LASTEXITCODE -ne 0) { throw 'Scoop release download or digest verification failed; previous application preserved' }
    Assert-InstalledApplication
""" + (remove if replace else "") + r"""
    & scoop install $localManifest --independent --no-update-scoop
    if ($LASTEXITCODE -ne 0) { throw 'Scoped Scoop application install failed' }
"""
    return script.replace("__APP__", app).replace("__BINARY__", binary).replace("__OPERATION__", operation).replace("__MANIFEST__", encoded)
