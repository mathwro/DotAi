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
) -> int:
    if mode != "sync":
        package_manager.reconcile_packages(manifest, runner, mode, force, include_dependencies)
    omp_config.reconcile_omp_extensions(manifest, runner)
    skill_manager.reconcile_skills(
        manifest, runner, update_skills=force or update_skills, refresh_sources=refresh_sources,
        recommended_only=recommended_only,
    )
    omp_config.reconcile_plugins(manifest, runner, "install" if mode == "sync" else mode)
    try:
        mcp_config.sync_mcp(manifest, runner)
    except (OSError, runtime.DotAiError) as exc:
        runner.failures.append(str(exc))
        print(f"{terminal.badge('FAIL')} MCP: {exc}")
    app_state.save_state(manifest_path, runner, mode, managed_skills)
    if runner.failures:
        print(f"\n{terminal.styled('Reconciliation failed:', 'red', 'bold')}")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1
    print(f"\n{terminal.styled(f'DotAi {mode} complete for {runner.platform}.', 'green', 'bold')}")
    return 0
