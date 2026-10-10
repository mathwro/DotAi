from __future__ import annotations

import contextlib
import io
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from dotai_app import (
    cli,
    health,
    manifest as manifests,
    lifecycle,
    locking,
    mcp as mcp_config,
    omp as omp_config,
    packages as package_manager,
    recommendations as skill_recommendations,
    reconcile as reconciliation,
    releases,
    routing as model_routing,
    runtime,
    skills as skill_manager,
    state as app_state,
    terminal,
)


class DotAiTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            "HOME": str(home), "USERPROFILE": str(home), "DOTAI_HOME": str(home),
            "DOTAI_CONFIG_DIR": str(home / "config"), "XDG_CONFIG_HOME": str(home / "xdg-config"),
            "DOTAI_STATE_DIR": str(home / "state"), "XDG_STATE_HOME": str(home / "xdg-state"),
            "GH_HOST": "github.com", "NO_COLOR": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        real_run = subprocess.run
        self.fixture_sources = {}
        self.fixture_records = []
        self.fixture_revision = "a" * 40
        versions = {"omp": "omp 1.2.3", "node": "v24.0.0", "npx": "10.0.0"}
        def prerequisite_boundary(command, *args, **kwargs):
            if isinstance(command, list) and len(command) == 2 and command[1] == "--version" and command[0] in versions:
                command = [sys.executable, "-c", f"print({versions[command[0]]!r})"]
            elif isinstance(command, list) and command[0] == "npx":
                command = self.fixture_npx_command(command)
            return real_run(command, *args, **kwargs)
        boundary = mock.patch.object(subprocess, "run", new=prerequisite_boundary)
        boundary.start()
        self.addCleanup(boundary.stop)
        metadata = mock.patch.object(locking, "_fetch_json", side_effect=self.fixture_metadata)
        metadata.start()
        self.addCleanup(metadata.stop)
        network = mock.patch("urllib.request.urlopen", side_effect=OSError("Fixture network disabled"))
        network.start()
        self.addCleanup(network.stop)
        terminal.configure_color("never")
        self.addCleanup(terminal.configure_color, "never")

    @staticmethod
    def fixture_tree(files: dict[str, str]) -> tuple[str, list[dict]]:
        def walk(prefix):
            entries = []
            rows = []
            children = sorted({name[len(prefix):].split("/")[0] for name in files if name.startswith(prefix)})
            for name in children:
                path = prefix + name
                if path in files:
                    content = files[path].encode()
                    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                    kind, mode = "blob", "100644"
                else:
                    digest, nested = walk(path + "/")
                    rows.extend(nested)
                    kind, mode = "tree", "40000"
                rows.append({"path": path, "type": kind, "mode": mode.zfill(6), "sha": digest})
                entries.append(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(digest))
            content = b"".join(entries)
            return hashlib.sha1(b"tree " + str(len(content)).encode() + b"\0" + content).hexdigest(), rows
        return walk("")

    def resolve_skill_fixture(self, source: str, contents: dict[str, str]) -> None:
        self.fixture_sources.setdefault(source, {}).update(contents)

    def fixture_metadata(self, url: str) -> dict:
        if url.startswith("https://registry.npmjs.org/skills/"):
            return {"name": "skills", "version": "1.7.1", "engines": {"node": ">=20.0.0"},
                    "dist": {"integrity": "sha512-fixture", "tarball": "https://registry.npmjs.org/skills/-/skills-1.7.1.tgz"}}
        for source, contents in self.fixture_sources.items():
            base = "https://api.github.com/repos/" + source + "/"
            files = {"skills/" + name + "/SKILL.md": content for name, content in contents.items()}
            digest, rows = self.fixture_tree(files)
            if url.startswith(base + "commits/"):
                return {"sha": self.fixture_revision, "commit": {"tree": {"sha": digest}}}
            if url == base + "git/trees/" + digest + "?recursive=1":
                return {"sha": digest, "truncated": False, "tree": rows}
        raise AssertionError(f"Unexpected external metadata request: {url}")

    def fixture_npx_command(self, command: list[str]) -> list[str]:
        self.assertIn(command[2], ("skills@latest", "skills@1.7.1"))
        if command[3:] == ["--version"]:
            return [sys.executable, "-c", "print('1.7.1')"]
        if command[3] == "list":
            records = [row for row in self.fixture_records if Path(row["path"]).exists()]
            return [sys.executable, "-c", f"print({json.dumps(records)!r})"]
        self.assertEqual(command[3], "add")
        self.assertEqual(command[2], "skills@1.7.1")
        source = next((source for source in self.fixture_sources
                       if command[4] == f"https://github.com/{source}/tree/{self.fixture_revision}"), None)
        self.assertIsNotNone(source, f"Installer must consume a resolved immutable source: {command}")
        names = [command[index + 1] for index, value in enumerate(command) if value == "--skill"]
        agent = command[command.index("--agent") + 1]
        root = skill_manager.skill_root({"agent": agent})
        payload = []
        for name in names:
            content = self.fixture_sources[source][name]
            digest, _ = self.fixture_tree({"SKILL.md": content})
            entry = self.github_skill_lock_entry(source, digest, name)
            entry["ref"] = self.fixture_revision
            payload.append({"path": str(root / name), "content": content, "name": name, "owner": entry})
            record = {"name": name, "path": str(root / name), "scope": "global",
                      "agents": [agent.title()], "source": source, "sourceType": "github",
                      "sourceUrl": "https://github.com/" + source}
            self.fixture_records = [row for row in self.fixture_records if row["path"] != record["path"]]
            self.fixture_records.append(record)
        xdg = os.environ.get("XDG_STATE_HOME")
        owner_path = Path(xdg) / "skills/.skill-lock.json" if xdg else runtime.home_dir() / ".agents/.skill-lock.json"
        script = """import json, pathlib, sys
rows = json.loads(sys.argv[1])
lock_path = pathlib.Path(sys.argv[2])
lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"version": 3, "skills": {}}
for row in rows:
    folder = pathlib.Path(row["path"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(row["content"])
    lock["skills"][row["name"]] = row["owner"]
lock_path.parent.mkdir(parents=True, exist_ok=True)
lock_path.write_text(json.dumps(lock))
pathlib.Path(sys.argv[3]).write_text(sys.argv[4])
"""
        return [sys.executable, "-c", script, json.dumps(payload), str(owner_path),
                str(runtime.home_dir() / "fixture-installed-command.json"), json.dumps(command)]

    def adopt_mcp_fixture(self, manifest: dict, target: Path) -> None:
        previous = copy.deepcopy(manifest)
        config = json.loads(target.read_text())
        for name in list(previous["mcp"]["servers"]):
            alias = name if name in config["mcpServers"] else next(
                (alias for alias in config["mcpServers"] if alias == name + "-alias"), None)
            if alias is None:
                del previous["mcp"]["servers"][name]
                continue
            previous["mcp"]["servers"][name] = copy.deepcopy(config["mcpServers"][alias])
            if previous["mcp"]["servers"][name].get("type") == "remote":
                previous["mcp"]["servers"][name]["type"] = "http"
        path = target.parent / "fixture-stack.json"
        path.write_text(json.dumps(previous))
        with contextlib.redirect_stdout(io.StringIO()):
            for name in previous["mcp"]["servers"]:
                self.assertEqual(cli.main(["--manifest", str(path), "adopt", "mcp:" + name]), 0)

    def own_skill_fixture(self, folder: Path, source: str) -> None:
        if shutil.which("git") is None:
            self.skipTest("Git independently verifies fixture trees")
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            subprocess.run(["git", "init", "--quiet", "--object-format=sha1", str(tree)], check=True, capture_output=True)
            shutil.copytree(folder, tree, dirs_exist_ok=True)
            subprocess.run(["git", "-C", str(tree), "-c", "core.filemode=true", "-c", "core.autocrlf=false", "add", "--all"], check=True, capture_output=True)
            digest = subprocess.run(["git", "-C", str(tree), "write-tree"], check=True, text=True, capture_output=True).stdout.strip()
        xdg = os.environ.get("XDG_STATE_HOME")
        lock_path = Path(xdg) / "skills" / ".skill-lock.json" if xdg else runtime.home_dir() / ".agents" / ".skill-lock.json"
        lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"version": 3, "skills": {}}
        lock["skills"][folder.name] = self.github_skill_lock_entry(source, digest, folder.name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.resolve_skill_fixture(source, {folder.name: (folder / "SKILL.md").read_text()})
        self.fixture_records.append({"name": folder.name, "path": str(folder.resolve()), "scope": "global",
                                     "agents": ["Universal"], "source": source, "sourceType": "github",
                                     "sourceUrl": "https://github.com/" + source})

    def minimal_manifest(self, target: str) -> dict:
        return {"version": 2, "prerequisites": [], "packages": [], "skills": [],
        "marketplaces": [],
        "plugins": [],
        "ompExtensions": [],
        "mcp": {
            "target": target,
            "servers": {
                "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                "microsoft-learn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"},
            },
        },}

    def compact_routing(self, providers: list[str], primary: str) -> dict:
        return {
            "providers": providers,
            "primaryProvider": primary,
            "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
            "usageReservePct": 10,
            "usageReservePolicy": "auto",
            "fallbackRevertPolicy": "cooldown-expiry",
        }

    def routing_recommendations(self) -> dict:
        return {
            "version": 1,
            "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
            "providers": {
                "github-copilot": {
                    "roles": {
                        "default": ["github-copilot/reasoning-model", "github-copilot/interactive-model", "github-copilot/unavailable"],
                        "task": ["github-copilot/worker-model", "github-copilot/small-model"],
                        "smol": ["github-copilot/small-model", "github-copilot/worker-model"],
                        "slow": ["github-copilot/reasoning-model:high", "github-copilot/interactive-model:high", "github-copilot/worker-model:high", "github-copilot/small-model:high"],
                    }
                },
                "anthropic": {
                    "roles": {
                        "default": ["anthropic/interactive-model", "anthropic/legacy-interactive-model", "anthropic/unavailable"],
                        "task": ["anthropic/worker-model", "anthropic/interactive-model", "anthropic/legacy-interactive-model"],
                        "smol": ["anthropic/small-model", "anthropic/worker-model"],
                        "slow": ["anthropic/reasoning-model:high", "anthropic/interactive-model:high", "anthropic/legacy-interactive-model:high"],
                    }
                },
                "openai-codex": {
                    "roles": {
                        "default": ["openai-codex/reasoning-model", "openai-codex/interactive-model", "openai-codex/unavailable"],
                        "task": ["openai-codex/worker-model", "openai-codex/interactive-model"],
                        "smol": ["openai-codex/small-model", "openai-codex/utility-model"],
                        "slow": ["openai-codex/reasoning-model:high", "openai-codex/interactive-model:high"],
                    }
                },
            },
        }

    def mock_routing_catalog(self):
        return mock.patch.object(model_routing, "load_routing_recommendations", return_value=self.routing_recommendations())

    def omp_output(self, selectors: list[str], values: dict[str, object]):
        catalog = json.dumps({"models": [{"selector": selector} for selector in selectors]})

        def output(command: list[str]) -> str:
            if command == ["omp", "models", "--json"]:
                return catalog
            return json.dumps({"key": command[3], "value": values[command[3]]})

        return output

    def compact_status_case(self) -> tuple[dict, list[str], dict[str, object]]:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = self.compact_routing(
            ["anthropic", "github-copilot"], "anthropic"
        )
        selectors = [
            "anthropic/legacy-interactive-model",
            "anthropic/small-model",
            "github-copilot/worker-model",
            "github-copilot/interactive-model",
        ]
        values = {
            "modelRoles": {
                "custom": "private/keep",
                "default": "anthropic/legacy-interactive-model",
                "task": "github-copilot/worker-model",
                "smol": "github-copilot/worker-model",
                "slow": "anthropic/legacy-interactive-model:high",
            },
            "retry.fallbackChains": {
                "custom": ["private/keep"],
                "default": [
                    "anthropic/legacy-interactive-model",
                    "github-copilot/interactive-model",
                ],
                "task": [
                    "github-copilot/worker-model",
                    "anthropic/legacy-interactive-model",
                ],
                "smol": [
                    "github-copilot/worker-model",
                    "anthropic/small-model",
                ],
                "slow": [
                    "anthropic/legacy-interactive-model:high",
                    "github-copilot/interactive-model:high",
                    "github-copilot/worker-model:high",
                ],
            },
            "task.agentModelOverrides": {
                "reviewer": "@slow",
                "sonic": "@smol",
                "task": "@task",
            },
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        return manifest, selectors, values


    def test_validate_omp_routing_accepts_compact_intent_and_null(self) -> None:
        routing = self.compact_routing(["anthropic", "github-copilot"], "anthropic")
        self.assertEqual(manifests.validate_omp_routing(routing), routing)
        self.assertEqual(manifests.validate_omp_routing(None), {})


    def test_validate_omp_routing_rejects_invalid_compact_intent(self) -> None:
        valid = self.compact_routing(["anthropic"], "anthropic")
        invalid = [
            [],
            {**valid, "providers": []},
            {**valid, "providers": ["anthropic", "anthropic"]},
            {**valid, "providers": [1]},
            {**valid, "providers": ["unknown"]},
            {**valid, "primaryProvider": "openai-codex"},
            {**valid, "agentModelOverrides": []},
            {**valid, "usageReservePct": True},
            {**valid, "usageReservePct": 101},
            {**valid, "usageReservePolicy": "ask"},
            {**valid, "fallbackRevertPolicy": "always"},
            {**valid, "unexpected": True},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(runtime.DotAiError):
                manifests.validate_omp_routing(value)


    def test_compact_manifest_loads_without_routing_recommendations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = {
                "providers": ["anthropic"],
                "primaryProvider": "anthropic",
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            error = runtime.DotAiError("missing recommendations")
            with mock.patch.object(model_routing, "load_routing_recommendations", side_effect=error):
                loaded = manifests.load_manifest(path)
                self.assertEqual(loaded["ompRouting"]["providers"], ["anthropic"])
                self.assertEqual(
                    model_routing.omp_routing_status(loaded, runtime.Runner("ubuntu")),
                    ("FAIL", "unable to read routing recommendations"),
                )

    def test_repository_routing_catalog_covers_all_managed_roles(self) -> None:
        recommendations = model_routing.load_routing_recommendations()
        for provider, entry in recommendations["providers"].items():
            with self.subTest(provider=provider):
                available = {
                    model_routing.selector_identity(selector)
                    for selectors in entry["roles"].values()
                    for selector in selectors
                }
                primaries, _, unavailable = model_routing.resolve_omp_routing(
                    recommendations, [provider], provider, available
                )
                self.assertEqual(set(primaries), {"default", "task", "smol", "slow"})
                self.assertEqual(unavailable, [])

    def test_validate_routing_recommendations_rejects_malformed_data(self) -> None:
        valid = self.routing_recommendations()
        missing_role = json.loads(json.dumps(valid))
        del missing_role["providers"]["anthropic"]["roles"]["smol"]
        empty_role = json.loads(json.dumps(valid))
        empty_role["providers"]["anthropic"]["roles"]["smol"] = []
        cross_provider = json.loads(json.dumps(valid))
        cross_provider["providers"]["anthropic"]["roles"]["smol"] = [
            "openai-codex/utility-model"
        ]
        missing_provider = json.loads(json.dumps(valid))
        del missing_provider["providers"]["anthropic"]
        extra_provider = json.loads(json.dumps(valid))
        extra_provider["providers"]["other"] = extra_provider["providers"]["anthropic"]
        altered_overrides = json.loads(json.dumps(valid))
        altered_overrides["agentModelOverrides"]["sonic"] = "@task"
        missing_override = json.loads(json.dumps(valid))
        del missing_override["agentModelOverrides"]["task"]
        invalid = [
            {**valid, "version": 2},
            {"version": 1, "agentModelOverrides": {}},
            missing_role,
            empty_role,
            cross_provider,
            {**valid, "agentModelOverrides": []},
            missing_provider,
            extra_provider,
            altered_overrides,
            missing_override,
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(runtime.DotAiError):
                model_routing.validate_routing_recommendations(value)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-recommendations.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(runtime.DotAiError):
                model_routing.load_routing_recommendations(path)

    def test_static_routing_is_only_accepted_for_configure_migration(self) -> None:
        legacy = {"roles": {"default": ["openai-codex/interactive-model"]}}
        with self.assertRaisesRegex(runtime.DotAiError, "configure omp-routing"):
            manifests.validate_omp_routing(legacy)
        self.assertEqual(
            manifests.validate_omp_routing(legacy, allow_legacy=True)["roles"],
            legacy["roles"],
        )

    def test_load_manifest_rejects_non_object_roots_and_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for root in ([], [self.minimal_manifest("mcp.json")], None, "stack"):
                with self.subTest(root=root):
                    path.write_text(json.dumps(root), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

            for section in ("packages", "skills", "marketplaces", "plugins", "mcp"):
                with self.subTest(missing=section):
                    manifest = self.minimal_manifest("mcp.json")
                    del manifest[section]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

            for field in ("target", "servers"):
                with self.subTest(missing_mcp_field=field):
                    manifest = self.minimal_manifest("mcp.json")
                    del manifest["mcp"][field]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

    def test_load_manifest_rejects_malformed_packages_and_platform_commands(self) -> None:
        package = {"name": "sample", "managed": True, "check": ["sample", "--version"], "install": {"default": [["sample", "install"]]}, }
        invalid = {
            "non-object": 1,
            "missing-name": {"check": ["sample"], "install": []},
            "invalid-name": {**package, "name": False},
            "missing-check": {"name": "sample", "install": []},
            "invalid-check": {**package, "check": ["sample", 42]},
            "invalid-platform-check": {**package, "check": {"unknown": ["sample"]}},
            "missing-install": {"name": "sample", "check": ["sample"]},
            "invalid-install": {**package, "install": "sample install"},
            "empty-command": {**package, "install": [""]},
            "invalid-argv": {**package, "install": [["sample", 42]]},
            "invalid-platform-command": {**package, "install": {"ubuntu": "sample install"}},
            "invalid-platform-name": {**package, "install": {"unknown": [["sample"]]}},
            "invalid-update": {**package, "update": [42]},
            "invalid-configure": {**package, "configure": {"default": [["sample", None]]}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for case, entry in invalid.items():
                with self.subTest(case=case):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["packages"] = [entry]
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

    def test_load_manifest_rejects_malformed_skills_plugins_and_extensions(self) -> None:
        invalid = (
            ("skills", [None]),
            ("skills", [{}]),
            ("skills", [{"source": 4}]),
            ("skills", [{"source": "owner/skill", "agent": False}]),
            ("skills", [{"source": "owner/skill", "skills": [3]}]),
            ("skills", [{"source": "owner/skill", "checkSkills": "skill"}]),
            ("marketplaces", [{"name": "market"}]),
            ("marketplaces", [{"name": "market", "source": 12}]),
            ("plugins", [{"id": "not-a-plugin-id"}]),
            ("plugins", [{"id": "plugin@market", "scope": "machine"}]),
            ("ompExtensions", ["path", "path"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for section, entries in invalid:
                with self.subTest(section=section, entries=entries):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest[section] = entries
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

    def test_load_manifest_rejects_malformed_mcp_servers(self) -> None:
        invalid = {
            "non-object": [],
            "missing-stdio-command": {"type": "stdio"},
            "invalid-stdio-command": {"command": ["npx"]},
            "invalid-stdio-args": {"command": "npx", "args": ["-y", 9]},
            "invalid-stdio-env": {"command": "npx", "env": {"TOKEN": 3}},
            "invalid-stdio-cwd": {"command": "npx", "cwd": False},
            "missing-http-url": {"type": "http"},
            "invalid-http-url": {"type": "http", "url": "not a URI"},
            "invalid-port-name": {"type": "http", "url": "https://example.test:not-a-port/mcp"},
            "invalid-port-range": {"type": "sse", "url": "https://example.test:65536/mcp"},
            "invalid-http-headers": {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": 3}},
            "invalid-enabled": {"type": "sse", "url": "https://example.test/mcp", "enabled": "yes"},
            "invalid-timeout": {"command": "npx", "timeout": True},
            "unsupported-type": {"type": "grpc", "command": "npx"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            for case, server in invalid.items():
                with self.subTest(case=case):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["mcp"]["servers"] = {"custom": server}
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

            for field, value in (("target", ""), ("servers", [])):
                with self.subTest(mcp_field=field):
                    manifest = self.minimal_manifest("mcp.json")
                    manifest["mcp"][field] = value
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(runtime.DotAiError):
                        manifests.load_manifest(path)

    def test_load_manifest_preserves_user_owned_extra_fields(self) -> None:
        manifest = self.minimal_manifest("mcp.json")
        manifest["localOnly"] = {"owner": "user"}
        manifest["mcp"]["servers"]["custom"] = {
            "command": "npx", "args": ["server"], "providerSetting": {"keep": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = manifests.load_manifest(path)
        self.assertEqual(loaded["localOnly"], {"owner": "user"})
        self.assertEqual(loaded["mcp"]["servers"]["custom"]["providerSetting"], {"keep": True})

    def test_load_manifest_normalizes_present_omp_routing_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertNotIn("ompRouting", manifests.load_manifest(path))

            manifest["ompRouting"] = {
                "providers": ["anthropic"],
                "primaryProvider": "anthropic",
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                manifests.load_manifest(path)["ompRouting"],
                {
                    "providers": ["anthropic"],
                    "primaryProvider": "anthropic",
                    "agentModelOverrides": {},
                    "usageReservePct": 10,
                    "usageReservePolicy": "auto",
                    "fallbackRevertPolicy": "cooldown-expiry",
                },
            )


    def test_loaded_null_omp_routing_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = manifests.load_manifest(path)

        runner = runtime.Runner("ubuntu")
        with mock.patch.object(runner, "output") as output, mock.patch.object(runner, "run") as run:
            self.assertEqual(model_routing.omp_routing_status(loaded, runner), ("OK", "not configured in manifest"))
            report = io.StringIO()
            with mock.patch.object(mcp_config, "mcp_status", return_value=(True, "managed")), contextlib.redirect_stdout(report):
                self.assertTrue(health.print_status(loaded, runner))
        output.assert_not_called()
        run.assert_not_called()
        self.assertIn("[INACTIVE]", report.getvalue())
        self.assertIn("configure omp-routing --dry-run", report.getvalue())

    def test_mcp_merge_preserves_unmanaged_values_backs_up_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "customTopLevel": {"preserve": True},
                        "mcpServers": {
                            "context7": {"type": "http", "url": "https://old.example/mcp"},
                            "private": {"type": "http", "url": "https://private.example/mcp"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                runner = runtime.Runner("ubuntu")
                self.adopt_mcp_fixture(manifest, target)
                self.assertTrue(mcp_config.sync_mcp(manifest, runner, ownership_check=lifecycle.check_update_ownership))
                merged = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(merged["customTopLevel"], {"preserve": True})
                self.assertIn("private", merged["mcpServers"])
                self.assertEqual(merged["mcpServers"]["context7"], manifest["mcp"]["servers"]["context7"])
                backups = list(target.parent.glob("mcp.json.bak.*"))
                self.assertEqual(len(backups), 1)
                self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                self.assertEqual(len(list(target.parent.glob("mcp.json.bak.*"))), 1)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not Windows ACLs")
    def test_config_backups_are_private_even_when_sources_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"mcpServers": {"context7": {"type": "http", "url": "https://old.example/mcp", "headers": {"X-Key": "old-reference"}}}}),
                encoding="utf-8",
            )
            target.chmod(0o644)
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                self.adopt_mcp_fixture(manifest, target)
                self.assertTrue(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu"), ownership_check=lifecycle.check_update_ownership))
            backup = next(target.parent.glob("mcp.json.bak.*"))
            self.assertIn("old-reference", backup.read_text(encoding="utf-8"))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

            custom = home / "custom-stack.json"
            custom.write_text('{"env":"OLD_TOKEN"}', encoding="utf-8")
            custom.chmod(0o644)
            prior = manifests.backup_manifest(custom)
            self.assertEqual(prior.read_text(encoding="utf-8"), custom.read_text(encoding="utf-8"))
            self.assertEqual(prior.stat().st_mode & 0o777, 0o600)

    def test_mcp_status_accepts_alias_headers_and_external_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "Ctx7": {
                                "type": "http",
                                "url": "https://mcp.context7.com/mcp",
                                "headers": {"CONTEXT7_API_KEY": "test-key"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            opencode = home / ".config" / "opencode" / "opencode.json"
            opencode.parent.mkdir(parents=True)
            opencode.write_text(
                json.dumps(
                    {
                        "mcp": {
                            "microsoft-learn": {
                                "type": "remote",
                                "url": "https://learn.microsoft.com/api/mcp",
                                "enabled": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            original = target.read_text(encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                healthy, detail = mcp_config.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_mcp_sync_respects_disabled_server_without_reporting_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            original = json.dumps(
                {
                    "disabledServers": ["context7"],
                    "mcpServers": {
                        "context7": manifest["mcp"]["servers"]["context7"],
                        "personal": {"type": "http", "url": "https://personal.example/mcp"},
                    },
                    "customTopLevel": {"owner": "user"},
                }
            )
            target.write_text(original, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
                self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
                self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
            self.assertNotIn("OK", output.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_respects_disabled_reserved_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / "mcp.json"
            required = {"type": "http", "url": "https://example.test/mcp"}
            original = json.dumps({
                "disabledServers": ["second"],
                "mcpServers": {"second": required},
            })
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest(str(target))
            manifest["mcp"]["servers"] = {"first": required, "second": required}
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = runtime.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(mcp_config.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(home.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_preserves_unowned_non_object_slot_and_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "context7": None,
                "personal": {"type": "http", "url": "https://personal.example/mcp"},
            }, "customTopLevel": {"owner": "user"}}
            target.write_text(json.dumps(original), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                try:
                    changed = mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu"))
                except AttributeError as exc:
                    self.fail(f"MCP sync crashed on a non-object managed entry: {exc}")
                self.assertFalse(changed)
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)
            self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_does_not_duplicate_disabled_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            provider = home / ".config" / "opencode" / "opencode.json"
            provider.parent.mkdir(parents=True)
            provider.write_text(
                json.dumps(
                    {
                        "disabledServers": ["disabled-alias"],
                        "mcp": {
                            "disabled-alias": {
                                "type": "remote", "url": "https://mcp.context7.com/mcp"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {"context7": manifest["mcp"]["servers"]["context7"]}
            original = provider.read_text(encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
                self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
            self.assertNotIn("OK", output.getvalue())
            self.assertFalse(target.exists())
            self.assertEqual(provider.read_text(encoding="utf-8"), original)

    def test_mcp_sync_keeps_same_command_instances_in_their_managed_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 5},
                "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 5},
            }}
            target.write_text(json.dumps(original), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 30},
                "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 30},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                runner = runtime.Runner("ubuntu")
                self.adopt_mcp_fixture(manifest, target)
                self.assertTrue(mcp_config.sync_mcp(manifest, runner, ownership_check=lifecycle.check_update_ownership))
                updated = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(updated["mcpServers"], {
                    "a": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/a", "timeout": 30},
                    "b": {"command": "python3", "args": ["-m", "example"], "cwd": "/workspace/b", "timeout": 30},
                })
                healthy, detail = mcp_config.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                after = target.read_bytes()
                self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                self.assertEqual(runner.failures, [])
                self.assertEqual(target.read_bytes(), after)
            backups = list(target.parent.glob("mcp.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_mcp_equivalent_requirements_share_one_healthy_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({"mcpServers": {
                "provider-alias": {"url": "https://shared.example/mcp", "headers": {"X-Auth": "SHARED_TOKEN_FROM_ENV"}},
            }})
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "first": {"url": "https://shared.example/mcp"},
                "second": {"url": "https://shared.example/mcp"},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                with self.subTest(operation="status"):
                    healthy, detail = mcp_config.mcp_status(manifest)
                    self.assertTrue(healthy, detail)
                with self.subTest(operation="sync"):
                    runner = runtime.Runner("ubuntu")
                    self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                    self.assertEqual(runner.failures, [])
                    self.assertEqual(target.read_text(encoding="utf-8"), original)
                    self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_does_not_overwrite_a_named_slot_satisfying_another_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({"mcpServers": {
                "b": {"url": "https://first.example/mcp", "headers": {"X-Auth": "FIRST_TOKEN_FROM_ENV"}},
            }})
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "a": {"url": "https://first.example/mcp"},
                "b": {"url": "https://second.example/mcp"},
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = runtime.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(mcp_config.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_preserves_unmanaged_alias_colliding_with_another_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = json.dumps({
                "mcpServers": {"personal": {
                    "url": "https://personal.example/mcp", "timeout": 5,
                    "headers": {"X-Auth": "PERSONAL_TOKEN_FROM_ENV"},
                }},
                "servers": {"personal": {"url": "https://managed.example/mcp", "timeout": 5}},
                "customTopLevel": {"owner": "user"},
            })
            target.write_text(original, encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "managed": {"url": "https://managed.example/mcp", "timeout": 30}
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = runtime.Runner("ubuntu", dry_run=dry_run)
                        self.assertFalse(mcp_config.sync_mcp(manifest, runner))
                        self.assertTrue(runner.failures)
                        self.assertFalse(mcp_config.mcp_status(manifest)[0])
                        self.assertEqual(target.read_text(encoding="utf-8"), original)
                        self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])

    def test_mcp_sync_ignores_malformed_args_on_unrelated_disabled_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            original = {"mcpServers": {
                "unrelated": {"command": "python3", "args": None, "enabled": False},
            }, "customTopLevel": {"owner": "user"}}
            target.write_text(json.dumps(original), encoding="utf-8")
            before = target.read_bytes()
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "managed": {"command": "python3", "args": ["-m", "example"]}
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        runner = runtime.Runner("ubuntu", dry_run=dry_run)
                        try:
                            changed = mcp_config.sync_mcp(manifest, runner)
                        except TypeError as exc:
                            self.fail(f"MCP sync crashed on unrelated malformed args: {exc}")
                        self.assertTrue(changed)
                        self.assertEqual(runner.failures, [])
                        if dry_run:
                            self.assertEqual(target.read_bytes(), before)
                            self.assertEqual(list(target.parent.glob("mcp.json.bak.*")), [])
                updated = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(updated["mcpServers"], {
                    "unrelated": {"command": "python3", "args": None, "enabled": False},
                    "managed": {"command": "python3", "args": ["-m", "example"]},
                })
                self.assertEqual(updated["customTopLevel"], {"owner": "user"})
                healthy, detail = mcp_config.mcp_status(manifest)
                self.assertTrue(healthy, detail)
                after = target.read_bytes()
                self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
                self.assertEqual(target.read_bytes(), after)
            backups = list(target.parent.glob("mcp.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_mcp_sync_reconciles_stdio_cwd_and_timeout_without_losing_alias_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local-alias": {
                                "type": "stdio", "command": "python3", "args": ["-m", "example"],
                                "cwd": "/workspace/old", "timeout": 5,
                                "env": {"TOKEN": "TOKEN_FROM_ENV", "LOCAL_SETTING": "keep"},
                                "providerOptions": {"retry": 2},
                            },
                            "personal": {"type": "http", "url": "https://personal.example/mcp"},
                        },
                        "customTopLevel": {"owner": "user"},
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "local": {
                    "type": "stdio", "command": "python3", "args": ["-m", "example"],
                    "cwd": "/workspace/current", "timeout": 30, "env": {"TOKEN": "TOKEN_FROM_ENV"},
                }
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                self.adopt_mcp_fixture(manifest, target)
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu"), ownership_check=lifecycle.check_update_ownership))
                merged = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(set(merged["mcpServers"]), {"local-alias", "personal"})
                self.assertEqual(merged["mcpServers"]["local-alias"]["cwd"], "/workspace/current")
                self.assertEqual(merged["mcpServers"]["local-alias"]["timeout"], 30)
                self.assertEqual(
                    merged["mcpServers"]["local-alias"]["env"],
                    {"TOKEN": "TOKEN_FROM_ENV", "LOCAL_SETTING": "keep"},
                )
                self.assertEqual(merged["mcpServers"]["local-alias"]["providerOptions"], {"retry": 2})
                self.assertEqual(merged["customTopLevel"], {"owner": "user"})
                self.assertTrue(mcp_config.mcp_status(manifest)[0])
                after = target.read_bytes()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu")))
                self.assertEqual(target.read_bytes(), after)
                self.assertEqual(len(list(target.parent.glob("mcp.json.bak.*"))), 1)

    def test_mcp_sync_reconciles_remote_timeout_while_preserving_extra_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "remote-alias": {
                                "type": "remote", "url": "https://mcp.example.test/mcp", "timeout": 5,
                                "enabled": True, "headers": {"X-Extra": "EXTRA_FROM_ENV"},
                                "providerOptions": {"keep": True},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {
                "remote": {
                    "type": "http", "url": "https://mcp.example.test/mcp",
                    "timeout": 20, "enabled": True,
                }
            }
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                self.adopt_mcp_fixture(manifest, target)
                self.assertFalse(mcp_config.mcp_status(manifest)[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(mcp_config.sync_mcp(manifest, runtime.Runner("ubuntu"), ownership_check=lifecycle.check_update_ownership))
                merged = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
                self.assertEqual(set(merged), {"remote-alias"})
                self.assertEqual(merged["remote-alias"]["timeout"], 20)
                self.assertEqual(merged["remote-alias"]["headers"], {"X-Extra": "EXTRA_FROM_ENV"})
                self.assertEqual(merged["remote-alias"]["providerOptions"], {"keep": True})
                self.assertTrue(mcp_config.mcp_status(manifest)[0])

    def test_selector_identity_removes_one_recognized_thinking_suffix(self) -> None:
        self.assertEqual(model_routing.selector_identity("openai-codex/interactive-model:high"), "openai-codex/interactive-model")
        self.assertEqual(model_routing.selector_identity("openai-codex/interactive-model:high:auto"), "openai-codex/interactive-model:high")
        self.assertEqual(model_routing.selector_identity("openai-codex/interactive-model:custom"), "openai-codex/interactive-model:custom")

    def test_available_omp_models_parses_only_complete_catalogs(self) -> None:
        runner = runtime.Runner("ubuntu")
        catalog = json.dumps(
            {
                "models": [
                    {"selector": "openai-codex/interactive-model"},
                    {"selector": "github-copilot/interactive-model"},
                    {"selector": "github-copilot/utility-model"},
                ]
            }
        )

        with mock.patch.object(runner, "output", return_value=catalog) as output:
            self.assertEqual(
                model_routing.available_omp_models(runner),
                {
                    "openai-codex/interactive-model",
                    "github-copilot/interactive-model",
                    "github-copilot/utility-model",
                },
            )
        output.assert_called_once_with(["omp", "models", "--json"])

        for malformed in ("", "[]", "{", "{}", '{"models": {}}', '{"models": ["selector"]}', '{"models": [{}]}', '{"models": [{"selector": ""}]}'):
            with self.subTest(malformed=malformed), mock.patch.object(runner, "output", return_value=malformed):
                self.assertIsNone(model_routing.available_omp_models(runner))

        with mock.patch.object(runner, "output", return_value='{"models": []}'):
            self.assertEqual(model_routing.available_omp_models(runner), set())

    def test_detected_routing_providers_require_supported_recommendations(self) -> None:
        recommendations = self.routing_recommendations()
        available = {
            "github-copilot/worker-model",
            "openai-codex/interactive-model",
            "private/model",
        }
        self.assertEqual(
            model_routing.detected_routing_providers(recommendations, available),
            ["github-copilot", "openai-codex"],
        )
        with self.assertRaisesRegex(runtime.DotAiError, "no recommended models"):
            model_routing.detected_routing_providers(
                recommendations,
                {"anthropic/unknown-model"},
            )
        self.assertEqual(
            model_routing.detected_routing_providers(recommendations, {"private/model"}),
            [],
        )

    def test_choose_primary_provider_applies_subscription_rules(self) -> None:
        cases = [
            (["github-copilot"], None, None, "github-copilot"),
            (["anthropic"], None, None, "anthropic"),
            (["openai-codex"], None, None, "openai-codex"),
            (["anthropic", "github-copilot"], None, None, "anthropic"),
            (["github-copilot", "openai-codex"], None, None, "openai-codex"),
            (["anthropic", "openai-codex"], "anthropic", None, "anthropic"),
            (["anthropic", "openai-codex"], "openai-codex", None, "openai-codex"),
            (["anthropic", "openai-codex"], None, "anthropic", "anthropic"),
        ]
        for providers, current, requested, expected in cases:
            with self.subTest(providers=providers, current=current, requested=requested):
                self.assertEqual(
                    model_routing.choose_primary_provider(providers, current, requested),
                    expected,
                )

        for response, expected in (("1", "anthropic"), ("2", "openai-codex")):
            with (
                self.subTest(response=response),
                mock.patch.object(sys.stdin, "isatty", return_value=True),
                mock.patch("builtins.input", return_value=response) as prompt,
            ):
                self.assertEqual(
                    model_routing.choose_primary_provider(
                        ["anthropic", "openai-codex"], None, None
                    ),
                    expected,
                )
                prompt.assert_called_once_with(
                    "Choose interactive primary: [1] Anthropic [2] OpenAI Codex: "
                )

        with (
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            self.assertRaisesRegex(runtime.DotAiError, "--primary"),
        ):
            model_routing.choose_primary_provider(["anthropic", "openai-codex"], None, None)

        with self.assertRaisesRegex(runtime.DotAiError, "--primary.*not available"):
            model_routing.choose_primary_provider(["anthropic"], None, "openai-codex")

        for providers, requested in (
            (["github-copilot"], "anthropic"),
            (["anthropic"], "github-copilot"),
            (["anthropic", "openai-codex"], "github-copilot"),
            (["anthropic", "openai-codex"], "private"),
        ):
            with (
                self.subTest(providers=providers, requested=requested),
                self.assertRaisesRegex(runtime.DotAiError, "--primary.*not available"),
            ):
                model_routing.choose_primary_provider(providers, None, requested)

        with (
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="3") as prompt,
            self.assertRaisesRegex(runtime.DotAiError, "Invalid primary selection"),
        ):
            model_routing.choose_primary_provider(["anthropic", "openai-codex"], None, None)
        prompt.assert_called_once()

        cancellation_errors = []
        for interruption in (EOFError, KeyboardInterrupt):
            with (
                self.subTest(interruption=interruption.__name__),
                mock.patch.object(sys.stdin, "isatty", return_value=True),
                mock.patch("builtins.input", side_effect=interruption),
                self.assertRaises(runtime.DotAiError) as raised,
            ):
                model_routing.choose_primary_provider(
                    ["anthropic", "openai-codex"], None, None
                )
            cancellation_errors.append(str(raised.exception))
        self.assertEqual(cancellation_errors, ["Primary selection cancelled"] * 2)

    def test_resolve_omp_routing_handles_provider_combinations(self) -> None:
        recommendations = self.routing_recommendations()
        provider_roles = recommendations["providers"]

        def available_for(providers: list[str]) -> set[str]:
            return {
                model_routing.selector_identity(provider_roles[provider]["roles"][role][0])
                for provider in providers
                for role in model_routing.ROUTING_ROLES
            }

        copilot = {
            "default": "github-copilot/reasoning-model",
            "task": "github-copilot/worker-model",
            "smol": "github-copilot/small-model",
            "slow": "github-copilot/reasoning-model:high",
        }
        anthropic = {
            "default": "anthropic/interactive-model",
            "task": "anthropic/worker-model",
            "smol": "anthropic/small-model",
            "slow": "anthropic/reasoning-model:high",
        }
        codex = {
            "default": "openai-codex/reasoning-model",
            "task": "openai-codex/worker-model",
            "smol": "openai-codex/small-model",
            "slow": "openai-codex/reasoning-model:high",
        }
        cases = [
            (["github-copilot"], "github-copilot", copilot),
            (["anthropic"], "anthropic", anthropic),
            (["openai-codex"], "openai-codex", codex),
            (
                ["github-copilot", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": codex["slow"]},
            ),
            (
                ["anthropic", "github-copilot"],
                "anthropic",
                {"default": anthropic["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "openai-codex"],
                "anthropic",
                {"default": anthropic["default"], "task": anthropic["task"], "smol": anthropic["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": anthropic["task"], "smol": anthropic["smol"], "slow": codex["slow"]},
            ),
            (
                ["anthropic", "github-copilot", "openai-codex"],
                "anthropic",
                {"default": anthropic["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": anthropic["slow"]},
            ),
            (
                ["anthropic", "github-copilot", "openai-codex"],
                "openai-codex",
                {"default": codex["default"], "task": copilot["task"], "smol": copilot["smol"], "slow": codex["slow"]},
            ),
        ]
        worker_order = ["github-copilot", "anthropic", "openai-codex"]
        expected_candidates = {
            "github-copilot": {
                "default": [copilot["default"]],
                "task": [copilot["task"], copilot["smol"]],
                "smol": [copilot["smol"], copilot["task"]],
                "slow": [
                    copilot["slow"],
                    "github-copilot/worker-model:high",
                    "github-copilot/small-model:high",
                ],
            },
            "anthropic": {
                "default": [anthropic["default"]],
                "task": [anthropic["task"], anthropic["default"]],
                "smol": [anthropic["smol"], anthropic["task"]],
                "slow": [
                    anthropic["slow"],
                    "anthropic/interactive-model:high",
                ],
            },
            "openai-codex": {
                role: [selector] for role, selector in codex.items()
            },
        }

        for providers, primary, expected_primaries in cases:
            with self.subTest(providers=providers, primary=primary):
                primaries, fallbacks, unavailable = model_routing.resolve_omp_routing(
                    recommendations, providers, primary, available_for(providers)
                )
                self.assertEqual(primaries, expected_primaries)
                self.assertEqual(unavailable, [])
                premium_order = [
                    primary,
                    *(
                        provider
                        for provider in ("anthropic", "openai-codex")
                        if provider in providers and provider != primary
                    ),
                    *(
                        provider
                        for provider in ("github-copilot",)
                        if provider in providers and provider != primary
                    ),
                ]
                for role in ("default", "slow"):
                    self.assertEqual(
                        fallbacks[role],
                        [
                            selector
                            for provider in premium_order
                            for selector in expected_candidates[provider][role]
                        ],
                    )
                for role in ("task", "smol"):
                    self.assertEqual(
                        fallbacks[role],
                        [
                            selector
                            for provider in worker_order
                            if provider in providers
                            for selector in expected_candidates[provider][role]
                        ],
                    )

        duplicate_recommendations = {
            "providers": {
                "anthropic": {
                    "roles": {
                        "default": ["anthropic/model", "anthropic/model"],
                        "task": ["anthropic/task"],
                        "smol": ["anthropic/missing"],
                        "slow": ["anthropic/model:high", "anthropic/model:high"],
                    }
                }
            }
        }
        primaries, fallbacks, unavailable = model_routing.resolve_omp_routing(
            duplicate_recommendations,
            ["anthropic"],
            "anthropic",
            {"anthropic/model", "anthropic/task"},
        )
        self.assertEqual(
            primaries,
            {
                "default": "anthropic/model",
                "task": "anthropic/task",
                "slow": "anthropic/model:high",
            },
        )
        self.assertEqual(fallbacks["default"], ["anthropic/model"])
        self.assertEqual(fallbacks["slow"], ["anthropic/model:high"])
        self.assertEqual(unavailable, ["smol"])

    def test_configure_omp_routing_persists_compact_intent_and_preserves_omp_values(self) -> None:
        selectors = [
            "github-copilot/interactive-model",
            "github-copilot/worker-model",
            "github-copilot/small-model",
            "openai-codex/interactive-model",
            "openai-codex/utility-model",
        ]
        values = {
            "modelRoles": {"custom": "private/keep", "default": "old/model"},
            "retry.fallbackChains": {"custom": ["private/keep"], "default": ["old/model"]},
            "task.agentModelOverrides": {"reviewer": "@slow"},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            runner = runtime.Runner("ubuntu")

            def persisted_before_omp(_command: list[str], _label: str) -> None:
                saved = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
                self.assertEqual(saved["primaryProvider"], "openai-codex")

            with (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run", side_effect=persisted_before_omp) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner), 0)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["ompRouting"]["providers"], ["github-copilot", "openai-codex"])
            self.assertEqual(saved["ompRouting"]["primaryProvider"], "openai-codex")
            self.assertNotIn("roles", saved["ompRouting"])
            self.assertEqual(saved["ompRouting"]["agentModelOverrides"], {"sonic": "@smol", "task": "@task"})
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)

            payloads = {call.args[0][3]: json.loads(call.args[0][4]) for call in run.call_args_list}
            self.assertEqual(
                payloads["modelRoles"],
                {
                    "custom": "private/keep",
                    "default": "openai-codex/interactive-model",
                    "task": "github-copilot/worker-model",
                    "smol": "github-copilot/small-model",
                    "slow": "openai-codex/interactive-model:high",
                },
            )
            self.assertEqual(
                payloads["retry.fallbackChains"],
                {
                    "custom": ["private/keep"],
                    "default": ["openai-codex/interactive-model", "github-copilot/interactive-model"],
                    "task": [
                        "github-copilot/worker-model",
                        "github-copilot/small-model",
                        "openai-codex/interactive-model",
                    ],
                    "smol": [
                        "github-copilot/small-model",
                        "github-copilot/worker-model",
                        "openai-codex/utility-model",
                    ],
                    "slow": [
                        "openai-codex/interactive-model:high",
                        "github-copilot/interactive-model:high",
                        "github-copilot/worker-model:high",
                        "github-copilot/small-model:high",
                    ],
                },
            )
            self.assertEqual(
                payloads["task.agentModelOverrides"],
                {"reviewer": "@slow", "sonic": "@smol", "task": "@task"},
            )

    def test_configure_omp_routing_dry_run_changes_nothing(self) -> None:
        selectors = [
            "github-copilot/interactive-model",
            "github-copilot/worker-model",
            "github-copilot/small-model",
            "openai-codex/interactive-model",
            "openai-codex/utility-model",
        ]
        values = {
            "modelRoles": {},
            "retry.fallbackChains": {},
            "task.agentModelOverrides": {},
            "retry.modelFallback": False,
            "retry.usageAwareFallback": False,
            "retry.usageReservePct": 0,
            "retry.usageReservePolicy": "confirm",
            "retry.fallbackRevertPolicy": "never",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            before = path.read_bytes()
            runner = runtime.Runner("ubuntu", dry_run=True)
            report = io.StringIO()
            with (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(report),
            ):
                self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner), 0)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
            run.assert_not_called()
            preview = report.getvalue()
            for expected in ("github-copilot", "openai-codex", "default", "task", "smol", "slow"):
                self.assertIn(expected, preview)
            self.assertNotIn("omp config set modelRoles", preview)
            self.assertNotIn('"primaryProvider":', preview)
            self.assertIn("Dry run", preview)

    def test_configure_omp_routing_is_idempotent(self) -> None:
        selectors = [
            "github-copilot/interactive-model",
            "github-copilot/worker-model",
            "github-copilot/small-model",
            "openai-codex/interactive-model",
            "openai-codex/utility-model",
        ]
        routing = self.compact_routing(["github-copilot", "openai-codex"], "openai-codex")
        values = {
            "modelRoles": {
                "custom": "private/keep",
                "default": "openai-codex/interactive-model",
                "task": "github-copilot/worker-model",
                "smol": "github-copilot/small-model",
                "slow": "openai-codex/interactive-model:high",
            },
            "retry.fallbackChains": {
                "custom": ["private/keep"],
                "default": ["openai-codex/interactive-model", "github-copilot/interactive-model"],
                "task": [
                    "github-copilot/worker-model",
                    "github-copilot/small-model",
                    "openai-codex/interactive-model",
                ],
                "smol": [
                    "github-copilot/small-model",
                    "github-copilot/worker-model",
                    "openai-codex/utility-model",
                ],
                "slow": [
                    "openai-codex/interactive-model:high",
                    "github-copilot/interactive-model:high",
                    "github-copilot/worker-model:high",
                    "github-copilot/small-model:high",
                ],
            },
            "task.agentModelOverrides": {"reviewer": "@slow", **routing["agentModelOverrides"]},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 10,
            "retry.usageReservePolicy": "auto",
            "retry.fallbackRevertPolicy": "cooldown-expiry",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = routing
            path.write_text(json.dumps(manifest), encoding="utf-8")
            before = path.read_bytes()
            runner = runtime.Runner("ubuntu")
            with (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
            run.assert_not_called()

    def test_configure_omp_routing_migrates_static_roles(self) -> None:
        selectors = ["openai-codex/interactive-model", "openai-codex/utility-model"]
        values = {
            "modelRoles": {
                "default": "openai-codex/interactive-model",
                "task": "openai-codex/interactive-model",
                "smol": "openai-codex/utility-model",
                "slow": "openai-codex/interactive-model:high",
            },
            "retry.fallbackChains": {
                "default": ["openai-codex/interactive-model"],
                "task": ["openai-codex/interactive-model"],
                "smol": ["openai-codex/utility-model"],
                "slow": ["openai-codex/interactive-model:high"],
            },
            "task.agentModelOverrides": {"sonic": "@slow", "reviewer": "@task"},
            "retry.modelFallback": True,
            "retry.usageAwareFallback": True,
            "retry.usageReservePct": 23,
            "retry.usageReservePolicy": "fail-closed",
            "retry.fallbackRevertPolicy": "never",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            raw = self.minimal_manifest("~/.omp/agent/mcp.json")
            raw["ompRouting"] = {
                "roles": {"default": ["old/exact-model"]},
                "agentModelOverrides": {"sonic": "@slow", "reviewer": "@task"},
                "usageReservePct": 23,
                "usageReservePolicy": "fail-closed",
                "fallbackRevertPolicy": "never",
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            manifest = manifests.load_manifest(path, allow_legacy_routing=True)
            runner = runtime.Runner("ubuntu")
            with (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner), 0)

            run.assert_not_called()
            backups = list(path.parent.glob("stack.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("roles", json.loads(backups[0].read_text(encoding="utf-8"))["ompRouting"])
            routing = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
            self.assertEqual(routing["providers"], ["openai-codex"])
            self.assertEqual(routing["primaryProvider"], "openai-codex")
            self.assertNotIn("roles", routing)
            self.assertEqual(routing["agentModelOverrides"], {"sonic": "@slow", "reviewer": "@task"})
            self.assertEqual(
                (routing["usageReservePct"], routing["usageReservePolicy"], routing["fallbackRevertPolicy"]),
                (23, "fail-closed", "never"),
            )

    def test_configure_omp_routing_preflight_failures_change_nothing(self) -> None:
        valid_codex = json.dumps(
            {
                "models": [
                    {"selector": "openai-codex/interactive-model"},
                    {"selector": "openai-codex/utility-model"},
                ]
            }
        )
        cases = [
            ("malformed recommendations", lambda _command: "", None, runtime.DotAiError("bad catalog"), True),
            ("malformed model catalog", lambda _command: "{", None, None, False),
            (
                "no supported provider",
                lambda _command: json.dumps({"models": [{"selector": "private/model"}]}),
                None,
                None,
                True,
            ),
            (
                "stale recommendations",
                lambda _command: json.dumps({"models": [{"selector": "anthropic/unknown-model"}]}),
                None,
                None,
                True,
            ),
            (
                "unavailable managed role",
                lambda _command: json.dumps({"models": [{"selector": "openai-codex/interactive-model"}]}),
                None,
                None,
                False,
            ),
            (
                "malformed OMP configuration",
                lambda command: valid_codex if command == ["omp", "models", "--json"] else "{",
                None,
                None,
                False,
            ),
            ("unavailable primary choice", lambda _command: valid_codex, "anthropic", None, True),
        ]
        for name, output, requested, catalog_error, raises in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "stack.json"
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                manifest["ompRouting"] = None
                path.write_text(json.dumps(manifest), encoding="utf-8")
                before = path.read_bytes()
                runner = runtime.Runner("ubuntu")
                loader = (
                    mock.patch.object(model_routing, "load_routing_recommendations", side_effect=catalog_error)
                    if catalog_error
                    else self.mock_routing_catalog()
                )
                with (
                    loader,
                    mock.patch.object(runner, "output", side_effect=output),
                    mock.patch.object(runner, "run") as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if raises:
                        with self.assertRaises(runtime.DotAiError):
                            model_routing.configure_omp_routing(manifest, path, runner, requested)
                    else:
                        self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner, requested), 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob("stack.json.bak.*")), [])
                run.assert_not_called()

    def test_configured_omp_value_requires_a_json_value_object(self) -> None:
        runner = runtime.Runner("ubuntu")
        with mock.patch.object(runner, "output", return_value=json.dumps({"key": "modelRoles", "value": {"default": "model"}})):
            self.assertEqual(model_routing.configured_omp_value(runner, "modelRoles"), {"default": "model"})
        for output in ("", "[]", "{", json.dumps({}), json.dumps({"value": None})):
            with self.subTest(output=output), mock.patch.object(runner, "output", return_value=output):
                self.assertIsNone(model_routing.configured_omp_value(runner, "modelRoles"))

    def test_configure_omp_routing_returns_failure_after_manifest_persistence(self) -> None:
        selectors = ["openai-codex/interactive-model", "openai-codex/utility-model"]
        values = {
            "modelRoles": {},
            "retry.fallbackChains": {},
            "task.agentModelOverrides": {},
            "retry.modelFallback": False,
            "retry.usageAwareFallback": False,
            "retry.usageReservePct": 0,
            "retry.usageReservePolicy": "confirm",
            "retry.fallbackRevertPolicy": "never",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            runner = runtime.Runner("ubuntu")

            def fail_first_write(_command: list[str], label: str) -> None:
                if not runner.failures:
                    runner.failures.append(label)

            with (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", side_effect=self.omp_output(selectors, values)),
                mock.patch.object(runner, "run", side_effect=fail_first_write),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(model_routing.configure_omp_routing(manifest, path, runner), 1)

            saved = json.loads(path.read_text(encoding="utf-8"))["ompRouting"]
            self.assertEqual(saved["providers"], ["openai-codex"])
            self.assertNotIn("roles", saved)
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)

    def test_runner_formats_only_original_supported_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "{python}"
            literal_json = '{"literal":{"braces":true}}'
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                runner = runtime.Runner("ubuntu")
                self.assertEqual(runner.argv(["tool", "{home}"]), ["tool", str(home)])
                self.assertEqual(
                    runner.argv(["{home}", "{repo}", "{python}", literal_json]),
                    [str(home), str(runtime.ROOT), sys.executable, literal_json],
                )


    def test_omp_routing_status_reports_ok_for_compact_intent(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        original_manifest = json.loads(json.dumps(manifest))
        runner = runtime.Runner("ubuntu")
        with (
            self.mock_routing_catalog(),
            mock.patch.object(
                runner, "output", side_effect=self.omp_output(selectors, values)
            ),
            mock.patch.object(runner, "run") as run,
            mock.patch.object(model_routing, "choose_primary_provider", side_effect=AssertionError("status must not select or prompt"),),
        ):
            self.assertEqual(
                model_routing.omp_routing_status(manifest, runner),
                ("OK", "configured roles match"),
            )
        run.assert_not_called()
        self.assertEqual(manifest, original_manifest)

    def test_omp_routing_status_reports_provider_selection_drift(self) -> None:
        manifest, selectors, _ = self.compact_status_case()
        cases = {
            "provider added": [*selectors, "openai-codex/interactive-model"],
            "provider removed": selectors[:2],
        }
        for name, available in cases.items():
            with self.subTest(name=name):
                runner = runtime.Runner("ubuntu")
                with (
                    self.mock_routing_catalog(),
                    mock.patch.object(
                        runner,
                        "output",
                        return_value=json.dumps(
                            {
                                "models": [
                                    {"selector": selector} for selector in available
                                ]
                            }
                        ),
                    ) as output,
                    mock.patch.object(runner, "run") as run,
                    mock.patch.object(model_routing, "choose_primary_provider", side_effect=AssertionError("status must not select or prompt"),),
                ):
                    self.assertEqual(
                        model_routing.omp_routing_status(manifest, runner),
                        (
                            "DRIFT",
                            "authenticated providers changed; run 'dotai configure omp-routing'",
                        ),
                    )
                output.assert_called_once_with(["omp", "models", "--json"])
                run.assert_not_called()

    def test_omp_routing_status_reports_omp_drift(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        cases = [
            (
                "modelRoles",
                {**values["modelRoles"], "default": "old/model"},
                "model role differs: default",
            ),
            (
                "retry.fallbackChains",
                {**values["retry.fallbackChains"], "task": ["old/model"]},
                "fallback chain differs: task",
            ),
            (
                "task.agentModelOverrides",
                {**values["task.agentModelOverrides"], "sonic": "old/model"},
                "agent override differs: sonic",
            ),
            ("retry.modelFallback", False, "retry.modelFallback differs"),
            (
                "retry.usageAwareFallback",
                False,
                "retry.usageAwareFallback differs",
            ),
            ("retry.usageReservePct", 5, "retry.usageReservePct differs"),
            (
                "retry.usageReservePolicy",
                "confirm",
                "retry.usageReservePolicy differs",
            ),
            (
                "retry.fallbackRevertPolicy",
                "never",
                "retry.fallbackRevertPolicy differs",
            ),
        ]
        for key, changed, detail in cases:
            with self.subTest(detail=detail):
                runner = runtime.Runner("ubuntu")
                current = {**values, key: changed}
                with (
                    self.mock_routing_catalog(),
                    mock.patch.object(
                        runner,
                        "output",
                        side_effect=self.omp_output(selectors, current),
                    ),
                    mock.patch.object(runner, "run") as run,
                    mock.patch.object(model_routing, "choose_primary_provider", side_effect=AssertionError("status must not select or prompt"),),
                ):
                    self.assertEqual(
                        model_routing.omp_routing_status(manifest, runner),
                        ("DRIFT", detail),
                    )
                run.assert_not_called()

        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["ompRouting"] = self.compact_routing(["anthropic"], "anthropic")
        runner = runtime.Runner("ubuntu")
        with (
            self.mock_routing_catalog(),
            mock.patch.object(
                runner,
                "output",
                return_value=json.dumps(
                    {"models": [{"selector": "anthropic/legacy-interactive-model"}]}
                ),
            ) as output,
            mock.patch.object(runner, "run") as run,
            mock.patch.object(model_routing, "choose_primary_provider", side_effect=AssertionError("status must not select or prompt"),),
        ):
            self.assertEqual(
                model_routing.omp_routing_status(manifest, runner),
                ("DRIFT", "unavailable roles: smol"),
            )
        output.assert_called_once_with(["omp", "models", "--json"])
        run.assert_not_called()

    def test_omp_routing_status_reports_inactive_and_fail(self) -> None:
        manifest, selectors, values = self.compact_status_case()
        runner = runtime.Runner("ubuntu")

        with (
            self.mock_routing_catalog(),
            mock.patch.object(
                runner,
                "output",
                return_value=json.dumps(
                    {"models": [{"selector": "openai-codex/interactive-model"}]}
                ),
            ) as output,
            mock.patch.object(runner, "run") as run,
        ):
            self.assertEqual(
                model_routing.omp_routing_status(manifest, runner),
                ("INACTIVE", "configured providers are unavailable"),
            )
        output.assert_called_once_with(["omp", "models", "--json"])
        run.assert_not_called()

        failures = [
            (
                mock.patch.object(model_routing, "load_routing_recommendations", side_effect=runtime.DotAiError("malformed recommendations"),),
                mock.patch.object(runner, "output"),
                ("FAIL", "unable to read routing recommendations"),
            ),
            (
                self.mock_routing_catalog(),
                mock.patch.object(runner, "output", return_value="{"),
                ("FAIL", "unable to read OMP model catalog"),
            ),
            (
                self.mock_routing_catalog(),
                mock.patch.object(
                    runner,
                    "output",
                    side_effect=self.omp_output(
                        selectors,
                        {**values, "modelRoles": None},
                    ),
                ),
                ("FAIL", "unable to read required OMP configuration"),
            ),
        ]
        for loader, output_patch, expected in failures:
            with self.subTest(expected=expected), loader, output_patch as output, mock.patch.object(
                runner, "run"
            ) as run:
                self.assertEqual(model_routing.omp_routing_status(manifest, runner), expected)
            run.assert_not_called()

        for routing in (None, "absent"):
            with self.subTest(routing=routing):
                unconfigured = self.minimal_manifest("~/.omp/agent/mcp.json")
                if routing is None:
                    unconfigured["ompRouting"] = None
                with (
                    mock.patch.object(model_routing, "load_routing_recommendations", side_effect=AssertionError("recommendations must not load"),),
                    mock.patch.object(
                        runner,
                        "output",
                        side_effect=AssertionError("OMP must not be queried"),
                    ),
                    mock.patch.object(runner, "run") as run,
                ):
                    self.assertEqual(
                        model_routing.omp_routing_status(unconfigured, runner),
                        ("OK", "not configured in manifest"),
                    )
                run.assert_not_called()

    def test_print_status_reports_configured_routing_health(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        runner = runtime.Runner("ubuntu")
        manifest["ompRouting"] = self.compact_routing(["anthropic"], "anthropic")
        terminal.configure_color("always")
        self.addCleanup(terminal.configure_color, "never")
        cases = [
            ("OK", "configured roles match", True, "\033[32;1m[OK]\033[0m"),
            (
                "DRIFT",
                "model role differs: default",
                False,
                "\033[33;1m[DRIFT]\033[0m",
            ),
            (
                "INACTIVE",
                "configured providers are unavailable",
                False,
                "\033[33;1m[INACTIVE]\033[0m",
            ),
            (
                "FAIL",
                "unable to read OMP model catalog",
                False,
                "\033[31;1m[FAIL]\033[0m",
            ),
        ]
        for label, detail, healthy, colored_badge in cases:
            with self.subTest(label=label):
                output = io.StringIO()
                with (
                    mock.patch.object(mcp_config, "mcp_status", return_value=(True, "managed")),
                    mock.patch.object(model_routing, "omp_routing_status", return_value=(label, detail)),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(health.print_status(manifest, runner), healthy)
                self.assertIn("OMP routing:", output.getvalue())
                self.assertIn(f"{colored_badge} {detail}", output.getvalue())

    def test_omp_extension_reconciliation_preserves_existing_entries(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        managed = "~/.pi/agent/extensions/rtk.ts"
        manifest["ompExtensions"] = [managed]
        runner = runtime.Runner("ubuntu")
        current = json.dumps({"key": "extensions", "value": ["~/custom/extension.ts"]})

        with (
            mock.patch.object(runner, "output", return_value=current),
            mock.patch.object(runner, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            omp_config.reconcile_omp_extensions(manifest, runner)

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["omp", "config", "set", "extensions"])
        self.assertEqual(
            json.loads(command[4]),
            ["~/custom/extension.ts", managed],
        )

        dry_runner = runtime.Runner("ubuntu", dry_run=True)
        output = io.StringIO()
        with (
            mock.patch.object(dry_runner, "output", return_value=""),
            contextlib.redirect_stdout(output),
        ):
            omp_config.reconcile_omp_extensions(manifest, dry_runner)
        self.assertFalse(dry_runner.failures)
        self.assertIn("OMP extensions: configure", output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / ".pi" / "agent" / "extensions" / "rtk.ts"
            source.parent.mkdir(parents=True)
            source.write_text("// test extension\n", encoding="utf-8")
            configured = json.dumps({"key": "extensions", "value": [managed]})
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}),
                mock.patch.object(runner, "output", return_value=configured),
            ):
                healthy, detail = omp_config.omp_extension_status(manifest, runner)
            self.assertTrue(healthy, detail)

    def test_skill_status_distinguishes_codex_plugin_from_pi_install(self) -> None:
        skill = {"source": "owner/skills", "agent": "pi", "checkSkills": ["alpha", "beta"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cache = home / ".codex" / "plugins" / "cache" / "owner" / "plugin" / "1.0.0" / "skills"
            for name in skill["checkSkills"]:
                path = cache / name / "SKILL.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {name}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                installed, detail = skill_manager.skill_status(skill)
                self.assertFalse(installed)
                self.assertIn("Codex plugin", detail)
                self.assertIn("inactive in OMP", detail)
                manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                manifest["skills"] = [skill]
                manifest["mcp"]["servers"] = {}
                output = io.StringIO()
                terminal.configure_color("never")
                with contextlib.redirect_stdout(output):
                    self.assertFalse(health.print_status(manifest, runtime.Runner("ubuntu")))
                self.assertIn("[INACTIVE]", output.getvalue())
                for name in skill["checkSkills"]:
                    path = home / ".pi" / "agent" / "skills" / name / "SKILL.md"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"# {name}\n", encoding="utf-8")
                installed, detail = skill_manager.skill_status(skill)
                self.assertTrue(installed)
                self.assertEqual(detail, "installed for pi")
    def test_universal_skill_target_uses_omp_discovery_path(self) -> None:
        skill = {"source": "owner/skills", "agent": "universal", "checkSkills": ["alpha"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\nname: alpha\ndescription: test\n---\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}):
                installed, detail = skill_manager.skill_status(skill)
            self.assertTrue(installed)
            self.assertEqual(detail, "installed for universal")

    def github_skill_lock_entry(self, source: str, folder_hash: str, name: str = "alpha") -> dict:
        return {
            "source": source,
            "sourceType": "github",
            "sourceUrl": f"https://github.com/{source}.git",
            "skillPath": f"skills/{name}/SKILL.md",
            "skillFolderHash": folder_hash,
            "ref": None,
            "installedAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def test_sync_uses_xdg_skill_lock_for_healthy_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8", newline="\n")
            entry = self.github_skill_lock_entry("owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298")
            xdg = home / "state"
            lock = xdg / "skills" / ".skill-lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_text(json.dumps({"version": 3, "skills": {"alpha": entry}}), encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry("owner/old", entry["skillFolderHash"])},
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": str(xdg), "GH_HOST": "github.com"}),
                contextlib.redirect_stdout(output),
            ):
                skill_manager.reconcile_skills(manifest, runtime.Runner("linux", dry_run=True))
            self.assertNotIn("npx", output.getvalue())
            self.assertIn("already installed", output.getvalue())

    def test_sync_recognizes_equivalent_github_repository_sources(self) -> None:
        sources = (
            "https://github.com/owner/skills",
            "https://github.com/owner/skills.git",
            "https://github.com/owner/skills/",
            "github:owner/skills",
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8", newline="\n")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298",
                )},
            }), encoding="utf-8")
            for source in sources:
                with self.subTest(source=source):
                    manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                    manifest["skills"] = [{"source": source, "checkSkills": ["alpha"]}]
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        skill_manager.reconcile_skills(manifest, runtime.Runner("linux", dry_run=True))
                    self.assertNotIn("npx", output.getvalue())
                    self.assertIn("already installed", output.getvalue())

    def test_sync_does_not_attribute_another_agents_copy_from_global_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            universal = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            codex = home / ".codex" / "skills" / "alpha" / "SKILL.md"
            for target, content in ((universal, "# Old alpha\n"), (codex, "# New alpha\n")):
                target.parent.mkdir(parents=True)
                target.write_text(content, encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/new", "c7c75786a2a668d0a7dcd9964d3e0ce15ba5f7a2",
                )},
                "lastSelectedAgents": ["codex"],
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/new", "agent": "universal", "checkSkills": ["alpha"]}]
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                contextlib.redirect_stdout(output),
            ):
                runner = runtime.Runner("linux", dry_run=True)
                skill_manager.reconcile_skills(manifest, runner)
            self.assertTrue(runner.failures)
            self.assertEqual(codex.read_text(encoding="utf-8"), "# New alpha\n")
            self.assertEqual(universal.read_text(encoding="utf-8"), "# Old alpha\n")

    def test_sync_does_not_merge_distinct_hosts_refs_or_subpaths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            cases = [
                ("owner/skills", {
                    "source": "owner/skills", "sourceType": "gitlab",
                    "sourceUrl": "https://gitlab.com/owner/skills.git",
                }),
                ("owner/skills/other-subpath", {}),
                ("https://github.com/owner/skills/tree/main/other-subpath", {}),
                ("https://example.com/owner/skills.git", {}),
                ("https://github.com/owner/other", {}),
            ]
            for source, fields in cases:
                with self.subTest(source=source, fields=fields):
                    entry = self.github_skill_lock_entry("owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298")
                    entry.update(fields)
                    (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                        "version": 3, "skills": {"alpha": entry},
                    }), encoding="utf-8")
                    manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
                    manifest["skills"] = [{"source": source, "checkSkills": ["alpha"]}]
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        runner = runtime.Runner("linux", dry_run=True)
                        skill_manager.reconcile_skills(manifest, runner)
                    self.assertTrue(runner.failures)
            entry = self.github_skill_lock_entry("owner/skills", "0ae35bdd602b22221c7503baa29f72fd9f115298")
            entry["ref"] = "old-branch"
            (home / ".agents/.skill-lock.json").write_text(json.dumps({"version": 3, "skills": {"alpha": entry}}))
            self.resolve_skill_fixture("owner/skills", {"alpha": "# Alpha\n"})
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": ""}):
                runner = runtime.Runner("linux", dry_run=True)
                with contextlib.redirect_stdout(io.StringIO()):
                    skill_manager.reconcile_skills(manifest, runner)
                self.assertFalse(runner.failures)
                manifest["skills"][0]["revision"] = self.fixture_revision
                with self.assertRaises(runtime.DotAiError):
                    locking.prepare(manifest, home / "stack.json", runner, "sync")
            self.assertEqual(target.read_text(), "# Alpha\n")

    def test_sync_checks_supporting_files_before_skipping_owned_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / ".agents" / "skills" / "alpha"
            (folder / "references").mkdir(parents=True)
            (folder / "SKILL.md").write_text("# Alpha\n", encoding="utf-8", newline="\n")
            guide = folder / "references" / "guide.md"
            guide.write_text("old\n", encoding="utf-8", newline="\n")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "920656539539d9ffa86223950d6b3883e72b0e32",
                )},
            }), encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "checkSkills": ["alpha"]}]
            for content, refresh in (("old\n", True), ("new\n", False)):
                with self.subTest(content=content):
                    guide.write_text(content, encoding="utf-8", newline="\n")
                    output = io.StringIO()
                    with (
                        mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                        contextlib.redirect_stdout(output),
                    ):
                        runner = runtime.Runner("linux", dry_run=True)
                        skill_manager.reconcile_skills(manifest, runner)
                    self.assertEqual(bool(runner.failures), refresh)


    def test_sync_leaves_healthy_skills_untouched_by_default(self) -> None:
        manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
        manifest["skills"] = [{
            "source": "owner/skills",
            "agent": "universal",
            "skills": ["alpha", "beta"],
            "checkSkills": ["alpha", "beta"],
        }]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in ("alpha", "beta"):
                target = home / ".agents" / "skills" / name / "SKILL.md"
                target.parent.mkdir(parents=True)
                target.write_text(f"# {name}\n", encoding="utf-8", newline="\n")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {
                    "alpha": self.github_skill_lock_entry("owner/skills", "51937797c336f38c66ecfb342d9cc37ac2c56a74"),
                    "beta": self.github_skill_lock_entry("owner/skills", "d4228d69961f61bf69edc471a90918494661c475", "beta"),
                },
            }), encoding="utf-8")
            plan = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(subprocess, "run", side_effect=AssertionError("healthy skills must not fetch")),
                contextlib.redirect_stdout(plan),
            ):
                skill_manager.reconcile_skills(manifest, runtime.Runner("linux"))


    def test_install_force_refreshes_an_existing_skill_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Alpha\n", encoding="utf-8")
            manifest_path = home / "stack.json"
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{
                "source": "owner/skills", "agent": "universal", "skills": ["alpha"], "checkSkills": ["alpha"],
            }]
            manifest_path.write_text(json.dumps(manifest))
            plan = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}), contextlib.redirect_stdout(plan):
                self.own_skill_fixture(target.parent, "owner/skills")
                self.resolve_skill_fixture("owner/skills", {"alpha": "# Refreshed Alpha\n"})
                self.assertEqual(reconciliation.reconcile(
                    manifest, manifest_path, runtime.Runner("ubuntu"), "install", force=True,
                ), 0, plan.getvalue())
            self.assertEqual(target.read_text(), "# Refreshed Alpha\n")

    def test_sync_installs_skills_when_any_required_skill_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{
                "source": "owner/skills", "agent": "universal", "revision": self.fixture_revision,
                "skills": ["alpha", "beta"], "checkSkills": ["alpha", "beta"],
            }]
            (home / "stack.json").write_text(json.dumps(manifest))
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# alpha\n")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": str(home / "xdg")}):
                self.own_skill_fixture(target.parent, "owner/skills")
                self.resolve_skill_fixture("owner/skills", {"alpha": "# alpha\n", "beta": "# beta\n"})
                with contextlib.redirect_stdout(io.StringIO()):
                    result = reconciliation.reconcile(manifest, home / "stack.json", runtime.Runner("linux"), "sync")
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(), "# alpha\n")
            self.assertEqual((target.parent.parent / "beta/SKILL.md").read_text(), "# beta\n")
            self.assertEqual(json.loads((home / "stack.lock.json").read_text())["skills"]["owner/skills|universal"]["installerVersion"], "1.7.1")


    def test_sync_update_skills_explicitly_refreshes_healthy_installations(self) -> None:
        self.resolve_skill_fixture("owner/skills", {"alpha": "# refreshed alpha\n"})
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# alpha\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3, "skills": {"alpha": self.github_skill_lock_entry(
                    "owner/skills", "51937797c336f38c66ecfb342d9cc37ac2c56a74",
                )},
            }), encoding="utf-8")
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{
                "source": "owner/skills",
                "agent": "universal",
                "skills": ["alpha"],
                "checkSkills": ["alpha"],
            }]
            path = home / "stack.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(releases, "print_release_notice"),
                contextlib.redirect_stdout(plan),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        result = cli.main(["--manifest", str(path), "sync", "--update-skills"])
                    except SystemExit as exc:
                        result = exc.code
                self.assertEqual(result, 0)
            self.assertEqual(target.read_text(), "# refreshed alpha\n")
            self.assertEqual(json.loads(path.read_text()), manifest)


    def test_status_highlights_legacy_pi_skill_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            skill_path = home / ".pi" / "agent" / "skills" / "legacy" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# legacy\n", encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "agent": "pi", "checkSkills": ["legacy"]}]
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                healthy = health.print_status(manifest, runtime.Runner("ubuntu"))
            self.assertFalse(healthy)
            self.assertIn("[DRIFT] Legacy Pi skill targets", output.getvalue())
            self.assertIn("Run 'dotai fix'", output.getvalue())


    def test_status_color_can_be_forced_or_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["mcp"]["servers"] = {}
            path.write_text(json.dumps(manifest), encoding="utf-8")

            colored = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}, clear=False):
                with contextlib.redirect_stdout(colored):
                    self.assertEqual(
                        cli.main(["--manifest", str(path), "--color", "always", "status"]),
                        0,
                    )
            self.assertIn("\033[", colored.getvalue())

            plain = io.StringIO()
            with contextlib.redirect_stdout(plain):
                self.assertEqual(
                    cli.main(["--manifest", str(path), "--color", "never", "status"]),
                    0,
                )
            self.assertNotIn("\033[", plain.getvalue())

    def test_add_mcp_invalid_port_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["localOnly"] = {"owner": "user"}
            manifest["mcp"]["servers"]["custom"] = {
                "command": "npx", "env": {"TOKEN": "LOCAL_API_TOKEN"},
                "providerSetting": {"keep": True},
            }
            original = (json.dumps(manifest, indent=4) + "\n\n").encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli.main([
                        "--manifest", str(path), "add", "mcp", "bad", "--url",
                        "https://example.test:not-a-port/mcp",
                    ]),
                    2,
                )
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(
                    cli.main([
                        "--manifest", str(path), "add", "mcp", "bad", "--url",
                        "https://example.test:443/mcp", "--header", "Authorization=API_TOKEN",
                    ]),
                    0,
                )
            updated = manifests.load_manifest(path)
            self.assertEqual(updated["mcp"]["servers"]["bad"], {
                "type": "http", "url": "https://example.test:443/mcp",
                "headers": {"Authorization": "API_TOKEN"},
            })
            self.assertEqual(updated["localOnly"], {"owner": "user"})
            self.assertEqual(updated["mcp"]["servers"]["custom"], manifest["mcp"]["servers"]["custom"])

    def test_add_plugin_invalid_id_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            original = json.dumps(self.minimal_manifest("mcp.json"), indent=4).encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--manifest", str(path), "add", "plugin", "bad"]), 2)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(cli.main(["--manifest", str(path), "add", "plugin", "review@team"]), 0)
            self.assertEqual(manifests.load_manifest(path)["plugins"], [{"id": "review@team", "scope": "user"}])

    def test_add_tool_empty_check_preserves_manifest_and_allows_valid_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["packages"] = [{"name": "sample", "managed": True, "check": "sample --version", "install": []}]
            original = json.dumps(manifest, indent=4).encode("utf-8")
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([
                    "--manifest", str(path), "add", "tool", "sample", "--check", "",
                    "--install", "default=sample install",
                ]), 2)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(cli.main([
                    "--manifest", str(path), "add", "tool", "sample", "--check", "sample --version",
                    "--install", "default=sample install",
                ]), 0)
            self.assertEqual(manifests.load_manifest(path)["packages"], [{"name": "sample", "managed": True, "check": "sample --version", "install": {"default": ["sample install"]}, }])

    def test_add_empty_integration_values_do_not_write_manifest(self) -> None:
        commands = [
            ["add", "skill", ""],
            ["add", "marketplace", "team", ""],
            ["add", "mcp", "local", "--command", ""],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            original = json.dumps(self.minimal_manifest("mcp.json"), indent=4).encode("utf-8")
            for command in commands:
                with self.subTest(command=command):
                    path.write_bytes(original)
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(["--manifest", str(path), *command]), 2)
                    self.assertEqual(path.read_bytes(), original)

    def test_add_preserves_unconfigured_routing_for_subsequent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            manifest["ompRouting"] = None
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--manifest", str(path), "add", "plugin", "review@team"]), 0)
                self.assertEqual(cli.main(["--manifest", str(path), "add", "skill", "owner/skills"]), 0)
                self.assertEqual(cli.main(["--manifest", str(path), "validate"]), 0)
            self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["ompRouting"])
            self.assertEqual(manifests.load_manifest(path)["skills"][0]["source"], "owner/skills")

    def test_write_manifest_rejects_invalid_candidate_before_creating_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            manifest = self.minimal_manifest("mcp.json")
            original = json.dumps(manifest, indent=4).encode("utf-8")
            path.write_bytes(original)
            manifest["plugins"] = [{"id": "bad"}]
            with self.assertRaises(runtime.DotAiError):
                manifests.write_manifest(path, manifest)
            self.assertEqual(path.read_bytes(), original)
            missing = root / "new" / "stack.json"
            with self.assertRaises(runtime.DotAiError):
                manifests.write_manifest(missing, manifest)
            self.assertFalse(missing.parent.exists())
            self.assertEqual(set(root.iterdir()), {path})

    def test_init_rejects_invalid_template_before_creating_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "stack.example.json"
            invalid = self.minimal_manifest("mcp.json")
            invalid["plugins"] = [{"id": "bad"}]
            example.write_text(json.dumps(invalid), encoding="utf-8")
            target = root / "new" / "stack.json"
            with (
                mock.patch.object(manifests, "EXAMPLE_MANIFEST", example),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.main(["--manifest", str(target), "init"]), 2)
                self.assertFalse(target.parent.exists())
                example.write_text(json.dumps(self.minimal_manifest("mcp.json")), encoding="utf-8")
                self.assertEqual(cli.main(["--manifest", str(target), "init"]), 0)
            self.assertEqual(manifests.load_manifest(target)["plugins"], [])

    def test_added_named_skill_reports_installed_only_after_skill_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "foo"]), 0)
                before = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(before, 1)
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][0]["checkSkills"], ["foo"])

            skill_file = home / ".agents" / "skills" / "foo" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# foo\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                result = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

    def test_added_uppercase_skill_is_healthy_in_installer_normalized_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "ALPHA"]), 0)

            skill_file = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# ALPHA\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                result = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())
            self.assertNotIn("[MISSING] owner/skills:", output.getvalue())
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["skills"][0]["skills"], ["ALPHA"])

    def test_added_skill_checks_follow_installer_punctuation_and_name_boundaries(self) -> None:
        cases = [
            (".. My__ Skill / V2!..", "my__-skill-v2"),
            ("...---...", "unnamed-skill"),
            ("A" * 256, "a" * 255),
        ]
        for selection, installed_name in cases:
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                manifest_path = home / "stack.json"
                mcp_path = home / "mcp.json"
                mcp_path.write_text("{}", encoding="utf-8")
                manifest = self.minimal_manifest(str(mcp_path))
                manifest["mcp"]["servers"] = {}
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", selection]), 0)

                skill_file = home / ".agents" / "skills" / installed_name / "SKILL.md"
                skill_file.parent.mkdir(parents=True)
                skill_file.write_text("# installed skill\n", encoding="utf-8")
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                    result = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
                self.assertEqual(result, 0, output.getvalue())
                self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())
                saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["skills"][0]["skills"], [selection])

    def test_explicit_skill_check_keeps_exact_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized_file = home / ".agents" / "skills" / "actual-skill" / "SKILL.md"
            normalized_file.parent.mkdir(parents=True)
            normalized_file.write_text("# normalized, not the explicit check\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "ALPHA", "--check-skill", "Actual Skill!"]), 0)
                missing = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(missing, 1, output.getvalue())
            self.assertIn("[MISSING] owner/skills:", output.getvalue())

            exact_file = home / ".agents" / "skills" / "Actual Skill!" / "SKILL.md"
            exact_file.parent.mkdir(parents=True)
            exact_file.write_text("# explicitly checked skill\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                healthy = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(healthy, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

    def test_added_wildcard_skill_reports_unverified_instead_of_missing_or_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills"]), 0)
                result = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 1)
            self.assertIn("[UNVERIFIED] owner/skills:", output.getvalue())
            self.assertNotIn("[MISSING] owner/skills:", output.getvalue())
            self.assertNotIn("[OK] owner/skills:", output.getvalue())

    def test_explicit_skill_check_overrides_named_install_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            mcp_path = home / "mcp.json"
            mcp_path.write_text("{}", encoding="utf-8")
            manifest = self.minimal_manifest(str(mcp_path))
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            skill_file = home / ".agents" / "skills" / "actual" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# actual\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--manifest", str(manifest_path), "add", "skill", "owner/skills", "--skill", "alias", "--check-skill", "actual"]), 0)
                result = cli.main(["--manifest", str(manifest_path), "--color", "never", "status"])
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn("[OK] owner/skills: installed for universal", output.getvalue())

    def test_add_commands_extend_every_supported_integration_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                ["add", "skill", "owner/skills", "--skill", "review", "--check-skill", "review"],
                ["add", "skill", "owner/skills", "--skill", "deploy", "--check-skill", "deploy"],
                ["add", "marketplace", "team", "owner/marketplace"],
                ["add", "plugin", "review@team"],
                ["add", "mcp", "local", "--command", "npx", "--arg=-y", "--arg", "server-package"],
                [
                    "add",
                    "tool",
                    "Example",
                    "--check",
                    "example --version",
                    "--install",
                    "windows=scoop install example",
                    "--install",
                    "linux=curl https://example.test/install | sh",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(cli.main(["--manifest", str(path), *command]), 0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["skills"][0]["source"], "owner/skills")
            self.assertEqual(value["skills"][0]["agent"], "universal")
            self.assertEqual(value["skills"][0]["skills"], ["review", "deploy"])
            self.assertEqual(value["skills"][0]["checkSkills"], ["review", "deploy"])
            self.assertEqual(value["marketplaces"][0]["name"], "team")
            self.assertEqual(value["plugins"][0]["id"], "review@team")
            self.assertEqual(value["mcp"]["servers"]["local"]["args"], ["-y", "server-package"])
            self.assertEqual(value["packages"][0]["install"]["windows"], ["scoop install example"])
            self.assertTrue(value["packages"][0]["managed"])

    def test_add_skill_preserves_existing_other_agent_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [
                {
                    "source": "mattpocock/skills",
                    "agent": "pi",
                    "skills": ["grill-me", "grill-with-docs"],
                    "checkSkills": ["grill-me", "grill-with-docs"],
                },
                {
                    "source": "other/skills",
                    "agent": "pi",
                    "skills": ["other"],
                    "checkSkills": ["other"],
                },
            ]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(
                        [
                            "--manifest",
                            str(path),
                            "add",
                            "skill",
                            "mattpocock/skills",
                            "--agent",
                            "universal",
                            "--skill",
                            "grill-me",
                            "--skill",
                            "grill-with-docs",
                            "--check-skill",
                            "grill-me",
                            "--check-skill",
                            "grill-with-docs",
                        ]
                    ),
                    0,
                )
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"], [
                *manifest["skills"],
                {"source": "mattpocock/skills", "agent": "universal",
                 "skills": ["grill-me", "grill-with-docs"], "checkSkills": ["grill-me", "grill-with-docs"]},
            ])

    def test_sync_reports_invalid_mcp_config_and_exits_unsuccessfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "mcp.json"
            target.write_text("{not valid JSON", encoding="utf-8")
            path = root / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest(str(target))), encoding="utf-8")
            manifest_bytes = path.read_bytes()
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(root / "state")}),
                mock.patch.object(releases, "latest_release_version", return_value=None),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                result = cli.main(["--manifest", str(path), "sync"])
            self.assertEqual(result, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "{not valid JSON")
            self.assertEqual(path.read_bytes(), manifest_bytes)
            self.assertFalse((root / "stack.lock.json").exists())
            self.assertFalse((root / "state" / "state.json").exists())
            self.assertFalse(list(root.glob("mcp.json.bak.*")))
            self.assertFalse(list(root.glob("stack.json.bak.*")))

    def test_sync_does_not_rewrite_existing_skill_agent_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            path = home / "stack.json"
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            manifest["skills"] = [{"source": "fixture/skills", "agent": "pi",
                                   "revision": self.fixture_revision, "skills": ["alpha"],
                                   "checkSkills": ["alpha"]}]
            original = json.dumps(manifest, indent=4) + "\n\n"
            path.write_text(original)
            target = home / ".pi/agent/skills/alpha/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("private pi copy\n")
            with mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": str(home / "xdg")}):
                self.own_skill_fixture(target.parent, "fixture/skills")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["--manifest", str(path), "sync", "--dry-run"]), 0)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(target.read_text(), "private pi copy\n")
            self.assertFalse((home / ".agents/skills/alpha").exists())
            self.assertFalse((home / "stack.lock.json").exists())


    def test_sync_notifies_about_new_recommendations_without_adopting_them(self) -> None:
        added = {
            "source": "owner/added",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "stack.json"
                example_path = root / "stack.example.json"
                manifest = self.minimal_manifest(str(root / "mcp.json"))
                manifest["mcp"]["servers"] = {}
                original = json.dumps(manifest)
                path.write_text(original, encoding="utf-8")
                example_path.write_text(json.dumps({**manifest, "skills": [added]}), encoding="utf-8")
                output = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {
                        "DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(root / "state"),
                        "XDG_STATE_HOME": str(root / "xdg-state"),
                    }),
                    mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                    mock.patch.object(releases, "latest_release_version", return_value=None),
                    mock.patch("builtins.input", side_effect=AssertionError("ordinary sync must not prompt")),
                    contextlib.redirect_stdout(output),
                ):
                    result = cli.main([
                        "--manifest", str(path), "sync", *(["--dry-run"] if dry_run else []),
                    ])
                self.assertEqual(result, 0)
                self.assertIn("owner/added", output.getvalue())
                self.assertIn("new-skill", output.getvalue())
                self.assertIn("--recommended-skills", output.getvalue())
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                self.assertFalse((root / ".agents" / "skills").exists())
                if dry_run:
                    self.assertFalse((root / "state").exists())
                else:
                    history = json.loads((root / "state" / "recommended-skills.json").read_text(encoding="utf-8"))
                    self.assertEqual(history[os.path.normcase(str(path.resolve()))]["skills"], [])

    def test_accepted_recommendation_refreshes_a_healthy_skill_source(self) -> None:
        before = {"source": "owner/recommended", "agent": "universal", "skills": ["*"], "checkSkills": ["keep"]}
        self.resolve_skill_fixture("owner/recommended", {"keep": "# keep\n"})
        after = {**before, "skills": ["keep"]}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest_path = home / "stack.json"
            example_path = home / "stack.example.json"
            manifest = self.minimal_manifest(str(home / "mcp.json"))
            manifest["skills"] = [before]
            manifest["mcp"]["servers"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            example = {**manifest, "skills": [after]}
            example_path.write_text(json.dumps(example), encoding="utf-8")
            skill_file = home / ".agents" / "skills" / "keep" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# keep\n", encoding="utf-8")
            (home / ".agents" / ".skill-lock.json").write_text(json.dumps({
                "version": 3,
                "skills": {"keep": self.github_skill_lock_entry(
                    "owner/recommended", "999550e4425ecd9ea5aeb58fef9f1a05ddf50d86", "keep",
                )},
            }), encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "DOTAI_STATE_DIR": str(home / "state"), "XDG_STATE_HOME": "", "GH_HOST": "github.com"}),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(releases, "latest_release_version", return_value=None),
                contextlib.redirect_stdout(output),
            ):
                self.own_skill_fixture(skill_file.parent, before["source"])
                self.resolve_skill_fixture(before["source"], {"keep": "# refreshed keep\n"})
                with mock.patch("builtins.input", return_value="a"):
                    result = cli.main(["--manifest", str(manifest_path), "sync", "--recommended-skills", "--enforce"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(manifest_path.read_text())["skills"], [after])
            self.assertEqual(skill_file.read_text(), "# refreshed keep\n")
            self.assertEqual(json.loads((home / "stack.lock.json").read_text())["skills"]["owner/recommended|universal"]["revision"], self.fixture_revision)

    def test_recommended_skill_sync_accepts_all_without_overwriting_custom_skills(self) -> None:
        retired = {
            "source": "owner/retired",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        added = {
            "source": "owner/added",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom-skill"],
            "checkSkills": ["custom-skill"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            skill_root = root / ".agents" / "skills"
            for name in ("old-skill", "custom-skill"):
                folder = skill_root / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(name, encoding="utf-8")
            self.own_skill_fixture(skill_root / "custom-skill", "user/custom")
            self.own_skill_fixture(skill_root / "old-skill", "owner/retired")
            self.resolve_skill_fixture("owner/added", {"new-skill": "installed"})

            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired, custom]
            manifest["mcp"]["servers"] = {}
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [added]
            example["mcp"]["servers"] = {}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "state.json").write_text(
                json.dumps({"manifest": str(path.resolve()), "managedRecommendedSkills": [retired]}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(releases, "latest_release_version", return_value=None),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(output),
            ):
                try:
                    result = cli.main(["--manifest", str(path), "sync", "--recommended-skills"])
                except SystemExit as exc:
                    result = exc.code
                self.assertEqual(result, 0)

            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"], [custom, added])
            self.assertEqual(len(list(root.glob("stack.json.bak.*"))), 1)
            self.assertFalse((skill_root / "old-skill").exists())
            self.assertEqual((skill_root / "custom-skill" / "SKILL.md").read_text(encoding="utf-8"), "custom-skill")
            self.assertEqual((skill_root / "new-skill" / "SKILL.md").read_text(encoding="utf-8"), "installed")
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(history[os.path.normcase(str(path.resolve()))], {"version": 1, "skills": [added]})
            self.assertIn("owner/retired", output.getvalue())
            self.assertIn("owner/added", output.getvalue())

    def test_enforced_recommended_skill_sync_adopts_matching_sources_and_removes_retired_skills(self) -> None:
        before = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["*"],
            "checkSkills": ["keep"],
        }
        after = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["keep"],
            "checkSkills": ["keep"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom"],
            "checkSkills": ["custom"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            skill_root = root / ".agents" / "skills"
            for name in ("keep", "retired", "custom"):
                folder = skill_root / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(name, encoding="utf-8")
            self.own_skill_fixture(skill_root / "keep", before["source"])
            self.own_skill_fixture(skill_root / "custom", "user/custom")
            self.own_skill_fixture(skill_root / "retired", before["source"])
            self.resolve_skill_fixture(before["source"], {"keep": "refreshed"})

            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [before, custom]
            manifest["mcp"]["servers"] = {}
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [after]
            example["mcp"]["servers"] = {}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": []}}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(releases, "latest_release_version", return_value=None),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                try:
                    result = cli.main(
                        ["--manifest", str(path), "sync", "--recommended-skills", "--enforce"]
                    )
                except SystemExit as exc:
                    result = exc.code

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["skills"], [after, custom])
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(
                history[os.path.normcase(str(path.resolve()))],
                {"version": 1, "skills": [after]},
            )
            self.assertFalse((skill_root / "retired").exists())
            self.assertEqual((skill_root / "keep" / "SKILL.md").read_text(encoding="utf-8"), "refreshed")
            self.assertEqual((skill_root / "custom" / "SKILL.md").read_text(encoding="utf-8"), "custom")




    def test_installed_skill_listing_uses_universal_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            listing = json.dumps(
                [
                    {
                        "name": "Old Skill!",
                        "path": str(home / ".agents" / "skills" / "old-skill"),
                        "scope": "global",
                        "agents": ["Pi"],
                        "source": "owner/retired",
                        "sourceUrl": "https://github.com/owner/retired.git",
                        "sourceType": "github",
                    }
                ]
            )
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}),
                mock.patch.object(runner, "output", return_value=listing),
            ):
                records = skill_manager.installed_skill_records("universal", runner, "owner/retired")
                self.assertIsNotNone(records)
                self.assertEqual([(record["name"], Path(record["path"]).name) for record in records], [("Old Skill!", "old-skill")])


    def test_installed_skill_listing_excludes_other_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            listing = json.dumps(
                [{
                    "name": "Old Skill!",
                    "path": str(home / ".pi" / "agent" / "skills" / "old-skill"),
                    "scope": "global",
                    "agents": ["Pi"],
                    "source": "owner/retired",
                    "sourceUrl": "https://github.com/owner/retired.git",
                    "sourceType": "github",
                }]
            )
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home)}),
                mock.patch.object(runner, "output", return_value=listing),
            ):
                self.assertEqual(skill_manager.installed_skill_records("universal", runner, "owner/retired"), [])

    def test_recommended_skill_review_applies_each_choice_and_keeps_rejections_pending(self) -> None:
        self.resolve_skill_fixture("owner/added", {"new-skill": "new-skill"})
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"], "checkSkills": ["old-skill"]}
        added = {"source": "owner/added", "agent": "universal", "skills": ["new-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [added]
            folder = root / ".agents/skills/old-skill"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("retained old skill\n")
            self.own_skill_fixture(folder, retired["source"])
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "state.json").write_text(
                json.dumps({"manifest": str(path.resolve()), "managedRecommendedSkills": [retired]}),
                encoding="utf-8",
            )
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch("builtins.input", side_effect=["e", "n", "y"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                updated, managed = skill_recommendations.review_recommended_skills(manifest, path, runner)
                app_state.save_state(path, runner, "sync", managed)
                _, pending, _ = skill_recommendations.recommended_skill_plan(updated, path)

            self.assertEqual(updated["skills"], [retired, added])
            self.assertEqual(managed, [retired, added])
            self.assertEqual([(change["kind"], change["source"]) for change in pending], [("remove", "owner/retired")])


    def test_legacy_recommendation_state_updates_current_recommendation_sources(self) -> None:
        before = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["old-skill"],
            "checkSkills": ["old-skill"],
        }
        after = {
            "source": "owner/recommended",
            "agent": "universal",
            "skills": ["new-skill"],
            "checkSkills": ["new-skill"],
        }
        custom = {
            "source": "user/custom",
            "agent": "universal",
            "skills": ["custom-skill"],
            "checkSkills": ["custom-skill"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [before, custom]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example["skills"] = [after]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): [before, custom]}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
            ):
                managed, changes, conflicts = skill_recommendations.recommended_skill_plan(manifest, path)

        self.assertEqual(managed, [before])
        self.assertEqual([(change["kind"], change["source"]) for change in changes], [("update", "owner/recommended")])
        self.assertEqual(conflicts, [])



    def test_recommended_skill_history_is_preserved_for_each_manifest(self) -> None:
        first = {"source": "owner/first", "agent": "universal", "skills": ["first"]}
        second = {"source": "owner/second", "agent": "universal", "skills": ["second"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            example_path = root / "stack.example.json"
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            second_manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            first_manifest["skills"] = [first]
            second_manifest["skills"] = [second]
            first_path.write_text(json.dumps(first_manifest), encoding="utf-8")
            second_path.write_text(json.dumps(second_manifest), encoding="utf-8")
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
            ):
                app_state.save_state(first_path, runner, "sync", [first])
                app_state.save_state(second_path, runner, "sync", [second])
                _, first_changes, _ = skill_recommendations.recommended_skill_plan(first_manifest, first_path)
                _, second_changes, _ = skill_recommendations.recommended_skill_plan(second_manifest, second_path)

            self.assertEqual([(change["kind"], change["source"]) for change in first_changes], [("remove", "owner/first")])
            self.assertEqual([(change["kind"], change["source"]) for change in second_changes], [("remove", "owner/second")])

    def test_recommended_skill_sync_writes_manifest_before_uninstalling(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            folder = root / ".agents" / "skills" / "old-skill"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("original", encoding="utf-8")
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            (state_root / "recommended-skills.json").write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": [retired]}}),
                encoding="utf-8",
            )
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(manifests, "write_manifest", side_effect=OSError("locked")),
                mock.patch.object(runner, "output", side_effect=AssertionError("Retirement began before manifest write")),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(OSError):
                    skill_recommendations.review_recommended_skills(manifest, path, runner)

            self.assertEqual((folder / "SKILL.md").read_text(encoding="utf-8"), "original")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)
            history = json.loads((state_root / "recommended-skills.json").read_text(encoding="utf-8"))
            self.assertEqual(history[os.path.normcase(str(path.resolve()))], {"version": 1, "skills": [retired]})


    def test_recommended_skill_sync_restores_manifest_when_removal_verification_fails(self) -> None:
        retired = {"source": "owner/retired", "agent": "universal", "skills": ["old-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stack.json"
            example_path = root / "stack.example.json"
            state_root = root / "state"
            folder = root / ".agents" / "skills" / "old-skill"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("original", encoding="utf-8")
            record = {
                "name": "Old Skill!", "path": str(folder), "scope": "global",
                "agents": ["Universal"], "source": "owner/retired",
                "sourceUrl": "https://github.com/owner/retired.git", "sourceType": "github",
            }
            listings = iter([
                json.dumps([record]),
                json.dumps([{**record, "source": None, "sourceUrl": None, "sourceType": None}]),
            ])

            def list_skills(*args, **kwargs):
                listing = next(listings)
                if not folder.exists():
                    folder.mkdir()
                    (folder / "SKILL.md").write_text("leftover", encoding="utf-8")
                return listing
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [retired]
            example = self.minimal_manifest("~/.omp/agent/mcp.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            example_path.write_text(json.dumps(example), encoding="utf-8")
            state_root.mkdir()
            history_path = state_root / "recommended-skills.json"
            history_path.write_text(
                json.dumps({os.path.normcase(str(path.resolve())): {"version": 1, "skills": [retired]}}),
                encoding="utf-8",
            )
            runner = runtime.Runner("ubuntu")
            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}, clear=False),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(runner, "output", side_effect=list_skills),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                updated, managed = skill_recommendations.review_recommended_skills(manifest, path, runner)

            self.assertTrue(runner.failures)
            self.assertTrue((folder / "SKILL.md").is_file())
            self.assertEqual(updated, manifest)
            self.assertEqual(managed, [retired])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)
            self.assertEqual(
                json.loads(history_path.read_text(encoding="utf-8"))[os.path.normcase(str(path.resolve()))],
                {"version": 1, "skills": [retired]},
            )

    def test_accepted_recommendations_persist_when_unrelated_sync_fails(self) -> None:
        self.resolve_skill_fixture("owner/added", {"new-skill": "new-skill"})
        added = {"source": "owner/added", "agent": "universal", "skills": ["new-skill"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "stack.json"
            example_path = root / "recommendations.json"
            state_root = root / "state"
            manifest = self.minimal_manifest(str(root / "mcp.json"))
            manifest["mcp"]["servers"] = {}
            extension = root / "extension.ts"
            manifest["ompExtensions"] = [str(extension)]
            manifest["integrationRequires"] = {"ompExtensions": []}
            path.write_text(json.dumps(manifest))
            example_path.write_text(json.dumps({**manifest, "skills": [added]}))
            real_run = subprocess.run

            def extension_failure(command, *args, **kwargs):
                if isinstance(command, list) and command[:3] == ["omp", "config", "get"]:
                    command = [sys.executable, "-c", "import json; print(json.dumps({'key':'extensions','value':[]}))"]
                elif isinstance(command, list) and command[:3] == ["omp", "config", "set"]:
                    command = [sys.executable, "-c", "import sys; print('extension fixture refused',file=sys.stderr); sys.exit(7)"]
                return real_run(command, *args, **kwargs)

            with (
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(root), "DOTAI_STATE_DIR": str(state_root)}),
                mock.patch.object(manifests, "SKILL_RECOMMENDATIONS", example_path),
                mock.patch.object(releases, "latest_release_version", return_value=None),
                mock.patch.object(subprocess, "run", side_effect=extension_failure),
                mock.patch("builtins.input", return_value="a"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["--manifest", str(path), "sync", "--recommended-skills"]), 1)
            history = json.loads((state_root / "recommended-skills.json").read_text())
            self.assertEqual(history[os.path.normcase(str(path.resolve()))], {"version": 1, "skills": [added]})
            self.assertEqual(json.loads(path.read_text())["skills"], [added])
            self.assertFalse(extension.exists())


    def test_fix_shows_diff_and_applies_after_confirmation_with_backup(self) -> None:
        self.resolve_skill_fixture("fixture/legacy", {"grill-me": "# grill\n", "grill-with-docs": "# docs\n"})
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            path = home / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [
                {
                    "source": "fixture/legacy",
                    "agent": "pi",
                    "skills": ["grill-me", "grill-with-docs"],
                    "checkSkills": ["grill-me", "grill-with-docs"],
                },
                {
                    "source": "custom/skills",
                    "agent": "claude",
                    "skills": ["custom"],
                    "checkSkills": ["custom"],
                },
            ]
            original = json.dumps(manifest)
            path.write_text(original, encoding="utf-8")
            personal = home / ".claude/skills/custom/SKILL.md"
            personal.parent.mkdir(parents=True)
            personal.write_text("private custom copy\n")
            output = io.StringIO()
            with (
                mock.patch("builtins.input", return_value="y"),
                mock.patch.dict(os.environ, {"DOTAI_HOME": str(home), "XDG_STATE_HOME": str(home / "xdg")}),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli.main(["--manifest", str(path), "fix"]), 0)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["skills"][0]["agent"], "universal")
            self.assertEqual(updated["skills"][1], manifest["skills"][1])
            self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)
            self.assertEqual(json.loads(next(path.parent.glob("stack.json.bak.*")).read_text()), manifest)
            self.assertEqual((home / ".agents/skills/grill-me/SKILL.md").read_text(), "# grill\n")
            self.assertEqual((home / ".agents/skills/grill-with-docs/SKILL.md").read_text(), "# docs\n")
            self.assertEqual(personal.read_text(), "private custom copy\n")
            self.assertNotIn('"agent":', output.getvalue())

    def test_fix_dry_run_shows_migration_without_writing(self) -> None:
        self.resolve_skill_fixture("owner/skills", {"one": "# one\n"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
            manifest["skills"] = [{"source": "owner/skills", "agent": "pi", "skills": ["one"], "checkSkills": ["one"]}]
            original = json.dumps(manifest)
            path.write_text(original, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--manifest", str(path), "fix", "--dry-run"]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(list(path.parent.glob("stack.json.bak.*")))
            self.assertIn("owner/skills", output.getvalue())

    def test_package_check_uses_declared_minimum_for_any_package(self) -> None:
        package = {"name": "Other tool", "managed": True, "check": ["other", "--version"], "minimumVersion": "0.43"}
        runner = runtime.Runner("ubuntu")
        for version, expected in (("other 0.42.99", False), ("other 0.43.0", True), ("other 0.50.0", True)):
            with self.subTest(version=version):
                package["check"] = [sys.executable, "-c", f"print({version!r})"]
                self.assertEqual(package_manager.package_check(package, runner), expected)

    def test_package_version_check_reads_stderr_only_version_output(self) -> None:
        command = [sys.executable, "-c", "import sys; print('other 0.50.0', file=sys.stderr)"]
        package = {"name": "Other tool", "managed": True, "check": command, "minimumVersion": "0.43"}
        self.assertEqual(
            package_manager.package_version_check(package, runtime.Runner("ubuntu"), command),
            (True, "other 0.50.0"),
        )

    def test_package_version_check_finds_version_after_stdout_notice(self) -> None:
        command = [
            sys.executable, "-c",
            "import sys; print('Checking installation'); print('other 0.50.0', file=sys.stderr)",
        ]
        package = {"name": "Other tool", "managed": True, "check": command, "minimumVersion": "0.43"}
        installed, report = package_manager.package_version_check(package, runtime.Runner("ubuntu"), command)
        self.assertTrue(installed, report)
        self.assertIn("other 0.50.0", report)

    def test_update_installs_a_missing_versioned_core_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "operation"
            check = [
                sys.executable, "-c",
                "import pathlib, sys; "
                f"present = pathlib.Path({str(marker)!r}).exists(); "
                "print('tool 0.50.0' if present else ''); "
                "sys.exit(0 if present else 127)",
            ]
            manifest = self.minimal_manifest("mcp.json")
            manifest["packages"] = [{
                "name": "Versioned core tool", "managed": True, "minimumVersion": "0.43", "check": check,
                "install": {"linux": [[
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('install')",
                ]]},
                "update": {"linux": [[
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('update')",
                ]]},
            }]
            runner = runtime.Runner("ubuntu")
            with contextlib.redirect_stdout(io.StringIO()):
                package_manager.reconcile_packages(manifest, runner, "update")
            self.assertEqual(marker.read_text(), "install")
            self.assertEqual(runner.failures, [])

    def test_add_mcp_supports_remote_headers_and_stdio_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                [
                    "add",
                    "mcp",
                    "authenticated",
                    "--url",
                    "https://example.test/mcp",
                    "--header",
                    "Authorization=API_TOKEN",
                    "--header",
                    "X-Signed=signature=with=padding",
                ],
                [
                    "add",
                    "mcp",
                    "local",
                    "--command",
                    "npx",
                    "--arg=-y",
                    "--arg=@scope/server",
                    "--env",
                    "API_TOKEN=LOCAL_API_TOKEN",
                    "--env",
                    "LOG_LEVEL=warning",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(cli.main(["--manifest", str(path), *command]), 0)

            servers = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]
            self.assertEqual(
                servers["authenticated"]["headers"],
                {"Authorization": "API_TOKEN", "X-Signed": "signature=with=padding"},
            )
            self.assertEqual(
                servers["local"]["env"],
                {"API_TOKEN": "LOCAL_API_TOKEN", "LOG_LEVEL": "warning"},
            )

    def test_add_mcp_rejects_credentials_for_wrong_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            path.write_text(json.dumps(self.minimal_manifest("~/.omp/agent/mcp.json")), encoding="utf-8")
            commands = [
                ["add", "mcp", "remote", "--url", "https://example.test/mcp", "--env", "TOKEN=TOKEN"],
                ["add", "mcp", "local", "--command", "npx", "--header", "Authorization=TOKEN"],
                [
                    "add",
                    "mcp",
                    "duplicate",
                    "--url",
                    "https://example.test/mcp",
                    "--header",
                    "Authorization=ONE",
                    "--header",
                    "Authorization=TWO",
                ],
                ["add", "mcp", "invalid", "--command", "npx", "--env", "INVALID-NAME=TOKEN"],
            ]
            for command in commands:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["--manifest", str(path), *command]), 2)

    def test_missing_default_manifest_requires_explicit_init_and_preserves_local_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "stack.json"
            example = root / "stack.example.json"
            template = self.minimal_manifest("~/.omp/agent/mcp.json")
            example.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")

            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"DOTAI_CONFIG_DIR": str(root)}),
                mock.patch.object(manifests, "EXAMPLE_MANIFEST", example),
                contextlib.redirect_stdout(output),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["validate"]), 2)
                self.assertFalse(target.exists())
                self.assertEqual(cli.main(["init"]), 0)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), template)
                default_local = dict(template)
                default_local["localOnly"] = True
                target.write_text(json.dumps(default_local, indent=2) + "\n", encoding="utf-8")
                self.assertEqual(cli.main(["validate"]), 0)
                custom = root / "custom.json"
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["--manifest", str(custom), "init"]), 0)
                self.assertEqual(json.loads(custom.read_text(encoding="utf-8")), template)
                custom_local = dict(template)
                custom_local["localOnly"] = True
                custom.write_text(json.dumps(custom_local, indent=2) + "\n", encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["--manifest", str(custom), "init"]), 2)
                self.assertEqual(json.loads(custom.read_text(encoding="utf-8")), custom_local)
                missing_custom = root / "missing.json"
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["--manifest", str(missing_custom), "validate"]), 2)
                self.assertFalse(missing_custom.exists())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), default_local)
            self.assertEqual(output.getvalue().count("Initialized"), 2)

    def test_release_notice_warns_only_for_newer_numeric_versions(self) -> None:
        for current, tag, warns in (
            ("1.9.0", "v1.10.0", True),
            ("1.2.3", "v1.2.3", False),
            ("1.2.3", "1.2.3", False),
            ("1.2.3", "v1.2.2", False),
        ):
            with self.subTest(current=current, tag=tag):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = json.dumps({"tag_name": tag}).encode("utf-8")
                output = io.StringIO()
                with (
                    mock.patch.object(releases, "VERSION", current),
                    mock.patch("urllib.request.urlopen", return_value=response),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(cli.main(["--color", "never", "version"]), 0)
                if warns:
                    self.assertIn("[UPDATE]", output.getvalue())
                    self.assertIn("1.10.0", output.getvalue())
                    self.assertIn("1.9.0", output.getvalue())
                else:
                    self.assertNotIn("[UPDATE]", output.getvalue())



    def test_release_check_failure_keeps_version_command_available(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(releases, "VERSION", "1.2.3"),
            mock.patch("urllib.request.urlopen", side_effect=OSError("offline")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["--color", "never", "version"]), 0)

    def test_malformed_release_response_is_ignored(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(releases.latest_release_version())




if __name__ == "__main__":
    unittest.main()
