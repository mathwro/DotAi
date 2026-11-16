#!/usr/bin/env python3
"""Maintainer upstream drift checker for OMP routing catalog and Linux RTK release recipes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import argparse
import hashlib
import json
import os
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotai_app import manifest as manifests
from dotai_app import releases
from dotai_app import routing as model_routing
from dotai_app import runtime
from dotai_app import terminal


DEFAULT_OMP_MODELS_URL = (
    "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/catalog/src/models.json"
)
FALLBACK_OMP_MODELS_URL = (
    "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/ai/src/models.json"
)
RTK_RELEASES_API_BASE = "https://api.github.com/repos/rtk-ai/rtk/releases"
RTK_LINUX_TARGETS = ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-gnu")

_RTK_DOWNLOAD_URL_RE = re.compile(
    r"https://github\.com/rtk-ai/rtk/releases/download/(v\d+\.\d+\.\d+)/rtk-\$\{?target\}?\.tar\.gz"
)
_RTK_LATEST_CHECKSUMS_RE = re.compile(
    r"https://github\.com/rtk-ai/rtk/releases/(?:latest/download|download/\$\{?latest_tag\}?)/checksums\.txt"
)
_RTK_LATEST_ARCHIVE_RE = re.compile(
    r"https://github\.com/rtk-ai/rtk/releases/(?:latest/download|download/\$\{?latest_tag\}?)/(?:\$\{?asset\}?|rtk-\$\{?target\}?\.tar\.gz)"
)
_RTK_TARGET_DIGEST_RE = re.compile(
    r"target=([A-Za-z0-9_.-]+);\s*digest=([0-9a-fA-F]{64})"
)
_RTK_SORT_GUARD_RE = re.compile(
    r"printf\s+'%s\\n'\s+'(\d+\.\d+\.\d+)'\s+\"\$current\"\s*\|\s*sort\s+-V"
)
_RTK_EQUAL_GUARD_RE = re.compile(
    r"sed\s+-n\s+'1p'\)\"\s*=\s*'(\d+\.\d+\.\d+)'"
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _github_token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def github_request_headers(url: str, *, accept_json: bool = False) -> dict[str, str]:
    headers = {"User-Agent": f"DotAi-UpstreamCheck/{releases.VERSION}"}
    if accept_json:
        headers["Accept"] = "application/vnd.github+json"
    host = (urlparse.urlsplit(url).hostname or "").lower()
    token = _github_token()
    if token and host in {"api.github.com", "github.com", "raw.githubusercontent.com"}:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url: str, *, timeout: int = 20, accept_json: bool = False) -> bytes:
    request = urlrequest.Request(
        url,
        headers=github_request_headers(url, accept_json=accept_json),
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urlerror.HTTPError as exc:
        try:
            detail = f"HTTP {exc.code} {exc.reason}"
        finally:
            exc.close()
        raise runtime.DotAiError(f"Upstream request failed for {url}: {detail}") from exc
    except OSError as exc:
        raise runtime.DotAiError(f"Upstream request failed for {url}: {exc}") from exc


def fetch_json(url: str, *, timeout: int = 20, accept_github_json: bool = False) -> Any:
    raw = fetch_bytes(url, timeout=timeout, accept_json=accept_github_json)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise runtime.DotAiError(f"Invalid JSON from {url}: {exc}") from exc


def load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise runtime.DotAiError(f"File not found: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise runtime.DotAiError(f"Unable to load JSON from {path}: {exc}") from exc


def parse_omp_catalog_models(payload: Any) -> dict[str, set[str]]:
    """Extract {provider: {'provider/model_id', ...}} from supported OMP catalog JSON shapes."""
    catalog: dict[str, set[str]] = {}

    if isinstance(payload, dict) and isinstance(payload.get("providers"), list):
        # Compact CatalogData format in packages/catalog/src/models.json:
        # providers: [[provider_name, base_meta, [[model_id, encoded_val, ...], ...]], ...]
        for row in payload["providers"]:
            if (
                isinstance(row, list)
                and len(row) >= 3
                and isinstance(row[0], str)
                and row[0]
                and isinstance(row[2], list)
            ):
                provider = row[0]
                bucket = catalog.setdefault(provider, set())
                for model_row in row[2]:
                    if isinstance(model_row, list) and model_row and isinstance(model_row[0], str) and model_row[0]:
                        bucket.add(f"{provider}/{model_row[0]}")
        if catalog:
            return catalog

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            selector = item.get("selector")
            provider = item.get("provider")
            model_id = item.get("id")
            if isinstance(selector, str) and "/" in selector:
                prov = selector.split("/", 1)[0]
                catalog.setdefault(prov, set()).add(model_routing.selector_identity(selector))
            elif isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
                ident = f"{provider}/{model_id}" if not model_id.startswith(f"{provider}/") else model_id
                catalog.setdefault(provider, set()).add(model_routing.selector_identity(ident))
        if catalog:
            return catalog

    if isinstance(payload, dict):
        models_list = payload.get("models")
        if isinstance(models_list, list):
            return parse_omp_catalog_models(models_list)

        source_dict = payload.get("providers") if isinstance(payload.get("providers"), dict) else payload
        if isinstance(source_dict, dict):
            for provider, entries in source_dict.items():
                if not isinstance(provider, str) or not provider:
                    continue
                if isinstance(entries, dict):
                    bucket = catalog.setdefault(provider, set())
                    for key, record in entries.items():
                        if isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"]:
                            mid = record["id"]
                        elif isinstance(key, str) and key:
                            mid = key
                        else:
                            continue
                        ident = mid if mid.startswith(f"{provider}/") else f"{provider}/{mid}"
                        bucket.add(model_routing.selector_identity(ident))
                elif isinstance(entries, list):
                    bucket = catalog.setdefault(provider, set())
                    for item in entries:
                        if isinstance(item, str) and item:
                            ident = item if item.startswith(f"{provider}/") else f"{provider}/{item}"
                            bucket.add(model_routing.selector_identity(ident))
                        elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                            mid = item["id"]
                            ident = mid if mid.startswith(f"{provider}/") else f"{provider}/{mid}"
                            bucket.add(model_routing.selector_identity(ident))
        if catalog:
            return catalog

    raise runtime.DotAiError("Unrecognized OMP model catalog JSON schema")


def load_omp_catalog(
    *,
    models_file: Path | None,
    models_url: str | None,
    timeout: int,
) -> tuple[dict[str, set[str]], str]:
    if models_file is not None:
        return parse_omp_catalog_models(load_json_file(models_file)), str(models_file)
    if models_url:
        return parse_omp_catalog_models(fetch_json(models_url, timeout=timeout)), models_url

    last_error: runtime.DotAiError | None = None
    for candidate_url in (DEFAULT_OMP_MODELS_URL, FALLBACK_OMP_MODELS_URL):
        try:
            return parse_omp_catalog_models(fetch_json(candidate_url, timeout=timeout)), candidate_url
        except runtime.DotAiError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def check_routing_catalog(
    routing_path: Path,
    *,
    models_file: Path | None = None,
    models_url: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    recommendations = model_routing.load_routing_recommendations(routing_path)
    catalog, source = load_omp_catalog(
        models_file=models_file,
        models_url=models_url,
        timeout=timeout,
    )

    provider_reports: dict[str, Any] = {}
    errors: list[str] = []
    notices: list[str] = []

    for provider, config in recommendations["providers"].items():
        if provider not in catalog:
            provider_reports[provider] = {
                "status": "UNVERIFIED",
                "inStaticCatalog": False,
                "catalogModelCount": 0,
                "missingModels": [],
                "emptyRoles": [],
                "roleValidCounts": {},
            }
            notices.append(
                f"Provider '{provider}' is absent from static OMP catalog (dynamic-only runtime discovery)"
            )
            continue

        available = catalog[provider]
        missing_models: list[str] = []
        empty_roles: list[str] = []
        role_valid_counts: dict[str, int] = {}

        for role in model_routing.ROUTING_ROLES:
            selectors = config["roles"][role]
            valid_count = 0
            for selector in selectors:
                identity = model_routing.selector_identity(selector)
                if identity in available:
                    valid_count += 1
                elif identity not in missing_models:
                    missing_models.append(identity)
            role_valid_counts[role] = valid_count
            if valid_count == 0:
                empty_roles.append(role)

        if missing_models or empty_roles:
            prov_status = "FAIL"
            if missing_models:
                errors.append(
                    f"Provider '{provider}' references missing OMP catalog model(s): {', '.join(missing_models)}"
                )
            if empty_roles:
                errors.append(
                    f"Provider '{provider}' has zero valid catalog models for role(s): {', '.join(empty_roles)}"
                )
        else:
            prov_status = "OK"

        provider_reports[provider] = {
            "status": prov_status,
            "inStaticCatalog": True,
            "catalogModelCount": len(available),
            "missingModels": missing_models,
            "emptyRoles": empty_roles,
            "roleValidCounts": role_valid_counts,
        }

    status = "FAIL" if errors else "OK"
    return {
        "status": status,
        "source": source,
        "providers": provider_reports,
        "errors": errors,
        "notices": notices,
    }


def _extract_linux_shell_script(package: dict[str, Any], phase: str) -> str:
    section = package.get(phase)
    if not isinstance(section, dict):
        raise runtime.DotAiError(f"RTK package '{phase}' must be a platform mapping")
    commands = section.get("linux")
    if (
        not isinstance(commands, list)
        or len(commands) != 1
        or not isinstance(commands[0], str)
        or not commands[0].strip()
    ):
        raise runtime.DotAiError(f"RTK package '{phase}.linux' must contain a single shell command string")
    return commands[0]


def _parse_rtk_linux_script(script: str, phase: str) -> tuple[str | None, dict[str, str], list[str]]:
    tags = set(_RTK_DOWNLOAD_URL_RE.findall(script))
    if len(tags) > 1:
        raise runtime.DotAiError(
            f"RTK '{phase}.linux' references multiple release tags: {', '.join(sorted(tags))}"
        )
    if not tags:
        if (
            _RTK_LATEST_CHECKSUMS_RE.search(script)
            and _RTK_LATEST_ARCHIVE_RE.search(script)
            and "sha256sum -c -" in script
            and all(target in script for target in RTK_LINUX_TARGETS)
        ):
            return None, {}, []
        raise runtime.DotAiError(
            f"RTK '{phase}.linux' has neither a checksums.txt-verified releases/latest recipe nor a pinned release URL"
        )
    tag = next(iter(tags))

    digests: dict[str, str] = {}
    for target, digest in _RTK_TARGET_DIGEST_RE.findall(script):
        lower_digest = digest.lower()
        if target in digests and digests[target] != lower_digest:
            raise runtime.DotAiError(
                f"RTK '{phase}.linux' defines conflicting SHA-256 digests for {target}"
            )
        digests[target] = lower_digest

    missing_targets = [target for target in RTK_LINUX_TARGETS if target not in digests]
    if missing_targets:
        raise runtime.DotAiError(
            f"RTK '{phase}.linux' is missing SHA-256 digest(s) for: {', '.join(missing_targets)}"
        )

    guard_versions = _RTK_SORT_GUARD_RE.findall(script) + _RTK_EQUAL_GUARD_RE.findall(script)
    return tag, digests, guard_versions


def extract_pinned_rtk_spec(manifest: dict[str, Any]) -> tuple[str | None, dict[str, str], list[str]]:
    rtk_pkg = next(
        (pkg for pkg in manifest.get("packages", []) if pkg.get("name") == "RTK"),
        None,
    )
    if rtk_pkg is None:
        raise runtime.DotAiError("Manifest does not declare an 'RTK' package")

    install_script = _extract_linux_shell_script(rtk_pkg, "install")
    update_script = _extract_linux_shell_script(rtk_pkg, "update")

    install_tag, install_digests, _ = _parse_rtk_linux_script(install_script, "install")
    update_tag, update_digests, update_guards = _parse_rtk_linux_script(update_script, "update")

    errors: list[str] = []
    if install_tag is None and update_tag is None:
        return None, {}, errors
    if install_tag is None or update_tag is None:
        errors.append(
            f"RTK install.linux ({install_tag or 'latest'}) disagrees with update.linux ({update_tag or 'latest'})"
        )
        return install_tag or update_tag, install_digests or update_digests, errors

    if install_tag != update_tag:
        errors.append(
            f"RTK install.linux tag ({install_tag}) disagrees with update.linux tag ({update_tag})"
        )

    expected_bare = update_tag.removeprefix("v")
    if not update_guards:
        errors.append("RTK update.linux is missing the version comparison guard")
    else:
        mismatched_guards = sorted({ver for ver in update_guards if ver != expected_bare})
        if mismatched_guards:
            errors.append(
                f"RTK update.linux version guard ({', '.join(mismatched_guards)}) disagrees with URL tag ({update_tag})"
            )

    for target in RTK_LINUX_TARGETS:
        if install_digests.get(target) != update_digests.get(target):
            errors.append(
                f"RTK install.linux and update.linux SHA-256 digests disagree for {target}: "
                f"{install_digests.get(target)} != {update_digests.get(target)}"
            )

    return install_tag, {target: install_digests[target] for target in RTK_LINUX_TARGETS}, errors

def _parse_checksums_text(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower()
        filename = parts[1].lstrip("*./")
        if _SHA256_HEX_RE.fullmatch(digest):
            parsed[Path(filename).name] = digest
    return parsed


def extract_release_linux_digests(
    release_payload: Any,
    *,
    require_checksums_asset: bool = False,
    verify_tarballs: bool = False,
    timeout: int = 20,
) -> tuple[str, dict[str, str], list[str]]:
    if not isinstance(release_payload, dict):
        raise runtime.DotAiError("GitHub release metadata must be a JSON object")
    tag = release_payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise runtime.DotAiError("GitHub release metadata is missing 'tag_name'")
    assets = release_payload.get("assets")
    if not isinstance(assets, list):
        raise runtime.DotAiError(f"GitHub release {tag} metadata is missing 'assets' list")

    asset_by_name: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            asset_by_name[asset["name"]] = asset

    checksums_map: dict[str, str] | None = None
    digests: dict[str, str] = {}
    errors: list[str] = []

    if require_checksums_asset:
        checksum_asset = asset_by_name.get("checksums.txt")
        download_url = checksum_asset.get("browser_download_url") if checksum_asset else None
        if not isinstance(download_url, str) or not download_url:
            errors.append(f"Upstream RTK release {tag} is missing checksums.txt asset")
            checksums_map = {}
        else:
            raw_checksums = fetch_bytes(download_url, timeout=timeout).decode("utf-8", errors="replace")
            checksums_map = _parse_checksums_text(raw_checksums)

    for target in RTK_LINUX_TARGETS:
        asset_name = f"rtk-{target}.tar.gz"
        asset = asset_by_name.get(asset_name)
        if asset is None:
            errors.append(f"Upstream RTK release {tag} is missing asset {asset_name}")
            continue

        digest_field = asset.get("digest")
        api_digest: str | None = None
        if isinstance(digest_field, str) and digest_field.lower().startswith("sha256:"):
            candidate = digest_field.split(":", 1)[1].lower()
            if _SHA256_HEX_RE.fullmatch(candidate):
                api_digest = candidate

        checksum_digest: str | None = None
        if api_digest is None or require_checksums_asset:
            if checksums_map is None:
                checksum_asset = asset_by_name.get("checksums.txt")
                download_url = checksum_asset.get("browser_download_url") if checksum_asset else None
                if isinstance(download_url, str) and download_url:
                    raw_checksums = fetch_bytes(download_url, timeout=timeout).decode("utf-8", errors="replace")
                    checksums_map = _parse_checksums_text(raw_checksums)
                else:
                    checksums_map = {}
            checksum_digest = checksums_map.get(asset_name)

        if require_checksums_asset:
            if checksum_digest is None:
                errors.append(f"Upstream RTK release {tag} checksums.txt is missing entry for {asset_name}")
                continue
            if api_digest is not None and checksum_digest != api_digest:
                errors.append(
                    f"Upstream RTK release {tag} checksums.txt digest ({checksum_digest}) disagrees with asset digest ({api_digest}) for {asset_name}"
                )
                continue

        resolved_digest = api_digest or checksum_digest
        if resolved_digest is None:
            errors.append(f"Upstream RTK release {tag} has no SHA-256 digest metadata for {asset_name}")
            continue
        if verify_tarballs:
            download_url = asset.get("browser_download_url")
            if not isinstance(download_url, str) or not download_url:
                errors.append(f"Upstream RTK asset {asset_name} is missing browser_download_url")
                continue
            tarball_bytes = fetch_bytes(download_url, timeout=max(timeout, 60))
            computed = hashlib.sha256(tarball_bytes).hexdigest()
            if computed != resolved_digest:
                errors.append(
                    f"Downloaded tarball {asset_name} SHA-256 ({computed}) disagrees with release metadata ({resolved_digest})"
                )
                continue

        digests[target] = resolved_digest

    return tag, digests, errors


def _version_tuple(tag: str) -> tuple[int, int, int] | None:
    match = releases.VERSION_PATTERN.fullmatch(tag.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_rtk_release(
    manifest_path: Path,
    *,
    pinned_release_file: Path | None = None,
    latest_release_file: Path | None = None,
    verify_tarballs: bool = False,
    timeout: int = 20,
) -> dict[str, Any]:
    manifest = manifests.load_manifest(manifest_path)
    pinned_tag, pinned_digests, errors = extract_pinned_rtk_spec(manifest)

    if latest_release_file is not None:
        latest_payload = load_json_file(latest_release_file)
    else:
        latest_payload = fetch_json(
            f"{RTK_RELEASES_API_BASE}/latest",
            timeout=timeout,
            accept_github_json=True,
        )

    if pinned_tag is None:
        latest_tag, latest_digests, latest_asset_errors = extract_release_linux_digests(
            latest_payload,
            require_checksums_asset=True,
            verify_tarballs=verify_tarballs,
            timeout=timeout,
        )
        errors.extend(latest_asset_errors)
        status = "FAIL" if errors else "OK"
        return {
            "status": status,
            "mode": "latest",
            "pinnedTag": None,
            "pinnedDigests": {},
            "upstreamPinnedDigests": {},
            "latestTag": latest_tag,
            "latestDigests": latest_digests,
            "outdated": False,
            "verifiedTarballs": verify_tarballs,
            "errors": errors,
        }

    if pinned_release_file is not None:
        pinned_payload = load_json_file(pinned_release_file)
    else:
        pinned_payload = fetch_json(
            f"{RTK_RELEASES_API_BASE}/tags/{pinned_tag}",
            timeout=timeout,
            accept_github_json=True,
        )

    upstream_pinned_tag, upstream_pinned_digests, pinned_asset_errors = extract_release_linux_digests(
        pinned_payload,
        verify_tarballs=verify_tarballs,
        timeout=timeout,
    )
    errors.extend(pinned_asset_errors)

    if upstream_pinned_tag != pinned_tag:
        errors.append(
            f"Upstream release tag ({upstream_pinned_tag}) disagrees with requested pinned tag ({pinned_tag})"
        )

    for target in RTK_LINUX_TARGETS:
        expected = pinned_digests.get(target)
        actual = upstream_pinned_digests.get(target)
        if expected and actual and expected != actual:
            errors.append(
                f"Pinned RTK {pinned_tag} SHA-256 mismatch for {target}: "
                f"manifest has {expected}, upstream has {actual}"
            )

    latest_tag, latest_digests, latest_asset_errors = extract_release_linux_digests(
        latest_payload,
        verify_tarballs=False,
        timeout=timeout,
    )

    pinned_ver = _version_tuple(pinned_tag)
    latest_ver = _version_tuple(latest_tag)
    if pinned_ver is not None and latest_ver is not None:
        outdated = latest_ver > pinned_ver
    else:
        outdated = latest_tag != pinned_tag

    if outdated and latest_asset_errors:
        errors.extend(latest_asset_errors)

    if errors:
        status = "FAIL"
    elif outdated:
        status = "DRIFT"
    else:
        status = "OK"

    return {
        "status": status,
        "mode": "pinned",
        "pinnedTag": pinned_tag,
        "pinnedDigests": pinned_digests,
        "upstreamPinnedDigests": upstream_pinned_digests,
        "latestTag": latest_tag,
        "latestDigests": latest_digests,
        "outdated": outdated,
        "verifiedTarballs": verify_tarballs,
        "errors": errors,
    }


def _overall_status(statuses: list[str]) -> str:
    if "FAIL" in statuses:
        return "FAIL"
    if "DRIFT" in statuses:
        return "DRIFT"
    return "OK"


def render_human_report(report: dict[str, Any]) -> None:
    routing_report = report.get("routing")
    if routing_report is not None:
        terminal.say(terminal.heading("OMP Routing Catalog"))
        for provider, pinfo in routing_report["providers"].items():
            pstatus = pinfo["status"]
            if not pinfo["inStaticCatalog"]:
                terminal.say(
                    f"  {terminal.badge(pstatus)} {provider}: absent from static models.json (runtime discovery only)"
                )
            elif pstatus == "OK":
                counts = ", ".join(
                    f"{role}={count}" for role, count in pinfo["roleValidCounts"].items()
                )
                terminal.say(
                    f"  {terminal.badge('OK')} {provider}: all recommended models verified in static catalog ({counts})"
                )
            else:
                if pinfo["missingModels"]:
                    terminal.say(
                        f"  {terminal.badge('FAIL')} {provider}: missing catalog models: {', '.join(pinfo['missingModels'])}"
                    )
                if pinfo["emptyRoles"]:
                    terminal.say(
                        f"  {terminal.badge('FAIL')} {provider}: roles with zero valid models: {', '.join(pinfo['emptyRoles'])}"
                    )

    rtk_report = report.get("rtk")
    if rtk_report is not None:
        if routing_report is not None:
            terminal.say("")
        terminal.say(terminal.heading("Linux RTK Release"))
        if rtk_report["errors"]:
            for err in rtk_report["errors"]:
                terminal.say(f"  {terminal.badge('FAIL')} {err}")
        elif rtk_report.get("mode") == "latest":
            tarball_note = " (tarballs verified)" if rtk_report["verifiedTarballs"] else ""
            terminal.say(
                f"  {terminal.badge('OK')} Unpinned releases/latest recipe verified against upstream {rtk_report['latestTag']} checksums.txt and Linux assets{tarball_note}"
            )
        else:
            tarball_note = " (tarballs verified)" if rtk_report["verifiedTarballs"] else ""
            terminal.say(
                f"  {terminal.badge('OK')} Pinned RTK {rtk_report['pinnedTag']} install/update tags and Linux SHA-256 digests match upstream{tarball_note}"
            )
            if rtk_report["outdated"]:
                terminal.say(
                    f"  {terminal.badge('DRIFT')} Newer upstream RTK release available: "
                    f"{rtk_report['latestTag']} (pinned: {rtk_report['pinnedTag']})"
                )
                for target in RTK_LINUX_TARGETS:
                    digest = rtk_report["latestDigests"].get(target, "<missing>")
                    terminal.say(f"    {target}: {digest}")

def write_github_step_summary(summary_path: Path, report: dict[str, Any]) -> None:
    lines = [f"## Upstream Drift Check: `{report['status']}`", ""]
    routing_report = report.get("routing")
    if routing_report is not None:
        lines.append(f"### OMP Routing Catalog (`{routing_report['status']}`)")
        for provider, pinfo in routing_report["providers"].items():
            if not pinfo["inStaticCatalog"]:
                lines.append(f"- **{provider}**: `UNVERIFIED` (dynamic runtime discovery)")
            elif pinfo["status"] == "OK":
                counts = ", ".join(
                    f"`{role}={count}`" for role, count in pinfo["roleValidCounts"].items()
                )
                lines.append(f"- **{provider}**: `OK` ({counts})")
            else:
                lines.append(
                    f"- **{provider}**: `FAIL` (missing: `{', '.join(pinfo['missingModels']) or 'none'}`, "
                    f"empty roles: `{', '.join(pinfo['emptyRoles']) or 'none'}`)"
                )
        lines.append("")

    rtk_report = report.get("rtk")
    if rtk_report is not None:
        lines.append(f"### Linux RTK Release (`{rtk_report['status']}`)")
        if rtk_report.get("mode") == "latest":
            lines.append(f"- Mode: `releases/latest` (`checksums.txt` verified) | Latest upstream tag: `{rtk_report['latestTag']}`")
        else:
            lines.append(
                f"- Pinned tag: `{rtk_report['pinnedTag']}` | Latest upstream tag: `{rtk_report['latestTag']}`"
            )
        if rtk_report["errors"]:
            for err in rtk_report["errors"]:
                lines.append(f"- **FAIL**: {err}")
        elif rtk_report["outdated"]:
            lines.append("- **New release digests for `stack.example.json`**:")
            for target in RTK_LINUX_TARGETS:
                digest = rtk_report["latestDigests"].get(target, "<missing>")
                lines.append(f"  - `{target}`: `{digest}`")
        lines.append("")

    try:
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify DotAi routing recommendations and Linux RTK release recipes against upstream."
    )
    parser.add_argument(
        "--check-routing",
        action="store_true",
        help="Check routing-recommendations.json against upstream OMP model catalog",
    )
    parser.add_argument(
        "--check-rtk",
        action="store_true",
        help="Check stack.example.json Linux RTK release recipe and SHA-256 digests against GitHub releases",
    )
    parser.add_argument(
        "--fail-on-outdated",
        action="store_true",
        help="Exit nonzero when upstream publishes a newer RTK release than pinned",
    )
    parser.add_argument(
        "--verify-tarballs",
        action="store_true",
        help="Download Linux RTK release tarballs and verify their SHA-256 bytes directly",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report to stdout",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=manifests.EXAMPLE_MANIFEST,
        help="Stack manifest to inspect for Linux RTK release recipe (default: stack.example.json)",
    )
    parser.add_argument(
        "--routing",
        type=Path,
        default=model_routing.ROUTING_RECOMMENDATIONS,
        help="Routing recommendations file to inspect (default: routing-recommendations.json)",
    )
    parser.add_argument(
        "--omp-models-url",
        help=f"Upstream OMP models.json URL (default: {DEFAULT_OMP_MODELS_URL})",
    )
    parser.add_argument(
        "--omp-models-file",
        type=Path,
        help="Offline OMP models.json fixture path",
    )
    parser.add_argument(
        "--rtk-pinned-release-file",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--rtk-latest-release-file",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Network timeout in seconds per HTTP request (default: 20)",
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto, always, or never (default: auto)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    terminal.configure_color("never" if args.json else args.color)

    run_routing = args.check_routing or not (args.check_routing or args.check_rtk)
    run_rtk = args.check_rtk or not (args.check_routing or args.check_rtk)

    report: dict[str, Any] = {}
    statuses: list[str] = []

    try:
        if run_routing:
            routing_result = check_routing_catalog(
                args.routing,
                models_file=args.omp_models_file,
                models_url=args.omp_models_url,
                timeout=args.timeout,
            )
            report["routing"] = routing_result
            statuses.append(routing_result["status"])

        if run_rtk:
            rtk_result = check_rtk_release(
                args.manifest,
                pinned_release_file=args.rtk_pinned_release_file,
                latest_release_file=args.rtk_latest_release_file,
                verify_tarballs=args.verify_tarballs,
                timeout=args.timeout,
            )
            report["rtk"] = rtk_result
            statuses.append(rtk_result["status"])
    except runtime.DotAiError as exc:
        if args.json:
            print(json.dumps({"status": "FAIL", "error": str(exc)}, indent=2))
        else:
            terminal.say(f"{terminal.badge('FAIL')} {exc}", file=sys.stderr)
        return 1

    overall = _overall_status(statuses)
    report = {"status": overall, **report}

    summary_env = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_env:
        write_github_step_summary(Path(summary_env), report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_human_report(report)

    if overall == "FAIL":
        return 1
    if overall == "DRIFT" and args.fail_on_outdated:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
