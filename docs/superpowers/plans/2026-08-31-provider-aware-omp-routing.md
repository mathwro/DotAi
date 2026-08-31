# Provider-Aware OMP Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure intelligent OMP routing from the user's authenticated Copilot, Codex, and Anthropic providers while storing only compact provider intent in `stack.json`.

**Architecture:** A tracked `routing-recommendations.json` owns exact provider/model recommendations. Pure Python helpers validate that catalog, detect supported providers from `omp models --json`, choose the interactive primary, and resolve role chains; the explicit configure command persists compact intent before reconciling OMP, while status derives the same desired state without mutation.

**Tech Stack:** Python 3.10+ standard library, JSON Schema draft-07, `argparse`, `unittest`, Oh My Pi CLI.

**Spec:** `docs/superpowers/specs/2026-08-31-provider-aware-omp-routing-design.md`

## Global Constraints

- Work only in `.worktrees/provider-neutral-routing` on branch `feature/provider-neutral-routing`.
- Keep runtime compatibility with Python 3.10 or newer and add no dependency.
- Keep provider/model recommendations declarative; do not put recommendation lists in `dotai.py`.
- Never read OMP credential storage or persist/print credentials, account identifiers, tokens, or quota details.
- `install`, `update`, and `sync` must not discover providers or configure routing.
- Preserve unmanaged OMP record entries and skip writes whose effective values already match.
- Dry runs may read provider/model/config state but must not modify files or machine state.
- Use a one-way migration for static `ompRouting.roles`; do not retain it as a second runtime routing mode.
- Prefix every shell command with `rtk`.

## File Structure

| File | Responsibility |
| --- | --- |
| `routing-recommendations.json` | Versioned exact recommendation lists and default agent overrides; never copied wholesale into user manifests. |
| `stack.schema.json` | Nullable unconfigured state and compact configured `ompRouting` contract. |
| `stack.example.json` | New-user baseline with `ompRouting: null`. |
| `dotai.py` | Catalog/manifest validation, provider choice, role resolution, configure persistence/reconciliation, migration, status, CLI. |
| `tests/test_dotai.py` | Observable contract tests for all provider combinations, mutation safety, migration, status, and command isolation. |
| `README.md` | Post-authentication workflow, automatic selection rules, prompt/flag behavior, and migration guidance. |

---

### Task 1: Define recommendation and compact manifest contracts

**Files:**
- Create: `routing-recommendations.json`
- Modify: `stack.schema.json:49-73`
- Modify: `stack.example.json:128-139`
- Modify: `dotai.py:21-31,209-288`
- Test: `tests/test_dotai.py:40-140,1194-1220`

**Interfaces:**
- Produces `ROUTING_RECOMMENDATIONS: Path` and `ROUTING_ROLES: tuple[str, ...]`.
- Produces `validate_routing_recommendations(value: Any) -> dict[str, Any]`.
- Produces `load_routing_recommendations(path: Path = ROUTING_RECOMMENDATIONS) -> dict[str, Any]`.
- Changes `validate_omp_routing(value: Any, *, allow_legacy: bool = False) -> dict[str, Any]` to return `{}` for null, normalized compact intent for configured data, or validated old data only when `allow_legacy=True`.
- Changes `load_manifest(path: Path, *, allow_legacy_routing: bool = False) -> dict[str, Any]` to pass the configure-only migration flag.

- [ ] **Step 1: Write failing contract tests**

Replace the static-role validation expectations with compact intent and catalog tests:

