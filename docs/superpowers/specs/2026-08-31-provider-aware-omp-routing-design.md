# Provider-Aware OMP Routing Design

## Goal

Replace the static Codex-plus-Copilot routing manifest with an explicit post-authentication workflow that discovers the user's available OMP providers, selects curated recommendations for that actual provider set, and configures role-based fallback routing. Copilot-only, Anthropic-only, and mixed Anthropic/Codex/Copilot setups must work without storing irrelevant model lists in the user-owned manifest.

## Scope

- Keep provider authentication inside OMP and outside DotAi.
- Keep `install`, `update`, and `sync` free of provider discovery and routing writes.
- Change `dotai configure omp-routing` into the command that discovers supported authenticated providers, resolves recommendations, persists compact routing intent, and applies it to OMP.
- Support `github-copilot`, `openai-codex`, and `anthropic` recommendation sets.
- Preserve usage-aware fallback, agent role overrides, status reporting, dry-run safety, idempotence, and unrelated OMP settings.
- Migrate the recently introduced static `ompRouting.roles` format through the explicit configure command.

Out of scope:

- Authenticating providers, reading OMP's credential database, or storing credentials.
- Automatically ranking arbitrary OMP providers without curated recommendations.
- Price-, latency-, usage-, or prompt-classification-based model selection beyond OMP's existing usage-aware fallback.
- Changing routing during `install`, `update`, or `sync`.

## Configuration boundaries

### Tracked recommendation catalog

Add `routing-recommendations.json` as internal declarative application data. It is tracked with DotAi but is not copied into `stack.json`. This keeps provider and model policy out of `dotai.py` without adding irrelevant model selectors to each user's configuration.

The catalog contains:

- a schema/version marker for fail-fast compatibility checks;
- the supported provider IDs;
- ordered model selector recommendations for `default`, `task`, `smol`, and `slow` per provider;
- default agent overrides for `sonic: @smol` and `task: @task`.

The initial catalog shape is exact:

```json
{
  "version": 1,
  "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
  "providers": {
    "github-copilot": {
      "roles": {
        "default": ["github-copilot/gpt-5.6-terra"],
        "task": ["github-copilot/gpt-5.6-terra", "github-copilot/gpt-5.6-luna"],
        "smol": ["github-copilot/gpt-5.6-luna", "github-copilot/gpt-5.6-terra"],
        "slow": ["github-copilot/gpt-5.6-terra:high", "github-copilot/gpt-5.6-luna:high"]
      }
    },
    "openai-codex": {
      "roles": {
        "default": ["openai-codex/gpt-5.6-sol"],
        "task": ["openai-codex/gpt-5.6-sol"],
        "smol": ["openai-codex/gpt-5.4-mini"],
        "slow": ["openai-codex/gpt-5.6-sol:high"]
      }
    },
    "anthropic": {
      "roles": {
        "default": ["anthropic/claude-opus-4-8", "anthropic/claude-opus-4-7", "anthropic/claude-opus-4-6"],
        "task": ["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-8"],
        "smol": ["anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-6"],
        "slow": ["anthropic/claude-opus-4-8:high", "anthropic/claude-opus-4-7:high", "anthropic/claude-opus-4-6:high"]
      }
    }
  }
}
```

`version` must equal `1`. Each provider must define exactly the four managed roles, and every role must contain a non-empty ordered array of non-empty selectors belonging to that provider.

Initial recommendations:

| Provider | `default` | `task` | `smol` | `slow` |
| --- | --- | --- | --- | --- |
| `github-copilot` | GPT-5.6 Terra | GPT-5.6 Terra, then Luna | GPT-5.6 Luna, then Terra | GPT-5.6 Terra, then Luna at high effort |
| `openai-codex` | GPT-5.6 Sol | GPT-5.6 Sol | GPT-5.4 Mini | GPT-5.6 Sol at high effort |
| `anthropic` | Claude Opus 4.8, then 4.7 and 4.6 | Claude Sonnet 4.6, then Opus 4.8 | Claude Haiku 4.5, then Sonnet 4.6 | Claude Opus 4.8, then 4.7 and 4.6 at high effort |

Selectors use OMP's exact provider/model IDs. Thinking suffixes remain part of the configured selector but not model-catalog identity matching, preserving the existing `:high` behavior.

