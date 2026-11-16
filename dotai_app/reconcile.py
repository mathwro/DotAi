"""Coordinate package and integration reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from . import mcp as mcp_config
from . import omp as omp_config
from . import packages as package_manager
from . import runtime
from . import skills as skill_manager
from . import state as app_state
from . import terminal


def reconcile(
    manifest: dict[str, Any],
    manifest_path: Path,
    runner: runtime.Runner,
    mode: str,
    force: bool = False,
    include_dependencies: bool = False,
    managed_skills: list[dict[str, Any]] | None = None,
    update_skills: bool = False,
    refresh_sources: set[str] | None = None,
    recommended_only: bool = False,
    managed_baseline: dict[str, Any] | None = None,
    active_sections: set[str] | None = None,
    preserve_skill_env: bool = False,
) -> int:
    if mode != "sync" and (active_sections is None or "packages" in active_sections):
        package_manager.reconcile_packages(manifest, runner, mode, force, include_dependencies)
    if active_sections is None or "extensions" in active_sections:
        omp_config.reconcile_omp_extensions(manifest, runner)
    if active_sections is None or "skills" in active_sections:
        skill_manager.reconcile_skills(
            manifest, runner, update_skills=mode == "update" or force or update_skills,
            refresh_sources=refresh_sources,
            recommended_only=recommended_only,
            preserve_existing_env=preserve_skill_env,
        )
    if active_sections is None or "plugins" in active_sections:
        omp_config.reconcile_plugins(manifest, runner, "install" if mode == "sync" else mode)
    if active_sections is None or "mcp" in active_sections:
        try:
            mcp_config.sync_mcp(manifest, runner)
        except (OSError, runtime.DotAiError) as exc:
            runner.failures.append(str(exc))
            runner.say(f"{terminal.badge('FAIL')} MCP: {exc}")
    app_state.save_state(manifest_path, runner, mode, managed_skills, managed_baseline)
    if runner.failures:
        runner.say(f"\n{terminal.styled('Reconciliation failed:', 'red', 'bold')}")
        for failure in runner.failures:
            runner.say(f"  - {failure}")
        return 1
    if runner.dry_run:
        runner.say(f"\nDotAi {mode} preview complete for {runner.platform}; no changes applied.")
    else:
        runner.say(f"\n{terminal.styled(f'DotAi {mode} complete for {runner.platform}.', 'green', 'bold')}")
    return 0