```python
def compact_routing(self, providers: list[str], primary: str) -> dict:
    return {
        "providers": providers,
        "primaryProvider": primary,
        "agentModelOverrides": {"sonic": "@smol", "task": "@task"},
        "usageReservePct": 10,
        "usageReservePolicy": "auto",
        "fallbackRevertPolicy": "cooldown-expiry",
    }


def test_routing_recommendation_catalog_is_exact_and_valid(self) -> None:
    recommendations = DOTAI.load_routing_recommendations()
    self.assertEqual(recommendations["version"], 1)
    self.assertEqual(set(recommendations["providers"]), {
        "github-copilot", "openai-codex", "anthropic"
    })
    self.assertEqual(
        recommendations["providers"]["anthropic"]["roles"]["smol"],
        ["anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-6"],
    )
    self.assertEqual(
        recommendations["providers"]["github-copilot"]["roles"]["task"][0],
        "github-copilot/gpt-5.6-terra",
    )


def test_validate_omp_routing_accepts_compact_intent_and_null(self) -> None:
    routing = self.compact_routing(["anthropic", "github-copilot"], "anthropic")
    self.assertEqual(DOTAI.validate_omp_routing(routing), routing)
    self.assertEqual(DOTAI.validate_omp_routing(None), {})


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
        with self.subTest(value=value), self.assertRaises(DOTAI.DotAiError):
            DOTAI.validate_omp_routing(value)


def test_static_routing_is_only_accepted_for_configure_migration(self) -> None:
    legacy = {"roles": {"default": ["openai-codex/gpt-5.6-sol"]}}
    with self.assertRaisesRegex(DOTAI.DotAiError, "configure omp-routing"):
        DOTAI.validate_omp_routing(legacy)
    self.assertEqual(
        DOTAI.validate_omp_routing(legacy, allow_legacy=True)["roles"],
        legacy["roles"],
    )
```

Add the malformed catalog and raw example checks explicitly:

```python
def test_validate_routing_recommendations_rejects_malformed_data(self) -> None:
    valid = DOTAI.load_routing_recommendations()
    missing_role = json.loads(json.dumps(valid))
    del missing_role["providers"]["anthropic"]["roles"]["smol"]
    empty_role = json.loads(json.dumps(valid))
    empty_role["providers"]["anthropic"]["roles"]["smol"] = []
    cross_provider = json.loads(json.dumps(valid))
    cross_provider["providers"]["anthropic"]["roles"]["smol"] = [
        "openai-codex/gpt-5.4-mini"
    ]
    invalid = [
        {**valid, "version": 2},
        {"version": 1, "agentModelOverrides": {}},
        missing_role,
        empty_role,
        cross_provider,
        {**valid, "agentModelOverrides": []},
    ]
    for value in invalid:
        with self.subTest(value=value), self.assertRaises(DOTAI.DotAiError):
            DOTAI.validate_routing_recommendations(value)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "routing-recommendations.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(DOTAI.DotAiError):
            DOTAI.load_routing_recommendations(path)


def test_repository_example_starts_with_unconfigured_routing(self) -> None:
    raw = json.loads((ROOT / "stack.example.json").read_text(encoding="utf-8"))
    self.assertIsNone(raw["ompRouting"])
    self.assertEqual(DOTAI.load_manifest(ROOT / "stack.example.json")["ompRouting"], {})
```

- [ ] **Step 2: Run contract tests and verify RED**

Run:

```sh
rtk python3 -m unittest \
  tests.test_dotai.DotAiTests.test_routing_recommendation_catalog_is_exact_and_valid \
  tests.test_dotai.DotAiTests.test_validate_omp_routing_accepts_compact_intent_and_null \
  tests.test_dotai.DotAiTests.test_validate_omp_routing_rejects_invalid_compact_intent \
  tests.test_dotai.DotAiTests.test_validate_routing_recommendations_rejects_malformed_data \
  tests.test_dotai.DotAiTests.test_repository_example_starts_with_unconfigured_routing \
  tests.test_dotai.DotAiTests.test_static_routing_is_only_accepted_for_configure_migration -v
```

Expected: FAIL because the recommendation loader and compact manifest contract do not exist.

- [ ] **Step 3: Create the exact recommendation catalog**

Create `routing-recommendations.json` with this complete content:

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

- [ ] **Step 4: Replace schema and example routing data**

Set `stack.example.json`'s entire routing value to null. Replace the schema block with `oneOf` null/configured forms; the configured object requires `providers` and `primaryProvider`, forbids unknown properties, makes provider entries unique and restricted to the three supported IDs, constrains `primaryProvider` to those IDs, and retains the exact existing policy defaults and agent override string mapping.

The configured schema body must be equivalent to:

