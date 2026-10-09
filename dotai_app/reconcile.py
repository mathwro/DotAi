"""Coordinate package and integration reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from . import mcp as mcp_config
from . import omp as omp_config
from . import manifest as manifests
from . import prerequisites
from . import packages as package_manager
from . import runtime
from . import skills as skill_manager
from . import state as app_state
from . import terminal
from . import catalog, locking


def reconcile(
    manifest: dict[str, Any],
    manifest_path: Path,
    runner: runtime.Runner,
    mode: str,
    force: bool = False,
    managed_skills: list[dict[str, Any]] | None = None,
    update_skills: bool = False,
    refresh_sources: set[str] | None = None,
    recommended_only: bool = False,
) -> int:
    intent = manifest
    try:
        manifests.validate_manifest(manifest)
        manifest = catalog.materialize(manifest, runner.platform)
    except runtime.DotAiError as exc:
        runner.fail("Manifest plan", str(exc))
        return 1
    if not prerequisites.preflight(manifest, runner, mode):
        return 1
    try:
        manifest = locking.prepare(intent, manifest_path, runner, mode)
    except (OSError, runtime.DotAiError) as exc:
        runner.fail("Resolved stack plan", str(exc))
        return 1
    if mode != "sync":
        package_manager.reconcile_packages(manifest, runner, mode, force)
        if runner.failures:
            return 1
    omp_config.reconcile_omp_extensions(manifest, runner)
    skill_manager.reconcile_skills(
        manifest, runner, update_skills=force or update_skills, refresh_sources=refresh_sources,
        recommended_only=recommended_only,
    )
    omp_config.reconcile_plugins(manifest, runner, "install" if mode == "sync" else mode)
    try:
        if manifest.get("mcp", {}).get("servers"):
            mcp_config.sync_mcp(manifest, runner)
    except (OSError, runtime.DotAiError) as exc:
        runner.fail("MCP", str(exc))
    if not runner.failures:
        try:
            if locking.record(intent, manifest_path, runner, mode):
                print(f"{terminal.badge('OK')} Recorded observed stack versions and revisions.")
        except (OSError, runtime.DotAiError) as exc:
            runner.fail("Stack lock", str(exc))
    app_state.save_state(manifest_path, runner, mode, managed_skills)
    if runner.failures:
        print(f"\n{terminal.styled('Reconciliation failed:', 'red', 'bold')}")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1
    if runner.dry_run:
        print(f"\n{terminal.badge('RUN')} DotAi {mode} preview for {runner.platform}; no changes applied.")
    else:
        print(f"\n{terminal.styled(f'DotAi {mode} complete for {runner.platform}.', 'green', 'bold')}")
    return 0
