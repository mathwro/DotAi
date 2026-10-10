"""Coordinate selected, preflighted stack actions and verified recording."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable
from . import catalog, lifecycle, locking
from . import manifest as manifests
from . import mcp as mcp_config
from . import omp as omp_config
from . import packages as package_manager
from . import prerequisites, runtime, terminal
from . import skills as skill_manager
from . import state as app_state


def _describe_plan(manifest: dict[str, Any], runner: runtime.Runner, mode: str, force: bool,
                   refresh_sources: set[str]) -> None:
    print(terminal.heading(f"Selected stack plan: {mode} on {runner.platform}"))
    for kind, identity, value in lifecycle.components(manifest):
        scope = value.get("agent", value.get("scope", "user"))
        target = value.get("revision", value.get("version", "declared configuration"))
        current = "not observed"
        action = "synchronize selected configuration"
        if value.get("enabled") is False:
            action = "ensure inactive, only with proven ownership"
            current = "activation will be checked"
        elif kind == "tool":
            check = runtime.selected(value.get("check", []), runner.platform)
            _, output = package_manager.package_version_check({}, runner, check) if check else (False, "")
            match = package_manager.PACKAGE_VERSION_PATTERN.search(output)
            current = match.group(0) if match else "not version-verified"
            operation = package_manager.package_operation(value, runner, mode, force)
            action = operation or "retain installed version"
            if mode != "sync" and value.get("configure") and package_manager.selected_subset(manifest, value.get("configureWhen", {})):
                action += "; apply selected tool configuration"
        elif kind == "skill":
            present = skill_manager.skill_status(value)[0]
            current = "installed selection" if present else "missing or incomplete selection"
            action = "refresh reviewed source" if value["source"] in refresh_sources else "retain owned selection" if present else "install missing selection"
            target = f"revision {target}; installer {value.get('installerVersion', 'not resolved')}"
        elif kind == "plugin":
            observed = omp_config.installed_plugin(value)
            current = observed.get("version", "not recorded") if observed else "not installed"
            action = "update owned plugin" if mode == "update" and value.get("updatePolicy") != "pinned" else "retain current plugin" if observed else "install missing plugin"
        elif kind == "marketplace":
            present = omp_config.marketplace_record(value) is not None
            current = "registered" if present else "not registered"
            target = value["source"]
            action = "update owned marketplace" if mode == "update" else "retain registration" if present else "register marketplace"
        else:
            try:
                observed = lifecycle.observation(kind, identity, value, manifest, runner)
                current = "active registration" if observed.get("active") else "not active"
                if observed.get("active") and kind == "extension":
                    action = "retain matching registration"
                elif kind == "mcp":
                    target_path, _ = mcp_config.desired_mcp(manifest)
                    if any(enabled and mcp_config.server_satisfies(found, value)
                           for _, found, _, _, enabled in mcp_config.discover_mcp_servers(target_path)):
                        action = "retain matching registration"
            except (OSError, ValueError, runtime.DotAiError):
                current = "registration unverified"
        if value.get("nativeAvailableVersion"):
            target = f"vendor latest stable at execution (preflight available {value['nativeAvailableVersion']}); exact observed version will be recorded"
        elif target == "latest":
            target = "reviewed installer target; exact observed version will be recorded"
        print(f"  {terminal.redact(lifecycle.qualified_selector(kind, lifecycle.safe_label(identity), value))}: scope {terminal.redact(scope)}; "
              f"current {terminal.redact(current)}; target {terminal.redact(lifecycle.safe_label(target))}; action {terminal.redact(action)}.")
    if not lifecycle.components(manifest):
        print("  No components selected.")


def reconcile(
    manifest: dict[str, Any], manifest_path: Path, runner: runtime.Runner, mode: str,
    force: bool = False, managed_skills: list[dict[str, Any]] | None = None,
    update_skills: bool = False, refresh_sources: set[str] | None = None,
    recommended_only: bool = False, provenance_manifest: dict[str, Any] | None = None,
    summarize: bool = True,
) -> int:
    intent = copy.deepcopy(manifest)
    provenance = provenance_manifest if provenance_manifest is not None else manifest
    if recommended_only:
        baseline = {(skill["source"], skill.get("agent", "universal")) for skill in manifests.recommended_skills()}
        intent["skills"] = [skill for skill in intent["skills"] if (skill["source"], skill.get("agent", "universal")) in baseline]
        for skill in manifest["skills"]:
            if (skill["source"], skill.get("agent", "universal")) not in baseline:
                runner.record_outcome(f"Skills from {skill['source']}", "skipped", "Preserved during enforced sync")
    stage = "Resolved stack plan"
    try:
        manifests.validate_manifest(intent)
        effective = locking.prepared(intent, manifest_path, runner, mode, provenance_manifest=provenance)
        if effective is None:
            effective = catalog.materialize(intent, runner.platform)
            if not prerequisites.preflight(effective, runner, mode, force=force):
                return 1
            effective = locking.prepare(
                intent, manifest_path, runner, mode, force=force, update_skills=update_skills,
                refresh_sources=refresh_sources, skill_receipt_owned=lifecycle.skill_receipt_owned,
                provenance_manifest=provenance,
            )
        if not prerequisites.preflight(effective, runner, mode, force=force, show_available=False):
            return 1
        refresh = set(refresh_sources or ())
        refresh.update(item["skill"]["source"] for item in runner._dotai_lock_plan["skills"].values() if item["refresh"])
        if force or update_skills or mode == "update":
            refresh.update(skill["source"] for skill in effective["skills"])
        _describe_plan(effective, runner, mode, force, refresh)

        def declaration(kind: str, identity: str, value: dict[str, Any]) -> dict[str, Any]:
            original = lifecycle.resolve_selector(intent, lifecycle.qualified_selector(kind, identity, value))[2]
            if kind == "skill":
                return {**original, **{key: value[key] for key in ("revision", "installerVersion") if key in value}}
            return original

        def record_installed(kind, identity, value, _view, operation_runner):
            lifecycle.record_install(kind, identity, declaration(kind, identity, value), provenance, operation_runner)

        def deactivate(kind, identity, value, _view, operation_runner):
            return lifecycle.ensure_disabled(kind, identity, declaration(kind, identity, value), provenance, operation_runner)

        def ownership_check(kind, identity, value, _view, operation_runner):
            lifecycle.check_update_ownership(kind, identity, declaration(kind, identity, value), provenance, operation_runner)

        def run_domain(label: str, action: Callable[[], Any]) -> None:
            before = len(runner.outcomes)
            failures = len(runner.failures)
            result = action()
            outcomes = runner.outcomes[before:]
            if len(runner.failures) != failures and not any(item["status"] == "failed" for item in outcomes):
                runner.record_outcome(label, "failed", runner.failures[-1])
            elif not outcomes:
                runner.record_outcome(label, "planned" if runner.dry_run and result is True else "changed" if result is True else "unchanged")

        with locking.execution(manifest_path, runner):
            stage = "Selected stack actions"
            order = {"tool": 0, "extension": 1, "skill": 2, "marketplace": 3, "plugin": 4, "mcp": 5}
            for kind, identity, value in sorted(lifecycle.components(effective), key=lambda item: order[item[0]]):
                # MCP is reconciled together to preserve semantic sharing and conflict checks.
                if kind == "mcp" and value.get("enabled", True):
                    continue
                label = f"{kind.capitalize()} {lifecycle.safe_label(identity)}"
                if runner.failures:
                    runner.record_outcome(label, "skipped", "A required earlier action failed")
                    continue
                view = dict(effective)
                if kind == "tool":
                    if value.get("enabled") is False:
                        run_domain(label, lambda: deactivate(kind, identity, value, view, runner))
                    elif mode == "sync":
                        print(f"{terminal.badge('OK')} {terminal.redact(identity)}: retained; sync does not install, update, or configure tools.")
                        runner.record_outcome(label, "unchanged")
                    else:
                        view["packages"] = [value]
                        run_domain(label, lambda: package_manager.reconcile_packages(
                            view, runner, mode, force,
                            on_installed=lambda package, current, current_runner: record_installed("tool", package["name"], package, current, current_runner),
                            ownership_check=lambda package, current, current_runner, operation: lifecycle.check_update_ownership(
                                "tool", package["name"], declaration("tool", package["name"], package),
                                provenance, current_runner, operation=operation),
                        ))
                elif kind == "skill":
                    view["skills"] = [value]
                    run_domain(label, lambda: skill_manager.reconcile_skills(
                        view, runner, mode=mode, update_skills=force or update_skills,
                        refresh_sources=refresh, receipt_owned=lifecycle.skill_receipt_owned,
                        deactivate=deactivate, record_installed=record_installed,
                    ))
                elif kind in {"plugin", "marketplace"}:
                    view["plugins"] = [value] if kind == "plugin" else []
                    view["marketplaces"] = [value] if kind == "marketplace" else []
                    run_domain(label, lambda: omp_config.reconcile_plugins(
                        view, runner, mode, deactivate=deactivate, record_installed=record_installed,
                        ownership_check=ownership_check,
                    ))
                elif kind == "extension":
                    view["ompExtensions"] = [value]
                    before = len(runner.outcomes)
                    run_domain(label, lambda: omp_config.reconcile_omp_extensions(
                        view, runner, deactivate=deactivate, record_installed=record_installed,
                    ))
                    if runner.dry_run and len(runner.outcomes) == before + 1 and runner.outcomes[-1]["status"] == "unchanged":
                        configured = omp_config.configured_omp_extensions(runner)
                        if configured is None or not any(omp_config.extension_identity(entry) == omp_config.extension_identity(identity) for entry in configured):
                            runner.outcomes[-1]["status"] = "planned"
                else:
                    run_domain(label, lambda: deactivate(kind, identity, value, view, runner))
            servers = {name: value for name, value in effective.get("mcp", {}).get("servers", {}).items() if value.get("enabled", True)}
            if servers:
                if runner.failures:
                    runner.record_outcome("MCP configuration", "skipped", "A required earlier action failed")
                else:
                    view = {**effective, "mcp": {**effective["mcp"], "servers": servers}}
                    run_domain("MCP configuration", lambda: mcp_config.sync_mcp(
                        view, runner, deactivate=deactivate, record_installed=record_installed, ownership_check=ownership_check,
                    ))
            if runner.failures:
                print("Remaining selected actions were not run; no successful stack lock or reconciliation state was recorded.")
                return 1
            stage = "Stack lock"
            changed = locking.record(intent, manifest_path, runner, mode)
            runner.record_outcome("Stack lock", "planned" if runner.dry_run else "changed" if changed else "unchanged")
            stage = "Reconciliation state"
            app_state.save_state(manifest_path, runner, mode, managed_skills)
            runner.record_outcome("Reconciliation state", "planned" if runner.dry_run else "changed")
        return 0
    except (OSError, ValueError, runtime.DotAiError) as exc:
        if not runner.failures:
            runner.fail(stage, str(exc))
        return 1
    finally:
        if runner.failures and not any(outcome["status"] == "failed" for outcome in runner.outcomes):
            runner.record_outcome(stage, "failed", runner.failures[-1])
        if summarize:
            runner.summary()


def fix_legacy_skills(manifest: dict[str, Any], path: Path, runner: runtime.Runner) -> int:
    updated, migrated = skill_manager.legacy_skill_migration(manifest)
    if not migrated:
        print(f"{terminal.badge('OK')} No legacy Pi-targeted skills found in {terminal.redact(path)}.")
        return 0
    print(terminal.heading("Proposed skill migration:"))
    print(manifests.manifest_diff(manifest, updated, path))
    selectors = [
        lifecycle.qualified_selector("skill", skill["source"], skill)
        for skill in updated["skills"] if skill["source"] in migrated and skill.get("agent") == "universal"
    ]
    selected = lifecycle.filter_manifest(updated, selectors)
    try:
        effective = catalog.materialize(selected, runner.platform)
        if not prerequisites.preflight(effective, runner, "install"):
            return 1
        effective = locking.prepare(
            selected, path, runner, "install", skill_receipt_owned=lifecycle.skill_receipt_owned,
            provenance_manifest=updated,
        )
        if not prerequisites.preflight(effective, runner, "install", show_available=False):
            return 1
        _describe_plan(effective, runner, "install", False, set())
        if not runner.dry_run:
            try:
                answer = input("Apply these changes and install the migrated skills? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in {"y", "yes"}:
                print(f"{terminal.badge('OK')} No changes applied.")
                return 0
            with locking.execution(path, runner):
                backup = manifests.write_manifest(path, updated, backup=True)
                print(f"{terminal.badge('OK')} Manifest backup written to {terminal.redact(backup)}")
        return reconcile(selected, path, runner, "install", provenance_manifest=updated, summarize=False)
    except (OSError, ValueError, runtime.DotAiError) as exc:
        runner.fail("Skill migration", str(exc))
        return 1
    finally:
        if runner.failures and not any(outcome["status"] == "failed" for outcome in runner.outcomes):
            runner.record_outcome("Skill migration", "failed", runner.failures[-1])
        if runner.outcomes or runner.failures:
            runner.summary()