```json
{
  "oneOf": [
    {"type": "null"},
    {
      "type": "object",
      "required": ["providers", "primaryProvider"],
      "additionalProperties": false,
      "properties": {
        "providers": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": {"enum": ["github-copilot", "openai-codex", "anthropic"]}
        },
        "primaryProvider": {"enum": ["github-copilot", "openai-codex", "anthropic"]},
        "agentModelOverrides": {
          "type": "object",
          "propertyNames": {"type": "string", "minLength": 1},
          "additionalProperties": {"type": "string", "minLength": 1}
        },
        "usageReservePct": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
        "usageReservePolicy": {"enum": ["confirm", "auto", "fail-closed"], "default": "auto"},
        "fallbackRevertPolicy": {"enum": ["cooldown-expiry", "never"], "default": "cooldown-expiry"}
      }
    }
  ]
}
```

Python validation additionally requires `primaryProvider in providers`, which draft-07 cannot express locally without duplicating branches.

- [ ] **Step 5: Implement catalog and manifest validators**

Add constants after `EXAMPLE_MANIFEST`:

```python
ROUTING_RECOMMENDATIONS = ROOT / "routing-recommendations.json"
ROUTING_ROLES = ("default", "task", "smol", "slow")
```

`validate_routing_recommendations()` must require version `1`, exactly the top-level keys shown in the catalog, exactly the four managed roles for every provider, non-empty string selector arrays, selector prefixes equal to the containing provider, and non-empty string override keys/values. Return the input object after validation; do not invent model defaults in Python.

`load_routing_recommendations()` must parse UTF-8 JSON, translate file/JSON failures to `DotAiError`, and call the validator.

Refactor the shared policy validation in `validate_omp_routing()` without changing its values. Compact mode requires the exact allowed keys, a non-empty unique string provider list drawn from `load_routing_recommendations()["providers"]`, and a primary present in that list. Legacy mode validates the old role arrays and returns them only when `allow_legacy=True`; otherwise raise `DotAiError("Manifest 'ompRouting.roles' is obsolete; run 'dotai configure omp-routing' to migrate")`.

Pass `allow_legacy_routing` from `load_manifest()` to `validate_omp_routing()`.

- [ ] **Step 6: Run contract tests and verify GREEN**

Run the Step 2 command plus:

```sh
rtk python3 -m unittest \
  tests.test_dotai.DotAiTests.test_stack_schema_has_exact_omp_routing_defaults \
  tests.test_dotai.DotAiTests.test_repository_example_manifest_has_no_winget_commands -v
```

Expected: PASS; the raw example is null and runtime normalization remains `{}`.

- [ ] **Step 7: Commit the data contracts**

```sh
rtk git add routing-recommendations.json stack.schema.json stack.example.json dotai.py tests/test_dotai.py
rtk git commit -m "feat: define provider routing recommendations"
```

---

### Task 2: Resolve providers, primary choice, and role chains

**Files:**
- Modify: `dotai.py:496-534`
- Test: `tests/test_dotai.py:220-286`

**Interfaces:**
- Consumes the validated recommendation catalog and the selector set returned by `available_omp_models()`.
- Produces `detected_routing_providers(recommendations: dict[str, Any], available: set[str]) -> list[str]`.
- Produces `choose_primary_provider(providers: list[str], current: str | None, requested: str | None) -> str`.
- Changes `resolve_omp_routing(recommendations: dict[str, Any], providers: list[str], primary: str, available: set[str]) -> tuple[dict[str, str], dict[str, list[str]], list[str]]`.

- [ ] **Step 1: Write failing provider detection tests**

Use the tracked catalog and exact selectors:

```python
def test_detected_routing_providers_require_supported_recommendations(self) -> None:
    recommendations = DOTAI.load_routing_recommendations()
    available = {
        "github-copilot/gpt-5.6-terra",
        "openai-codex/gpt-5.6-sol",
        "private/model",
    }
    self.assertEqual(
        DOTAI.detected_routing_providers(recommendations, available),
        ["github-copilot", "openai-codex"],
    )
    with self.assertRaisesRegex(DOTAI.DotAiError, "no recommended models"):
        DOTAI.detected_routing_providers(
            recommendations,
            {"anthropic/claude-unknown"},
        )
    self.assertEqual(
        DOTAI.detected_routing_providers(recommendations, {"private/model"}),
        [],
    )
```

- [ ] **Step 2: Write failing primary-selection tests**

Cover deterministic cases without prompting:

```python
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
                DOTAI.choose_primary_provider(providers, current, requested),
                expected,
            )
```

