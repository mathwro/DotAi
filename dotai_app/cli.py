"""Argument parsing and command dispatch."""

from __future__ import annotations

from pathlib import Path
import argparse
import os
import sys
from . import conversion
from . import health
from . import integrations
from . import manifest as manifests
from . import prerequisites
from . import recommendations as skill_recommendations
from . import reconcile as reconciliation
from . import releases
from . import routing as model_routing
from . import runtime
from . import skills as skill_manager
from . import terminal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dotai", description="Install and reconcile a portable AI development stack.")
    parser.add_argument("--manifest", type=Path, help="Stack manifest (default: stack.json); convert requires an explicit source")
    parser.add_argument("--platform", choices=["windows", "wsl", "ubuntu", "arch", "macos", "linux"], help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto for terminals, always, or never (default: auto)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="Install missing components and synchronize configuration")
    install.add_argument("--force", action="store_true", help="Reinstall components already present")
    install.add_argument("--dry-run", action="store_true", help="Print actions without changing the machine")
    update = sub.add_parser("update", help="Update managed components and synchronize configuration")
    update.add_argument("--dry-run", action="store_true")
    sync = sub.add_parser("sync", help="Synchronize skills, plugins, and MCP configuration")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--update-skills", action="store_true", help="Refresh already installed skills")
    sync.add_argument(
        "--recommended-skills",
        action="store_true",
        help="Review repository-recommended skill additions, updates, and removals",
    )
    sync.add_argument(
        "--enforce",
        action="store_true",
        help="Offer user-owned skill cleanup, then enforce recommendations; skip preserved user-owned skills",
    )
    configure = sub.add_parser("configure", help="Configure explicit post-authentication integrations")
    configure_sub = configure.add_subparsers(dest="configure_target", required=True)
    configure_routing = configure_sub.add_parser("omp-routing", help="Configure OMP multi-provider model routing")
    configure_routing.add_argument("--dry-run", action="store_true")
    configure_routing.add_argument(
        "--primary",
        choices=["anthropic", "openai-codex"],
        help="Interactive primary when both Anthropic and Codex are authenticated",
    )
    sub.add_parser("version", help="Print the current DotAi version")
    sub.add_parser("init", help="Generate a new manifest from stack.example.json")
    convert = sub.add_parser("convert", help="Preview and explicitly convert a version-1 manifest; no environment operations")
    convert.add_argument("--dry-run", action="store_true", help="Review changes without creating files or backups")
    convert.add_argument("--destination", type=Path, help="New destination; defaults to the explicit source path")
    convert.add_argument("--manage", action="append", metavar="NAME", help="Authorize a reviewed legacy package's management; repeatable")
    convert.add_argument("--yes", action="store_true", help="Confirm the file write after explicit package ownership review")
    fix = sub.add_parser("fix", help="Review and migrate legacy Pi-targeted skills to OMP")
    fix.add_argument("--dry-run", action="store_true", help="Show the migration without changing files or machine state")
    add = sub.add_parser("add", help="Add a tool, skill, marketplace, plugin, or MCP server to the manifest")
    add_sub = add.add_subparsers(dest="kind", required=True)

    add_tool = add_sub.add_parser("tool", help="Add or replace a command-line tool")
    add_tool.add_argument("name")
    add_tool.add_argument("--check", dest="check_command", required=True, help="Command that exits zero when installed")
    add_tool.add_argument("--install", dest="install_commands", action="append", required=True, metavar="PLATFORM=COMMAND")
    add_tool.add_argument("--update", dest="update_commands", action="append", metavar="PLATFORM=COMMAND")
    add_tool.add_argument("--requires", action="append", help="Name of an external checks-only prerequisite; repeatable")

    add_skill = add_sub.add_parser("skill", help="Add or replace a skills.sh source")
    add_skill.add_argument("source")
    add_skill.add_argument("--agent", default="universal")
    add_skill.add_argument("--skill", dest="skills", action="append")
    add_skill.add_argument("--check-skill", dest="check_skills", action="append")
    add_skill.add_argument("--replace", action="store_true", help="Replace rather than merge this source and agent's selections")
    add_skill.add_argument("--revision", help="Desired upstream source revision")
    add_skill.add_argument("--installer-version", help="Exact skills installer version")

    add_marketplace = add_sub.add_parser("marketplace", help="Add or replace an OMP marketplace")
    add_marketplace.add_argument("name")
    add_marketplace.add_argument("source")

    add_plugin = add_sub.add_parser("plugin", help="Add or replace an OMP marketplace plugin")
    add_plugin.add_argument("id")
    add_plugin.add_argument("--scope", choices=["user", "project"], default="user")

    add_mcp = add_sub.add_parser("mcp", help="Add or replace an OMP MCP server")
    add_mcp.add_argument("name")
    mcp_transport = add_mcp.add_mutually_exclusive_group(required=True)
    mcp_transport.add_argument("--url")
    mcp_transport.add_argument("--command", dest="server_command")
    add_mcp.add_argument("--transport", choices=["http", "sse"], default="http")
    add_mcp.add_argument("--arg", dest="server_args", action="append")
    add_mcp.add_argument(
        "--header",
        dest="headers",
        action="append",
        metavar="NAME=VALUE",
        help="HTTP header NAME=VALUE; VALUE may name an environment variable; repeatable",
    )
    add_mcp.add_argument(
        "--env",
        dest="server_env",
        action="append",
        metavar="NAME=VALUE",
        help="stdio environment NAME=VALUE; repeatable",
    )
    sub.add_parser("status", help="Show installed and configured components")
    sub.add_parser("doctor", help="Check commands, configuration, and platform prerequisites")
    sub.add_parser("validate", help="Validate the manifest")
    sub.add_parser("platform", help="Print the detected platform")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert" and args.manifest is None:
        parser.error("convert requires an explicit --manifest PATH source")
    if args.manifest is None:
        args.manifest = manifests.DEFAULT_MANIFEST
    if args.command == "sync" and args.enforce and not args.recommended_skills:
        parser.error("--enforce requires --recommended-skills")
    terminal.configure_color(args.color)
    if args.platform:
        os.environ["DOTAI_PLATFORM"] = args.platform
    if args.command == "version":
        print(releases.VERSION)
        releases.print_release_notice()
        return 0
    if args.command == "convert":
        try:
            return conversion.convert_manifest(
                args.manifest, destination=args.destination, dry_run=args.dry_run,
                manage=args.manage, yes=args.yes,
            )
        except (OSError, runtime.DotAiError) as exc:
            print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
            return 2
    platform_name = runtime.detect_platform()
    if args.command == "platform":
        print(platform_name)
        return 0
    if args.command in {"install", "status", "sync"}:
        releases.print_release_notice()
    if args.command == "init":
        try:
            if not manifests.initialize_manifest(args.manifest, allow_custom=True):
                raise runtime.DotAiError(f"Manifest already exists: {args.manifest}")
        except (OSError, runtime.DotAiError) as exc:
            print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
            return 2
        return 0
    allow_legacy_routing = (
        args.command == "configure" and args.configure_target == "omp-routing"
    )
    try:
        manifest = manifests.load_manifest(
            args.manifest, allow_legacy_routing=allow_legacy_routing
        )
    except (OSError, runtime.DotAiError) as exc:
        print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(f"{terminal.badge('OK')} Valid manifest: {terminal.redact(args.manifest)}")
        return 0
    if args.command == "add":
        try:
            return integrations.add_integration(args, manifest, args.manifest)
        except (OSError, runtime.DotAiError) as exc:
            print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
            return 2
    runner = runtime.Runner(platform_name, getattr(args, "dry_run", False), args.verbose)
    if args.command in {"install", "update", "sync", "fix"} and not prerequisites.preflight(manifest, runner, args.command):
        return 1
    if args.command == "configure":
        try:
            return model_routing.configure_omp_routing(
                manifest, args.manifest, runner, args.primary
            )
        except (OSError, runtime.DotAiError) as exc:
            print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
            return 2
    if args.command == "fix":
        try:
            return skill_manager.fix_legacy_skills(manifest, args.manifest, runner)
        except (OSError, runtime.DotAiError) as exc:
            print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
            return 2
    if args.command == "status":
        return 0 if health.print_status(manifest, runner) else 1
    if args.command == "doctor":
        return 0 if health.doctor(manifest, runner) else 1
    if args.command == "sync":
        managed_skills = None
        refresh_sources: set[str] = set()
        if not args.recommended_skills:
            try:
                skill_recommendations.print_recommended_skill_notice(manifest, args.manifest)
            except (OSError, runtime.DotAiError) as exc:
                print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
                return 2
        if args.recommended_skills:
            try:
                old_skills = manifest["skills"]
                manifest, managed_skills = skill_recommendations.review_recommended_skills(
                    manifest, args.manifest, runner, args.enforce
                )
                if runner.failures:
                    return 1
                previous = {skill["source"]: skill for skill in old_skills}
                refresh_sources = {
                    skill["source"] for skill in manifest["skills"]
                    if previous.get(skill["source"]) != skill
                }
            except (OSError, runtime.DotAiError) as exc:
                print(f"{terminal.styled('dotai:', 'red', 'bold')} {terminal.redact(exc)}", file=sys.stderr)
                return 2
        return reconciliation.reconcile(
            manifest, args.manifest, runner, "sync", managed_skills=managed_skills,
            update_skills=args.update_skills, refresh_sources=refresh_sources,
            recommended_only=args.enforce,
        )
    if args.command == "update":
        skill_manager.print_legacy_skill_notice(manifest)
    return reconciliation.reconcile(
        manifest,
        args.manifest,
        runner,
        args.command,
        getattr(args, "force", False),
    )