DotAi validates the catalog when routing needs it. A malformed version, provider entry, role, selector list, or default override fails with a clear error before any manifest or OMP write.

### User manifest

`stack.example.json` changes to:

```json
"ompRouting": null
```

A null or absent section means routing has not been configured. The JSON Schema and Python validation must agree on this state.

After configuration, the user-owned `stack.json` contains compact intent only:

```json
{
  "ompRouting": {
    "providers": ["anthropic", "github-copilot"],
    "primaryProvider": "anthropic",
    "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
    "usageReservePct": 10,
    "usageReservePolicy": "auto",
    "fallbackRevertPolicy": "cooldown-expiry"
  }
}
```

Rules:

- `providers` is a non-empty unique array of supported provider IDs in deterministic order.
- `primaryProvider` is one of `providers` and controls interactive roles only.
- `agentModelOverrides` remains an optional non-empty-string mapping so users can customize agent assignments without editing the recommendation catalog.
- Existing reserve and revert fields retain their current validation and defaults.
- Exact model selectors and resolved fallback chains do not persist in `stack.json`.

## Provider discovery and selection

`omp models --json` is the sole source of provider availability. OMP already limits this catalog to providers with resolvable authentication and enabled models; DotAi must not inspect credentials directly.

The configure command considers only supported providers that expose at least one exact recommended model. An authenticated supported provider whose available models match none of its recommendations is an error, not a silent omission, because the tracked recommendation catalog is stale for that provider.

Interactive primary selection follows these rules:

1. One supported provider: use it without prompting.
2. Copilot plus Anthropic: Anthropic is primary.
3. Copilot plus Codex: Codex is primary.
4. Anthropic plus Codex: reuse a valid persisted `primaryProvider`; otherwise require a choice between them.
5. All three providers: the Anthropic-versus-Codex rule still decides the interactive primary; Copilot does not enter that prompt.

`--primary anthropic|openai-codex` supplies the choice for automation. It is valid only when that provider is discovered and is an eligible interactive primary. If an Anthropic-versus-Codex choice is required without a persisted value or flag, an interactive terminal prompts once. A non-interactive process fails before writes and tells the user to pass `--primary`.

## Role resolution

Provider ordering is separate from each provider's ordered model recommendations.

- `default` and `slow`: selected interactive primary first, the other available premium provider second, Copilot last.
- `task` and `smol`: Copilot first, Anthropic second, Codex third.

For each role, DotAi concatenates the available providers' recommendation lists in that provider order, removes duplicate selectors while retaining first occurrence, and filters candidates against the OMP model catalog using selector identity. The first retained selector becomes `modelRoles[role]`; the complete retained list becomes `retry.fallbackChains[role]`.

Every managed role must resolve at least one candidate. Failure to resolve any role aborts before manifest or OMP writes. This prevents a partially configured agent topology.

This policy preserves the existing Codex-primary/Copilot-worker behavior, makes Anthropic primary with Copilot workers, and still uses both Anthropic and Codex when both are available. The Anthropic-versus-Codex choice affects only `default` and `slow`; worker roles remain specialized.

## Configure lifecycle

`dotai configure omp-routing [--primary PROVIDER] [--dry-run]` performs these steps:

1. Load the selected manifest, permitting the old static routing shape only for this command's one-way migration path.
2. Load and validate `routing-recommendations.json`.
3. Run `omp models --json`, validate the complete catalog, and identify supported authenticated providers.
4. Validate that each detected supported provider has at least one available recommended selector.
5. Resolve or prompt for the interactive primary.
6. Build all four role primaries and fallback chains; require every role to resolve.
7. Read every managed OMP record and scalar before modifying state.
8. Build the compact manifest intent and the merged OMP desired records.
9. Print detected providers, the interactive primary, resolved role primaries, fallback chains, manifest changes, and OMP writes.
10. For `--dry-run`, return after the preview without creating a backup, writing the manifest, or invoking `omp config set`.
11. If the manifest intent changed, create a timestamped backup and atomically replace `stack.json`.
12. Apply only differing OMP settings.

Managed OMP values remain:

- `modelRoles` for `default`, `task`, `smol`, and `slow`;
- `retry.fallbackChains` for those roles;
- configured `task.agentModelOverrides` entries;
- `retry.modelFallback: true`;
- `retry.usageAwareFallback: true`;
- `retry.usageReservePct`;
- `retry.usageReservePolicy`;
- `retry.fallbackRevertPolicy`.

