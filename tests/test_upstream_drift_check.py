from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_upstream


X86_TARGET = "x86_64-unknown-linux-musl"
ARM_TARGET = "aarch64-unknown-linux-gnu"
PINNED_X86_DIGEST = "1" * 64
PINNED_ARM_DIGEST = "2" * 64
NEWER_X86_DIGEST = "a" * 64
NEWER_ARM_DIGEST = "b" * 64


def make_rtk_install_script(tag: str, x86_digest: str, arm_digest: str) -> str:
    return (
        f'set -eu; case "$(uname -m)" in '
        f"x86_64|amd64) target={X86_TARGET}; digest={x86_digest};; "
        f"arm64|aarch64) target={ARM_TARGET}; digest={arm_digest};; "
        f'*) printf "Unsupported RTK architecture\\n" >&2; exit 1;; esac; '
        f'tmp=$(mktemp -d); trap \'rm -rf "$tmp"\' EXIT; '
        f'curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/{tag}/rtk-${{target}}.tar.gz" '
        f'-o "$tmp/rtk.tar.gz"; printf \'%s  %s\\n\' "$digest" "$tmp/rtk.tar.gz" | sha256sum -c -'
    )


def make_rtk_update_script(
    tag: str,
    x86_digest: str,
    arm_digest: str,
    *,
    guard_version: str | None = None,
) -> str:
    bare = guard_version if guard_version is not None else tag.removeprefix("v")
    body = make_rtk_install_script(tag, x86_digest, arm_digest)
    return (
        f'current=$(rtk --version 2>/dev/null || true); current=${{current#rtk }}; '
        f'if [ -n "$current" ] && [ "$(printf \'%s\\n\' \'{bare}\' "$current" | sort -V | sed -n \'1p\')" = \'{bare}\' ]; '
        f'then printf \'RTK %s is already at least {bare}\\n\' "$current"; '
        f"else {body}; fi"
    )


def make_manifest(
    *,
    install_tag: str = "v0.50.0",
    update_tag: str = "v0.50.0",
    install_x86: str = PINNED_X86_DIGEST,
    install_arm: str = PINNED_ARM_DIGEST,
    update_x86: str = PINNED_X86_DIGEST,
    update_arm: str = PINNED_ARM_DIGEST,
    update_guard_version: str | None = None,
) -> dict:
    return {
        "version": 1,
        "packages": [
            {
                "name": "RTK",
                "check": ["rtk", "--version"],
                "minimumVersion": "0.43",
                "install": {
                    "windows": [["scoop", "install", "rtk"]],
                    "macos": [["brew", "install", "rtk"]],
                    "linux": [make_rtk_install_script(install_tag, install_x86, install_arm)],
                },
                "update": {
                    "windows": [["scoop", "update", "rtk"]],
                    "macos": [["brew", "upgrade", "rtk"]],
                    "linux": [
                        make_rtk_update_script(
                            update_tag,
                            update_x86,
                            update_arm,
                            guard_version=update_guard_version,
                        )
                    ],
                },
            }
        ],
        "skills": [],
        "marketplaces": [],
        "plugins": [],
        "ompExtensions": ["~/.pi/agent/extensions/rtk.ts"],
        "ompRouting": None,
        "mcp": {"target": "~/.omp/agent/mcp.json", "servers": {}},
    }


def make_routing_recommendations() -> dict:
    return {
        "version": 1,
        "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
        "providers": {
            "github-copilot": {
                "roles": {
                    "default": ["github-copilot/alpha-main", "github-copilot/alpha-fallback"],
                    "task": ["github-copilot/alpha-task"],
                    "smol": ["github-copilot/alpha-fast"],
                    "slow": ["github-copilot/alpha-deep:high"],
                }
            },
            "openai-codex": {
                "roles": {
                    "default": ["openai-codex/codex-main"],
                    "task": ["openai-codex/codex-task"],
                    "smol": ["openai-codex/codex-fast"],
                    "slow": ["openai-codex/codex-deep:high"],
                }
            },
            "anthropic": {
                "roles": {
                    "default": ["anthropic/claude-main"],
                    "task": ["anthropic/claude-task"],
                    "smol": ["anthropic/claude-fast"],
                    "slow": ["anthropic/claude-main:high"],
                }
            },
        },
    }