For the ambiguous case, patch `sys.stdin.isatty()` true and `builtins.input` to return `"1"` and `"2"`; assert Anthropic and Codex respectively. Patch `isatty()` false and assert `DotAiError` names `--primary`. Assert unavailable requested providers and any response other than `1`/`2` fail without a retry loop. Patch `builtins.input` to raise `EOFError` and `KeyboardInterrupt` in separate subtests and assert both become the same cancellation `DotAiError` before any state change.

- [ ] **Step 3: Write failing role-resolution matrix tests**

Create available sets from one selector per role/provider and assert full chains for these cases:

```python
cases = [
    (["github-copilot"], "github-copilot", {
        "default": "github-copilot/gpt-5.6-terra",
        "task": "github-copilot/gpt-5.6-terra",
        "smol": "github-copilot/gpt-5.6-luna",
        "slow": "github-copilot/gpt-5.6-terra:high",
    }),
    (["anthropic"], "anthropic", {
        "default": "anthropic/claude-opus-4-8",
        "task": "anthropic/claude-sonnet-4-6",
        "smol": "anthropic/claude-haiku-4-5",
        "slow": "anthropic/claude-opus-4-8:high",
    }),
    (["github-copilot", "openai-codex"], "openai-codex", {
        "default": "openai-codex/gpt-5.6-sol",
        "task": "github-copilot/gpt-5.6-terra",
        "smol": "github-copilot/gpt-5.6-luna",
        "slow": "openai-codex/gpt-5.6-sol:high",
    }),
    (["anthropic", "github-copilot"], "anthropic", {
        "default": "anthropic/claude-opus-4-8",
        "task": "github-copilot/gpt-5.6-terra",
        "smol": "github-copilot/gpt-5.6-luna",
        "slow": "anthropic/claude-opus-4-8:high",
    }),
]
```

Add Anthropic-plus-Codex cases for each primary and all-three cases for each primary. For every case, assert the expected primaries, no unavailable roles, `fallbacks["default"]` begins with the chosen premium provider, and `fallbacks["task"]`/`["smol"]` use provider order Copilot, Anthropic, Codex when each is present.

Retain explicit duplicate-removal and `:high` identity coverage. Remove one role's available selectors and assert that role appears in `unavailable`.

- [ ] **Step 4: Run pure routing tests and verify RED**

```sh
rtk python3 -m unittest \
  tests.test_dotai.DotAiTests.test_detected_routing_providers_require_supported_recommendations \
  tests.test_dotai.DotAiTests.test_choose_primary_provider_applies_subscription_rules \
  tests.test_dotai.DotAiTests.test_resolve_omp_routing_handles_provider_combinations -v
```

Expected: FAIL because detection/choice helpers do not exist and the resolver still consumes static roles.

- [ ] **Step 5: Implement minimal pure routing helpers**

`detected_routing_providers()` must derive provider IDs from available selectors, intersect with catalog keys, require at least one available recommended selector identity per detected supported provider, and return sorted IDs. It ignores unsupported providers and returns an empty list when no supported provider is available; configure turns that empty result into an error, while status turns it into `INACTIVE`.

`choose_primary_provider()` must validate `requested` before applying rules. Use premium order `("anthropic", "openai-codex")`; Copilot is selected only when neither premium provider exists. For two premiums, requested wins, then a valid persisted current value, then one terminal prompt:

```text
Choose interactive primary: [1] Anthropic [2] OpenAI Codex: 
```

`resolve_omp_routing()` must use these provider orders:

```python
interactive_order = [
    primary,
    *(provider for provider in ("anthropic", "openai-codex") if provider in providers and provider != primary),
    *(provider for provider in ("github-copilot",) if provider in providers and provider != primary),
]
worker_order = [
    provider for provider in ("github-copilot", "anthropic", "openai-codex")
    if provider in providers
]
```

For each managed role, flatten its providers' recommendation arrays, retain each exact selector once in first-seen order, filter by `selector_identity()`, set the first retained selector as primary, and record an unavailable role when no selector remains. Keep the helper side-effect free.

- [ ] **Step 6: Run pure routing tests and verify GREEN**

Run the Step 4 command plus the existing available-model and selector-identity tests. Expected: PASS.

- [ ] **Step 7: Commit provider-aware resolution**

```sh
rtk git add dotai.py tests/test_dotai.py
rtk git commit -m "feat: resolve routing from authenticated providers"
```