Existing unrelated record entries are merged and preserved. Repeated execution with the same provider catalog, preference, manifest intent, and OMP state creates no backup and performs no writes.

If manifest persistence succeeds but an OMP write fails, the compact manifest remains the desired state. `status` reports drift, and a later configure run converges. If manifest persistence fails, no OMP write begins.

## Legacy static routing migration

The old `ompRouting.roles` shape is not a second permanent routing mode.

- Normal validation for commands other than `configure omp-routing` rejects it with an instruction to run the configure command.
- The configure command may load the old shape only to obtain existing reserve, revert, and agent override values.
- Provider detection and the current recommendation catalog determine the replacement provider list and routes; old exact role selectors are not copied.
- A successful non-dry-run creates a manifest backup and replaces the old section with compact intent before applying OMP settings.
- Dry-run previews the one-way replacement without writing.

## Status behavior

An absent or null `ompRouting` remains unconfigured and does not make the overall stack unhealthy.

For configured compact intent, status loads the recommendation catalog and current OMP model catalog, derives the expected routes, and remains observational:

- `OK`: persisted providers match the currently available supported providers, every role resolves, and all managed OMP values match.
- `DRIFT`: at least one configured route can resolve, but provider availability changed, a recommendation changed, or a managed OMP value differs. The detail tells the user to rerun `configure omp-routing` when provider selection changed.
- `INACTIVE`: none of the persisted providers or routes is currently available.
- `FAIL`: the recommendation catalog, OMP model catalog, or required OMP configuration is unreadable.

Newly authenticated supported providers and logged-out persisted providers produce provider-selection drift rather than being silently ignored. Status never prompts, mutates the manifest, or writes OMP settings.

## Error and safety behavior

Before any write, fail for:

- unreadable or malformed recommendation/model catalogs;
- no supported authenticated provider;
- a detected supported provider with no available recommendation;
- an invalid or unavailable `--primary` value;
- an unresolved managed role;
- a required primary choice in a non-interactive process;
- unreadable required OMP configuration.

Provider credentials, account identifiers, tokens, and quota details are never read from storage, written to the manifest, or printed. Dry runs may perform the same observational OMP reads as a real configure run but modify neither files nor machine state.

## Files and responsibilities

- `routing-recommendations.json`: tracked provider/model recommendation data and default agent overrides.
- `stack.example.json`: unconfigured `ompRouting: null` baseline.
- `stack.schema.json`: nullable unconfigured shape plus compact configured-intent contract.
- `dotai.py`: catalog validation, provider selection, role resolution, compact persistence, one-way migration, reconciliation, status, CLI prompt/flag behavior.
- `tests/test_dotai.py`: behavioral coverage for every provider combination and lifecycle invariant.
- `README.md`: post-install authentication/configuration workflow, automatic selection rules, ambiguous-primary prompt, `--primary`, and migration guidance.

## Tests and verification

Behavioral tests must cover:

- Recommendation catalog and compact manifest validation, including nullable unconfigured state.
- Copilot-only and Anthropic-only role resolution.
- Existing Copilot-plus-Codex routing: Codex interactive, Copilot workers.
- Copilot-plus-Anthropic routing: Anthropic interactive, Copilot workers.
- Anthropic-plus-Codex routing with either interactive primary choice.
- All three providers, including Copilot worker specialization and both interactive choices.
- Exact filtering, thinking suffixes, duplicate removal, missing recommendations, and unresolved roles.
- Prompt reuse, explicit `--primary`, invalid flags, cancellation/EOF, and non-interactive failure.
- Compact manifest backup/write, dry-run zero mutation, idempotence, and one-way static-role migration.
- Preservation of unrelated OMP roles, chains, overrides, and top-level user configuration.
- Status labels for matching state, changed provider availability, changed recommendations, unavailable providers, and unreadable data.
- Proof that `install`, `update`, and `sync` do not discover or configure routing.
- Alignment among `stack.example.json`, `stack.schema.json`, runtime validation, and README instructions.

Final verification:

```sh
rtk python3 -m unittest discover -s tests -v
rtk python3 dotai.py validate
rtk python3 dotai.py configure omp-routing --dry-run --primary openai-codex
```

The smoke check must discover the installed OMP provider catalog, preview compact manifest intent and role chains, and leave both `stack.json` and OMP configuration unchanged.
