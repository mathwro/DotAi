# OMP multi-provider routing design

## Goal

Add an explicit, manifest-driven DotAi workflow that configures Oh My Pi (OMP) model roles and retry fallbacks from the models available after a user authenticates providers. The workflow must make Codex the preferred interactive model when available, route ordinary concurrent subagents through GitHub Copilot, and fail over safely when either provider is unavailable or reaches a quota limit.

## Scope

- Add an optional `ompRouting` manifest section to `stack.schema.json` and `stack.example.json`.
- Add `dotai configure omp-routing [--dry-run]`.
- Detect available OMP models through `omp models --json` only when the explicit configure command runs.
- Reconcile managed OMP settings while preserving unrelated user settings.
- Report routing health through `dotai status`.
- Leave `install`, `update`, and `sync` free of provider detection, authentication checks, and model-routing writes.

Out of scope:

- Obtaining provider credentials or performing OAuth sign-in.
- Storing credentials, account data, usage figures, or API keys in the manifest.
- Selecting providers by price, latency, or prompt classification.
- Changing user-owned selectors outside the managed routing keys.

## Manifest format

`ompRouting` is optional. Its absence leaves all routing behavior unchanged.

```json
{
  "ompRouting": {
    "roles": {
      "default": [
        "openai-codex/gpt-5.6-sol",
        "github-copilot/gpt-5.6-terra"
      ],
      "task": [
        "github-copilot/gpt-5.6-terra",
        "github-copilot/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol"
      ],
      "smol": [
        "github-copilot/gpt-5.6-luna",
        "github-copilot/gpt-5.6-terra",
        "openai-codex/gpt-5.4-mini"
      ],
      "slow": [
        "openai-codex/gpt-5.6-sol:high",
        "github-copilot/gpt-5.6-terra:high",
        "github-copilot/gpt-5.6-luna:high"
      ]
    },
    "agentModelOverrides": {
      "sonic": "@smol",
      "task": "@task"
    },
    "usageReservePct": 10,
    "usageReservePolicy": "auto",
    "fallbackRevertPolicy": "cooldown-expiry"
  }
}
```

### Validation

- `roles` is a non-empty object whose keys and ordered selector values are non-empty strings.
- Each role has at least one candidate selector.
- `agentModelOverrides` is an optional string-to-string object.
- `usageReservePct` is an integer from 0 through 100.
- `usageReservePolicy` is `confirm`, `auto`, or `fail-closed`.
- `fallbackRevertPolicy` is `cooldown-expiry` or `never`.

The application performs matching validation, rather than relying on schema validation alone.

## Configure command

`dotai configure omp-routing` follows this sequence:

1. Load and validate the selected manifest.
2. Require `omp` to be executable.
3. Run `omp models --json` and parse the returned model selectors.
4. For each configured role, retain candidate selectors available in that model catalog. A selector's optional `:thinking` suffix does not affect catalog membership.
5. Select the first available candidate as the role's primary `modelRoles` value; map the complete ordered list of available candidates, including that primary, to `retry.fallbackChains[role]`.
6. Fail without writes if every configured role has no available candidate. Roles with no matching candidates are reported and excluded; at least one resolved role is required for a successful configuration.
7. Read existing OMP records with `omp config get ... --json` for `modelRoles`, `retry.fallbackChains`, and `task.agentModelOverrides`.
8. Merge only configured role keys, fallback-chain keys, and agent override keys. Retain all unrelated records already in OMP.
9. Set the merged records and these scalar values:
   - `retry.modelFallback: true`
   - `retry.usageAwareFallback: true`
   - `retry.usageReservePct` from the manifest
   - `retry.usageReservePolicy` from the manifest
   - `retry.fallbackRevertPolicy` from the manifest
10. Skip each write whose effective value is already equal to the desired value.

`--dry-run` prints the discovered providers, resolved role primaries, fallback chains, skipped roles, and pending writes; it does not invoke `omp config set`.

The command uses normal OMP configuration commands. It never reads OMP’s credential database or prints credentials.

## Default routing policy

The tracked example config expresses the following preference order:

| Work type | Primary | Fallback |
| --- | --- | --- |
| Main interactive session (`default`) | OpenAI Codex GPT-5.6 Sol | GitHub Copilot GPT-5.6 Terra |
| General subagents (`task`) | GitHub Copilot GPT-5.6 Terra | GitHub Copilot GPT-5.6 Luna, then OpenAI Codex GPT-5.6 Sol |
| Lightweight workers (`smol`) | GitHub Copilot GPT-5.6 Luna | GitHub Copilot GPT-5.6 Terra, then OpenAI Codex GPT-5.4 Mini |
| Hard reasoning (`slow`) | OpenAI Codex GPT-5.6 Sol, high effort | GitHub Copilot GPT-5.6 Terra, then Luna, high effort |

`task.agentModelOverrides` binds bundled `sonic` agents to `@smol` and bundled general `task` agents to `@task`. This preserves Codex capacity for interactive and difficult work while allowing concurrent worker load to use Copilot first.

Usage-aware fallback uses a 10% reserve with automatic selection. Where OMP can obtain reliable coding-plan quota reports, it switches before hard exhaustion; normal error/cooldown fallback remains the behavior for all other cases. `cooldown-expiry` returns sessions to their preferred primary model after the provider’s cooldown ends.

## Status behavior

When `ompRouting` exists, `dotai status` reports a routing row:

- `OK`: every configured role has a primary selector currently available, and its OMP role plus fallback chain match the resolved desired configuration.
- `DRIFT`: OMP is available and at least one configured role can resolve, but a managed role, fallback chain, agent override, or retry scalar differs.
- `INACTIVE`: OMP is installed but no routing candidate is currently available. This commonly means authentication has not yet occurred.
- `FAIL`: OMP cannot be executed or its model/config output is unreadable.

Status detection is observational. It does not modify OMP settings and does not make `status` succeed when the routing configuration is inactive or drifted.

## Installation and reconciliation invariants

- `install` continues to install packages and reconcile existing package, extension, skill, plugin, and MCP behavior only.
- `update` and `sync` continue to omit provider/model detection and routing writes.
- The explicit configure command is safe to repeat and preserves unmanaged OMP record entries.
- A first-time user can run `dotai install`, authenticate providers using OMP, then run `dotai configure omp-routing`.

## Tests and verification

Extend `tests/test_dotai.py` with focused behavioral coverage for:

- Manifest validation errors for malformed `ompRouting` values.
- Selector matching, including thinking-suffix handling and unavailable candidates.
- Primary/fallback construction from a parsed `omp models --json` response.
- Merging managed entries with unrelated `modelRoles`, fallback chains, and agent overrides.
- Idempotence and omitted writes.
- Dry-run output and zero mutation.
- No available candidates and unreadable OMP output.
- `status` labels for OK, DRIFT, INACTIVE, and FAIL.
- Explicit proof that `install`, `update`, and `sync` do not call `omp models` or configure routing.

After implementation, run the focused unit suite, validate the local manifest, dry-run the new command, apply it on the current machine, and read back its OMP records.