---

### Task 3: Persist compact intent and reconcile OMP

**Files:**
- Modify: `dotai.py:548-615,1085-1145,1229-1233,1294-1341`
- Test: `tests/test_dotai.py:287-549`

**Interfaces:**
- Produces `build_omp_routing_intent(routing: dict[str, Any], recommendations: dict[str, Any], providers: list[str], primary: str) -> dict[str, Any]`.
- Changes `configure_omp_routing(manifest: dict[str, Any], path: Path, runner: Runner, requested_primary: str | None = None) -> int`.
- `main()` calls `load_manifest(..., allow_legacy_routing=True)` only for `configure omp-routing`, then passes `args.manifest` and `args.primary` to the configurator.
- The CLI exposes `dotai configure omp-routing [--primary anthropic|openai-codex] [--dry-run]`.

- [ ] **Step 1: Write failing compact persistence test**

Create a temporary manifest with `ompRouting: null`, use an available catalog containing Copilot and Codex recommendations, and supply valid current OMP values through `runner.output`. Call:

```python
DOTAI.configure_omp_routing(manifest, path, runner)
```

Assert:

```python
saved = json.loads(path.read_text(encoding="utf-8"))
self.assertEqual(saved["ompRouting"]["providers"], ["github-copilot", "openai-codex"])
self.assertEqual(saved["ompRouting"]["primaryProvider"], "openai-codex")
self.assertNotIn("roles", saved["ompRouting"])
self.assertEqual(len(list(path.parent.glob("stack.json.bak.*"))), 1)
```

Assert the `modelRoles` payload keeps unrelated `custom` data, uses Codex for `default`/`slow`, and uses Copilot for `task`/`smol`. Assert fallback chains include both providers in the approved role-specific order and agent overrides contain catalog defaults.

- [ ] **Step 2: Write failing dry-run, idempotence, and migration tests**

Dry-run: capture the manifest bytes and backup count before the call, then assert both remain unchanged and `runner.run` is never called; output must contain the manifest diff, detected providers, primary, role primaries, chains, and pending OMP commands.

Idempotence: start from already compact intent and matching OMP values; assert no backup, no file rewrite, and no `runner.run` call.

Migration: write a static role manifest with non-default reserve/override values, load it with `allow_legacy_routing=True`, configure it, and assert the backup contains `roles` while the new file contains compact providers/primary and preserves the old reserve/override values.

Preflight failure: malformed recommendation/model/config output, no supported provider, stale recommendations, or an unavailable managed role must leave the manifest bytes, backup count, and OMP calls unchanged.

OMP write failure: simulate one `runner.run` failure after manifest persistence; assert return `1`, compact intent remains saved, and one backup exists.

- [ ] **Step 3: Write failing parser and dispatch tests**

```python
args = DOTAI.build_parser().parse_args([
    "configure", "omp-routing", "--primary", "anthropic", "--dry-run"
])
self.assertEqual(
    (args.configure_target, args.primary, args.dry_run),
    ("omp-routing", "anthropic", True),
)
```

Invoke `main()` against a temporary legacy manifest while mocking `configure_omp_routing`; assert it receives `(manifest, path, runner, requested_primary)` and that `validate`, `status`, `install`, `update`, and `sync` reject the same legacy file with the migration instruction. Keep the existing proof that install/update/sync never call `available_omp_models()`.

- [ ] **Step 4: Run configure lifecycle tests and verify RED**

```sh
rtk python3 -m unittest \
  tests.test_dotai.DotAiTests.test_configure_omp_routing_persists_compact_intent_and_preserves_omp_values \
  tests.test_dotai.DotAiTests.test_configure_omp_routing_dry_run_changes_nothing \
  tests.test_dotai.DotAiTests.test_configure_omp_routing_is_idempotent \
  tests.test_dotai.DotAiTests.test_configure_omp_routing_migrates_static_roles \
  tests.test_dotai.DotAiTests.test_configure_omp_routing_parser_and_lifecycle_are_explicit -v
```

Expected: FAIL because the configurator does not accept a manifest path/primary or persist compact intent.

- [ ] **Step 5: Implement compact intent construction**

