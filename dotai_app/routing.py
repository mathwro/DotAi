"""Provider/model recommendations and explicit OMP routing configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sys
from . import manifest as manifests
from . import runtime
from . import terminal


ROUTING_RECOMMENDATIONS = runtime.ROOT / "routing-recommendations.json"


ROUTING_ROLES = ("default", "task", "smol", "slow")


DEFAULT_AGENT_MODEL_OVERRIDES = {"sonic": "@smol", "task": "@task"}


THINKING_SUFFIXES = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "auto"})


def validate_routing_recommendations(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "agentModelOverrides", "providers"}:
        raise runtime.DotAiError("Routing recommendations must have exactly version, agentModelOverrides, and providers")
    if isinstance(value["version"], bool) or value["version"] != 1:
        raise runtime.DotAiError("Routing recommendations 'version' must be 1")

    overrides = value["agentModelOverrides"]
    if overrides != DEFAULT_AGENT_MODEL_OVERRIDES:
        raise runtime.DotAiError("Routing recommendation agentModelOverrides must match the managed defaults")

    providers = value["providers"]
    if not isinstance(providers, dict) or set(providers) != manifests.SUPPORTED_ROUTING_PROVIDERS:
        raise runtime.DotAiError("Routing recommendations must define exactly the supported providers")
    for provider, settings in providers.items():
        if not isinstance(provider, str) or not provider:
            raise runtime.DotAiError("Routing recommendation provider names must be non-empty strings")
        if not isinstance(settings, dict) or set(settings) != {"roles"}:
            raise runtime.DotAiError("Routing recommendation providers must contain exactly roles")
        roles = settings["roles"]
        if not isinstance(roles, dict) or set(roles) != set(ROUTING_ROLES):
            raise runtime.DotAiError("Routing recommendation providers must define exactly the managed roles")
        for selectors in roles.values():
            if not isinstance(selectors, list) or not selectors:
                raise runtime.DotAiError("Routing recommendation roles must be non-empty arrays")
            if any(
                not isinstance(selector, str)
                or not selector
                or not selector.startswith(f"{provider}/")
                for selector in selectors
            ):
                raise runtime.DotAiError("Routing recommendation selectors must be non-empty and match their provider")
    return value


def load_routing_recommendations(
    path: Path = ROUTING_RECOMMENDATIONS,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise runtime.DotAiError(f"Unable to read routing recommendations from {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runtime.DotAiError(f"Invalid JSON in {path}: {exc}") from exc
    except UnicodeError as exc:
        raise runtime.DotAiError(f"Routing recommendations are not UTF-8: {path}: {exc}") from exc
    return validate_routing_recommendations(value)


def selector_identity(selector: str) -> str:
    base, separator, suffix = selector.rpartition(":")
    return base if separator and suffix in THINKING_SUFFIXES else selector


def available_omp_models(runner: runtime.Runner) -> set[str] | None:
    raw = runner.output(["omp", "models", "--json"])
    try:
        catalog = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        return None
    models = catalog["models"]
    if any(not isinstance(model, dict) or not isinstance(model.get("selector"), str) or not model["selector"] for model in models):
        return None
    return {model["selector"] for model in models}


def detected_routing_providers(
    recommendations: dict[str, Any], available: set[str]
) -> list[str]:
    available_identities = {selector_identity(selector) for selector in available}
    supported = recommendations["providers"]
    providers = sorted(
        {selector.partition("/")[0] for selector in available} & set(supported)
    )
    for provider in providers:
        recommended = supported[provider]["roles"].values()
        if not any(
            selector_identity(selector) in available_identities
            for selectors in recommended
            for selector in selectors
        ):
            raise runtime.DotAiError(f"Provider {provider} has no recommended models available")
    return providers


def choose_primary_provider(
    providers: list[str], current: str | None, requested: str | None
) -> str:
    premium = [
        provider for provider in ("anthropic", "openai-codex") if provider in providers
    ]
    if requested is not None and requested not in premium:
        raise runtime.DotAiError(f"Requested --primary {requested} is not available")
    if not premium:
        if "github-copilot" in providers:
            return "github-copilot"
        raise runtime.DotAiError("No supported routing providers are available")
    if len(premium) == 1:
        return premium[0]
    if requested is not None:
        return requested
    if current in premium:
        return current
    if not sys.stdin.isatty():
        raise runtime.DotAiError(
            "Both Anthropic and OpenAI Codex are available; pass --primary"
        )
    try:
        response = input(
            "Choose interactive primary: [1] Anthropic [2] OpenAI Codex: "
        )
    except (EOFError, KeyboardInterrupt) as exc:
        raise runtime.DotAiError("Primary selection cancelled") from exc
    if response == "1":
        return "anthropic"
    if response == "2":
        return "openai-codex"
    raise runtime.DotAiError("Invalid primary selection; enter 1 or 2")


def resolve_omp_routing(
    recommendations: dict[str, Any],
    providers: list[str],
    primary: str,
    available: set[str],
) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
    interactive_order = [
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
    worker_order = [
        provider
        for provider in ("github-copilot", "anthropic", "openai-codex")
        if provider in providers
    ]
    available_identities = {selector_identity(selector) for selector in available}
    primaries: dict[str, str] = {}
    fallbacks: dict[str, list[str]] = {}
    unavailable: list[str] = []
    for role in ROUTING_ROLES:
        provider_order = (
            interactive_order if role in ("default", "slow") else worker_order
        )
        candidates = (
            selector
            for provider in provider_order
            for selector in recommendations["providers"][provider]["roles"][role]
        )
        retained: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if (
                candidate not in seen
                and selector_identity(candidate) in available_identities
            ):
                seen.add(candidate)
                retained.append(candidate)
        if retained:
            primaries[role] = retained[0]
            fallbacks[role] = retained
        else:
            unavailable.append(role)
    return primaries, fallbacks, unavailable


def configured_omp_value(runner: runtime.Runner, key: str) -> Any | None:
    raw = runner.output(["omp", "config", "get", key, "--json"])
    try:
        configured = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(configured, dict) or "value" not in configured:
        return None
    return configured["value"]


def build_omp_routing_intent(
    routing: dict[str, Any],
    recommendations: dict[str, Any],
    providers: list[str],
    primary: str,
) -> dict[str, Any]:
    return {
        "providers": sorted(providers),
        "primaryProvider": primary,
        "agentModelOverrides": routing.get(
            "agentModelOverrides", recommendations["agentModelOverrides"]
        ),
        "usageReservePct": routing.get("usageReservePct", 10),
        "usageReservePolicy": routing.get("usageReservePolicy", "auto"),
        "fallbackRevertPolicy": routing.get(
            "fallbackRevertPolicy", "cooldown-expiry"
        ),
    }


def omp_routing_scalar_values(routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "retry.modelFallback": True,
        "retry.usageAwareFallback": True,
        "retry.usageReservePct": routing["usageReservePct"],
        "retry.usageReservePolicy": routing["usageReservePolicy"],
        "retry.fallbackRevertPolicy": routing["fallbackRevertPolicy"],
    }


def configure_omp_routing(
    manifest: dict[str, Any],
    path: Path,
    runner: runtime.Runner,
    requested_primary: str | None = None,
) -> int:
    routing = manifest.get("ompRouting") or {}
    recommendations = load_routing_recommendations()
    available = available_omp_models(runner)
    if available is None:
        print(f"{terminal.badge('FAIL')} OMP routing: unable to read OMP model catalog")
        return 1

    providers = detected_routing_providers(recommendations, available)
    persisted_primary = routing.get("primaryProvider")
    primary = choose_primary_provider(providers, persisted_primary, requested_primary)
    primaries, fallbacks, unavailable = resolve_omp_routing(
        recommendations, providers, primary, available
    )
    if unavailable:
        print(f"{terminal.badge('FAIL')} OMP routing: unavailable managed roles: {', '.join(unavailable)}")
        return 1

    intent = build_omp_routing_intent(
        routing, recommendations, providers, primary
    )
    record_keys = (
        "modelRoles",
        "retry.fallbackChains",
        "task.agentModelOverrides",
    )
    scalar_values = omp_routing_scalar_values(intent)
    current = {
        key: configured_omp_value(runner, key)
        for key in (*record_keys, *scalar_values)
    }
    if any(
        not isinstance(current[key], dict)
        or any(not isinstance(name, str) for name in current[key])
        for key in record_keys
    ) or any(current[key] is None for key in scalar_values):
        print(f"{terminal.badge('FAIL')} OMP routing: unable to read required OMP configuration")
        return 1

    updated = dict(manifest)
    updated["ompRouting"] = intent
    desired_records = {
        "modelRoles": {**current["modelRoles"], **primaries},
        "retry.fallbackChains": {
            **current["retry.fallbackChains"],
            **fallbacks,
        },
        "task.agentModelOverrides": {
            **current["task.agentModelOverrides"],
            **intent["agentModelOverrides"],
        },
    }
    writes: list[tuple[str, str]] = []
    for key, value in desired_records.items():
        if current[key] != value:
            writes.append((key, json.dumps(value, separators=(",", ":"))))
    for key, value in scalar_values.items():
        if type(current[key]) is not type(value) or current[key] != value:
            serialized = str(value).lower() if isinstance(value, bool) else str(value)
            writes.append((key, serialized))

    print(f"{terminal.heading('OMP routing:')}")
    print(f"  discovered providers: {', '.join(providers)}")
    print(f"  interactive primary: {primary}")
    print(
        "  resolved role primaries: "
        + json.dumps(primaries, separators=(",", ":"))
    )
    print(
        "  fallback chains: "
        + json.dumps(fallbacks, separators=(",", ":"))
    )
    print("  manifest changes:")
    diff = manifests.manifest_diff(manifest, updated, path)
    print(diff or "    none")
    print("  pending OMP commands:")
    if writes:
        for key, value in writes:
            print(f"    omp config set {key} {value}")
    else:
        print("    none")

    if runner.dry_run:
        print(f"{terminal.badge('RUN')} Dry run: no manifest or OMP changes applied.")
        return 0

    manifest_changed = manifest != updated
    if manifest_changed:
        backup = manifests.write_manifest(path, updated, backup=True)
        print(f"{terminal.badge('OK')} Manifest backup written to {backup}")

    failures_before = len(runner.failures)
    for key, value in writes:
        runner.run(
            ["omp", "config", "set", key, value],
            f"Configure OMP routing {key}",
        )
    if not manifest_changed and not writes:
        print(f"{terminal.badge('OK')} OMP routing: already configured")
    return 1 if len(runner.failures) > failures_before else 0


def omp_routing_status(manifest: dict[str, Any], runner: runtime.Runner) -> tuple[str, str]:
    routing = manifest.get("ompRouting")
    if not routing:
        return "OK", "not configured in manifest"

    try:
        recommendations = load_routing_recommendations()
    except runtime.DotAiError:
        return "FAIL", "unable to read routing recommendations"
    available = available_omp_models(runner)
    if available is None:
        return "FAIL", "unable to read OMP model catalog"
    try:
        providers = detected_routing_providers(recommendations, available)
    except runtime.DotAiError as exc:
        return "FAIL", str(exc)

    persisted_providers = set(routing["providers"])
    detected_providers = set(providers)
    if not persisted_providers & detected_providers:
        return "INACTIVE", "configured providers are unavailable"
    if persisted_providers != detected_providers:
        return (
            "DRIFT",
            "authenticated providers changed; run 'dotai configure omp-routing'",
        )

    primaries, fallbacks, unavailable = resolve_omp_routing(
        recommendations,
        routing["providers"],
        routing["primaryProvider"],
        available,
    )
    if unavailable:
        return "DRIFT", f"unavailable roles: {', '.join(unavailable)}"

    record_keys = ("modelRoles", "retry.fallbackChains", "task.agentModelOverrides")
    scalar_values = omp_routing_scalar_values(routing)
    current = {key: configured_omp_value(runner, key) for key in (*record_keys, *scalar_values)}
    if any(
        not isinstance(current[key], dict) or any(not isinstance(name, str) for name in current[key])
        for key in record_keys
    ) or any(current[key] is None for key in scalar_values):
        return "FAIL", "unable to read required OMP configuration"
    for role, primary in primaries.items():
        if current["modelRoles"].get(role) != primary:
            return "DRIFT", f"model role differs: {role}"
    for role, chain in fallbacks.items():
        if current["retry.fallbackChains"].get(role) != chain:
            return "DRIFT", f"fallback chain differs: {role}"
    for agent, model in routing["agentModelOverrides"].items():
        if current["task.agentModelOverrides"].get(agent) != model:
            return "DRIFT", f"agent override differs: {agent}"
    for key, value in scalar_values.items():
        if type(current[key]) is not type(value) or current[key] != value:
            return "DRIFT", f"{key} differs"
    return "OK", "configured roles match"