def make_compact_omp_catalog(provider_models: dict[str, list[str]]) -> dict:
    return {
        "version": 1,
        "keys": ["name"],
        "booleanShapes": [],
        "nodes": [],
        "compat": [],
        "providers": [
            [provider, None, [[model_id, [0]] for model_id in models]]
            for provider, models in provider_models.items()
        ],
    }


def make_release_payload(
    tag: str,
    x86_digest: str,
    arm_digest: str,
    *,
    include_digest_field: bool = True,
    checksums_url: str | None = None,
    x86_download_url: str = "https://download.invalid/rtk-x86.tar.gz",
    arm_download_url: str = "https://download.invalid/rtk-arm.tar.gz",
) -> dict:
    assets = [
        {
            "name": f"rtk-{X86_TARGET}.tar.gz",
            "digest": f"sha256:{x86_digest}" if include_digest_field else None,
            "browser_download_url": x86_download_url,
        },
        {
            "name": f"rtk-{ARM_TARGET}.tar.gz",
            "digest": f"sha256:{arm_digest}" if include_digest_field else None,
            "browser_download_url": arm_download_url,
        },
    ]
    if checksums_url:
        assets.append(
            {
                "name": "checksums.txt",
                "digest": None,
                "browser_download_url": checksums_url,
            }
        )
    return {"tag_name": tag, "assets": assets}


class UpstreamDriftCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manifest_path = self.root / "stack.example.json"
        self.routing_path = self.root / "routing-recommendations.json"
        self.omp_catalog_path = self.root / "omp-models.json"
        self.rtk_pinned_path = self.root / "rtk-pinned.json"
        self.rtk_latest_path = self.root / "rtk-latest.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_json(self, path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def full_provider_models(self) -> dict[str, list[str]]:
        return {
            "github-copilot": ["alpha-main", "alpha-fallback", "alpha-task", "alpha-fast", "alpha-deep"],
            "openai-codex": ["codex-main", "codex-task", "codex-fast", "codex-deep"],
            "anthropic": ["claude-main", "claude-task", "claude-fast"],
        }

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = check_upstream.main(["--color", "never", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_rtk_check_passes_when_manifest_and_upstream_digests_match(self) -> None:
        self.write_json(self.manifest_path, make_manifest())
        release = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, release)
        self.write_json(self.rtk_latest_path, release)

        code, out, err = self.run_cli(
            "--check-rtk",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )

        self.assertEqual(code, 0, msg=out + err)
        self.assertEqual(err, "")
        self.assertIn("[OK]", out)
        self.assertIn("v0.50.0", out)

    def test_rtk_check_fails_when_install_and_update_digests_disagree(self) -> None:
        self.write_json(
            self.manifest_path,
            make_manifest(update_x86="9" * 64),
        )
        release = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, release)
        self.write_json(self.rtk_latest_path, release)

        code, out, err = self.run_cli(
            "--check-rtk",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )

        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn(X86_TARGET, out)

    def test_rtk_check_fails_when_update_version_guard_disagrees_with_url_tag(self) -> None:
        self.write_json(
            self.manifest_path,
            make_manifest(update_guard_version="0.49.0"),
        )
        release = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, release)
        self.write_json(self.rtk_latest_path, release)

        code, out, _ = self.run_cli(
            "--check-rtk",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )

        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("0.49.0", out)

    def test_rtk_check_fails_when_upstream_asset_digest_differs_from_pinned(self) -> None:
        self.write_json(self.manifest_path, make_manifest())
        tampered = make_release_payload("v0.50.0", PINNED_X86_DIGEST, "f" * 64)
        self.write_json(self.rtk_pinned_path, tampered)
        self.write_json(self.rtk_latest_path, tampered)

        code, out, _ = self.run_cli(
            "--check-rtk",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )

        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn(ARM_TARGET, out)

    def test_rtk_check_reports_drift_for_newer_upstream_release_and_honors_fail_on_outdated(self) -> None:
        self.write_json(self.manifest_path, make_manifest())
        pinned = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        latest = make_release_payload("v1.2.0", NEWER_X86_DIGEST, NEWER_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, pinned)
        self.write_json(self.rtk_latest_path, latest)

        code, out, _ = self.run_cli(
            "--check-rtk",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )
        self.assertEqual(code, 0, msg=out)
        self.assertIn("[DRIFT]", out)
        self.assertIn("v1.2.0", out)
        self.assertIn(NEWER_X86_DIGEST, out)
        self.assertIn(NEWER_ARM_DIGEST, out)

        strict_code, strict_out, _ = self.run_cli(
            "--check-rtk",
            "--fail-on-outdated",
            "--manifest",
            str(self.manifest_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )
        self.assertEqual(strict_code, 1)
        self.assertIn("[DRIFT]", strict_out)

    def test_rtk_check_supports_checksums_txt_fallback_and_tarball_verification(self) -> None:
        x86_bytes = b"synthetic-x86-tarball"
        arm_bytes = b"synthetic-arm-tarball"
        x86_sha = hashlib.sha256(x86_bytes).hexdigest()
        arm_sha = hashlib.sha256(arm_bytes).hexdigest()

        self.write_json(
            self.manifest_path,
            make_manifest(
                install_x86=x86_sha,
                install_arm=arm_sha,
                update_x86=x86_sha,
                update_arm=arm_sha,
            ),
        )
        checksums_url = "https://download.invalid/checksums.txt"
        x86_url = "https://download.invalid/rtk-x86.tar.gz"
        arm_url = "https://download.invalid/rtk-arm.tar.gz"
        release = make_release_payload(
            "v0.50.0",
            x86_sha,
            arm_sha,
            include_digest_field=False,
            checksums_url=checksums_url,
            x86_download_url=x86_url,
            arm_download_url=arm_url,
        )
        self.write_json(self.rtk_pinned_path, release)
        self.write_json(self.rtk_latest_path, release)

        checksums_body = (
            f"{x86_sha}  rtk-{X86_TARGET}.tar.gz\n"
            f"{arm_sha}  rtk-{ARM_TARGET}.tar.gz\n"
        ).encode("utf-8")
        responses = {
            checksums_url: checksums_body,
            x86_url: x86_bytes,
            arm_url: arm_bytes,
        }

        with mock.patch.object(check_upstream, "fetch_bytes", side_effect=lambda url, **_: responses[url]):
            code, out, _ = self.run_cli(
                "--check-rtk",
                "--verify-tarballs",
                "--manifest",
                str(self.manifest_path),
                "--rtk-pinned-release-file",
                str(self.rtk_pinned_path),
                "--rtk-latest-release-file",
                str(self.rtk_latest_path),
            )

        self.assertEqual(code, 0, msg=out)
        self.assertIn("[OK]", out)

    def test_routing_check_passes_with_compact_and_provider_object_and_list_catalogs(self) -> None:
        self.write_json(self.routing_path, make_routing_recommendations())

        compact = make_compact_omp_catalog(self.full_provider_models())
        provider_dict = {
            provider: {model_id: {"name": model_id} for model_id in models}
            for provider, models in self.full_provider_models().items()
        }
        record_list = [
            {"provider": provider, "id": model_id}
            for provider, models in self.full_provider_models().items()
            for model_id in models
        ]

        for shape_name, payload in (
            ("compact", compact),
            ("provider_dict", provider_dict),
            ("record_list", record_list),
        ):
            with self.subTest(shape=shape_name):
                self.write_json(self.omp_catalog_path, payload)
                code, out, err = self.run_cli(
                    "--check-routing",
                    "--routing",
                    str(self.routing_path),
                    "--omp-models-file",
                    str(self.omp_catalog_path),
                )
                self.assertEqual(code, 0, msg=out + err)
                self.assertIn("[OK]", out)

    def test_routing_check_fails_when_static_provider_references_missing_model_id(self) -> None:
        self.write_json(self.routing_path, make_routing_recommendations())
        models = self.full_provider_models()
        models["github-copilot"].remove("alpha-fallback")
        self.write_json(self.omp_catalog_path, make_compact_omp_catalog(models))

        code, out, _ = self.run_cli(
            "--check-routing",
            "--routing",
            str(self.routing_path),
            "--omp-models-file",
            str(self.omp_catalog_path),
        )

        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("github-copilot/alpha-fallback", out)

    def test_routing_check_fails_when_role_has_zero_valid_models_in_catalog(self) -> None:
        self.write_json(self.routing_path, make_routing_recommendations())
        models = self.full_provider_models()
        models["anthropic"] = ["claude-main", "claude-task"]  # smol model missing -> 0 valid models for smol
        self.write_json(self.omp_catalog_path, make_compact_omp_catalog(models))

        code, out, _ = self.run_cli(
            "--check-routing",
            "--routing",
            str(self.routing_path),
            "--omp-models-file",
            str(self.omp_catalog_path),
            "--json",
        )

        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["routing"]["status"], "FAIL")
        anthropic_report = report["routing"]["providers"]["anthropic"]
        self.assertIn("anthropic/claude-fast", anthropic_report["missingModels"])
        self.assertIn("smol", anthropic_report["emptyRoles"])

    def test_routing_check_reports_dynamic_provider_unverified_without_crashing(self) -> None:
        self.write_json(self.routing_path, make_routing_recommendations())
        models = self.full_provider_models()
        del models["openai-codex"]
        self.write_json(self.omp_catalog_path, make_compact_omp_catalog(models))

        code, out, _ = self.run_cli(
            "--check-routing",
            "--routing",
            str(self.routing_path),
            "--omp-models-file",
            str(self.omp_catalog_path),
            "--json",
        )

        self.assertEqual(code, 0, msg=out)
        report = json.loads(out)
        self.assertEqual(report["routing"]["providers"]["openai-codex"]["inStaticCatalog"], False)
        self.assertEqual(report["routing"]["providers"]["openai-codex"]["status"], "UNVERIFIED")

    def test_combined_json_output_and_github_headers_honor_tokens(self) -> None:
        self.write_json(self.manifest_path, make_manifest())
        self.write_json(self.routing_path, make_routing_recommendations())
        self.write_json(self.omp_catalog_path, make_compact_omp_catalog(self.full_provider_models()))
        pinned = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        latest = make_release_payload("v1.2.0", NEWER_X86_DIGEST, NEWER_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, pinned)
        self.write_json(self.rtk_latest_path, latest)

        code, out, err = self.run_cli(
            "--json",
            "--manifest",
            str(self.manifest_path),
            "--routing",
            str(self.routing_path),
            "--omp-models-file",
            str(self.omp_catalog_path),
            "--rtk-pinned-release-file",
            str(self.rtk_pinned_path),
            "--rtk-latest-release-file",
            str(self.rtk_latest_path),
        )

        self.assertEqual(code, 0, msg=out + err)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["status"], "DRIFT")
        self.assertEqual(payload["routing"]["status"], "OK")
        self.assertEqual(payload["rtk"]["status"], "DRIFT")
        self.assertEqual(payload["rtk"]["pinnedTag"], "v0.50.0")
        self.assertEqual(payload["rtk"]["latestTag"], "v1.2.0")
        self.assertEqual(
            payload["rtk"]["latestDigests"],
            {X86_TARGET: NEWER_X86_DIGEST, ARM_TARGET: NEWER_ARM_DIGEST},
        )

        synthetic_token = "tok_" + "x" * 24
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": synthetic_token}, clear=True):
            headers = check_upstream.github_request_headers("https://api.github.com/repos/rtk-ai/rtk/releases/latest")
            self.assertEqual(headers.get("Authorization"), f"Bearer {synthetic_token}")
            other_headers = check_upstream.github_request_headers("https://example.invalid/models.json")
            self.assertNotIn("Authorization", other_headers)

    def test_step_summary_written_and_invalid_inputs_report_clean_failure(self) -> None:
        summary_file = self.root / "step-summary.md"
        self.write_json(self.manifest_path, make_manifest())
        self.write_json(self.routing_path, make_routing_recommendations())
        self.write_json(self.omp_catalog_path, make_compact_omp_catalog(self.full_provider_models()))
        pinned = make_release_payload("v0.50.0", PINNED_X86_DIGEST, PINNED_ARM_DIGEST)
        latest = make_release_payload("v1.2.0", NEWER_X86_DIGEST, NEWER_ARM_DIGEST)
        self.write_json(self.rtk_pinned_path, pinned)
        self.write_json(self.rtk_latest_path, latest)

        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}):
            code, _, _ = self.run_cli(
                "--manifest",
                str(self.manifest_path),
                "--routing",
                str(self.routing_path),
                "--omp-models-file",
                str(self.omp_catalog_path),
                "--rtk-pinned-release-file",
                str(self.rtk_pinned_path),
                "--rtk-latest-release-file",
                str(self.rtk_latest_path),
            )
        self.assertEqual(code, 0)
        summary_text = summary_file.read_text(encoding="utf-8")
        self.assertIn("Upstream Drift Check: `DRIFT`", summary_text)
        self.assertIn(NEWER_X86_DIGEST, summary_text)

        bad_code, bad_out, bad_err = self.run_cli(
            "--check-routing",
            "--routing",
            str(self.root / "missing-routing.json"),
            "--json",
        )
        self.assertEqual(bad_code, 1)
        self.assertEqual(bad_err, "")
        self.assertEqual(json.loads(bad_out)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