`build_omp_routing_intent()` returns exact compact keys. Use existing values for `agentModelOverrides`, reserve percentage/policy, and revert policy; for null routing use catalog overrides and current defaults. A legacy routing dict contributes only these policy/override values—never its model roles.

Sort `providers` before persistence and set `primaryProvider` to the selected provider. Reuse `omp_routing_scalar_values()` unchanged against the compact result.

- [ ] **Step 6: Refactor configure into a preflight-then-write sequence**

The configurator must:

1. load recommendations;
2. read available OMP selectors;
3. detect providers;
4. choose primary from requested/persisted/input;
5. resolve all role chains and reject any unavailable role;
6. read and validate every required OMP record/scalar;
7. build compact intent, a shallow copied manifest with that section, merged OMP records, and differing writes;
8. print the approved preview;
9. return without mutation in dry-run;
10. when intent changed, call `backup_manifest(path)` and `write_manifest(path, updated)` before any OMP write;
11. execute only differing OMP writes and return failure if `runner.failures` grew.

Do not return early merely because `ompRouting` is null; null is the expected first-run state. Catch file/catalog/input errors in `main()` alongside existing `DotAiError` handling so the CLI exits `2` with one actionable message. Runtime OMP read/write failures retain exit `1` and status badges.

- [ ] **Step 7: Add CLI flag and configure-only legacy load**

Add:

```python
configure_routing.add_argument(
    "--primary",
    choices=["anthropic", "openai-codex"],
    help="Interactive primary when both Anthropic and Codex are authenticated",
)
```

Before normal manifest loading, derive:

```python
allow_legacy_routing = (
    args.command == "configure" and args.configure_target == "omp-routing"
)
```

Pass that flag to `load_manifest`. Dispatch with the selected path and primary. No other command gets legacy permission or routing discovery.

- [ ] **Step 8: Run configure lifecycle tests and verify GREEN**

Run the Step 4 command plus all tests whose names contain `configure_omp_routing`, `load_manifest`, and `runner_formats`. Expected: PASS with no unexpected warnings.

- [ ] **Step 9: Commit the configure lifecycle**

```sh
rtk git add dotai.py tests/test_dotai.py
rtk git commit -m "feat: configure routing for authenticated providers"
```

---

### Task 4: Derive status and document the workflow

**Files:**
- Modify: `dotai.py:872-906,953-956`
- Modify: `tests/test_dotai.py:550-680`
- Modify: `README.md:158-177`

**Interfaces:**
- `omp_routing_status(manifest: dict[str, Any], runner: Runner) -> tuple[str, str]` consumes compact routing plus the tracked recommendations and never prompts or writes.
- `print_status()` continues to render routing only for configured truthy intent.

- [ ] **Step 1: Write failing compact status tests**

Build a compact Anthropic-plus-Copilot manifest and matching catalog/config values. Assert `("OK", "configured roles match")` and no `runner.run` calls.

Add one observable case per label:

- `DRIFT, "authenticated providers changed; run 'dotai configure omp-routing'"` when a new supported provider appears or a persisted one disappears while another still resolves.
- `DRIFT, "model role differs: default"` and equivalent fallback/override/scalar mismatches when provider selection is stable.
- `INACTIVE, "configured providers are unavailable"` when no persisted provider has any available selector.
- `FAIL` for malformed recommendations, model catalog, or required OMP config.
- `OK, "not configured in manifest"` for absent/null routing, with no recommendation or OMP calls.

Assert `print_status()` uses yellow `DRIFT`/`INACTIVE`, red `FAIL`, green `OK`, and returns false for every non-OK configured state.

- [ ] **Step 2: Run status tests and verify RED**

```sh
rtk python3 -m unittest \
  tests.test_dotai.DotAiTests.test_omp_routing_status_reports_ok_for_compact_intent \
  tests.test_dotai.DotAiTests.test_omp_routing_status_reports_provider_selection_drift \
  tests.test_dotai.DotAiTests.test_omp_routing_status_reports_inactive_and_fail \
  tests.test_dotai.DotAiTests.test_print_status_renders_routing_only_when_configured_and_uses_its_health -v
```

Expected: FAIL because status still resolves static role lists.

- [ ] **Step 3: Refactor status through shared pure helpers**

For configured intent:

1. load recommendations and available selectors;
2. derive currently detected supported providers;
3. return INACTIVE if none of the persisted providers is available;
4. compare detected provider set to persisted provider set and return provider-selection DRIFT before reading OMP values;
5. resolve roles using persisted `primaryProvider` without calling `choose_primary_provider()` (status must never prompt);
6. require every managed role;
7. read and compare current records/scalars exactly as configure does.

Translate recommendation/model/config parsing failures to FAIL details. Keep the current unmanaged-entry semantics: only managed role and override keys participate in equality checks.

- [ ] **Step 4: Update README with the exact user workflow**

Replace the current fixed Codex/Copilot paragraph. Document:

1. run install;
2. authenticate Copilot, Codex, Anthropic, or any combination inside OMP;
3. preview `./dotai configure omp-routing --dry-run`;
4. when both Anthropic and Codex exist, choose interactively or pass `--primary anthropic|openai-codex`;
5. apply `./dotai configure omp-routing`;
6. explain premium interactive precedence and Copilot/Anthropic/Codex worker ordering;
7. state that only compact provider intent is stored, credentials are never read, and unrelated OMP settings remain;
8. tell users with static `ompRouting.roles` to run configure for the backed-up one-way migration;
9. explain that provider availability changes produce status drift and rerunning configure refreshes intent.

- [ ] **Step 5: Run status and documentation-adjacent tests and verify GREEN**

Run the Step 2 command plus the color/status tests and repository example test. Expected: PASS.

- [ ] **Step 6: Commit status and documentation**

```sh
rtk git add dotai.py tests/test_dotai.py README.md
rtk git commit -m "docs: explain provider-aware routing setup"
```

---

### Task 5: Verify the complete provider-aware workflow

**Files:**
- Verify: `routing-recommendations.json`
- Verify: `stack.schema.json`
- Verify: `stack.example.json`
- Verify: `dotai.py`
- Verify: `tests/test_dotai.py`
- Verify: `README.md`

**Interfaces:**
- Consumes the completed `configure omp-routing` and status behavior.
- Produces repository-level evidence without applying routing to the user's live OMP configuration.

- [ ] **Step 1: Run the complete focused behavior suite**

```sh
rtk python3 -m unittest discover -s tests -v
```

Expected: every test passes, including all provider combinations, migration, dry-run, idempotence, status, and command-isolation contracts.

- [ ] **Step 2: Validate a freshly initialized local manifest**

Remove only the ignored worktree-local `stack.json` if a prior verification created it, then run:

```sh
rtk python3 dotai.py validate
```

Expected: DotAi initializes from `stack.example.json`, prints a valid-manifest result, and the generated file contains `"ompRouting": null`.

- [ ] **Step 3: Record the manifest checksum before the smoke test**

```sh
rtk sha256sum stack.json
```

Retain the checksum as the dry-run mutation baseline.

- [ ] **Step 4: Exercise the real installed OMP catalog without writes**

```sh
rtk python3 dotai.py configure omp-routing --dry-run --primary openai-codex
```

Expected on the current Copilot-plus-Codex workstation: discovered providers include `github-copilot` and `openai-codex`; Codex leads `default`/`slow`; Copilot leads `task`/`smol`; output previews compact manifest intent and differing OMP settings only.

If Anthropic is also authenticated, the explicit flag resolves the otherwise ambiguous premium choice without prompting. Do not run the command without `--dry-run` during verification.

- [ ] **Step 5: Prove the smoke test did not change the manifest**

```sh
rtk sha256sum stack.json
```

Expected: identical checksum to Step 3. The test suite's mocked runner assertions provide the corresponding proof that dry-run never invokes `omp config set`.

- [ ] **Step 6: Inspect language diagnostics when available**

Request workspace diagnostics for `dotai.py` and `tests/test_dotai.py` through the configured language server. Expected: no new errors. If no Python language server is configured, record that fact and rely on the executed suite and smoke test.

- [ ] **Step 7: Commit any verification-only corrections**

If verification required source/test/doc corrections, repeat Steps 1-6 and commit only those corrections:

```sh
rtk git add routing-recommendations.json stack.schema.json stack.example.json dotai.py tests/test_dotai.py README.md docs/superpowers/plans/2026-08-31-provider-aware-omp-routing.md
rtk git commit -m "fix: complete provider-aware routing verification"
```

When no corrections were needed, do not create an empty commit.
