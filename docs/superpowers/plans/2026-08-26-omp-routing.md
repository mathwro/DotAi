# OMP Multi-Provider Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, manifest-driven DotAi command that discovers authenticated OMP models and configures safe role-based multi-provider fallback routing.

**Architecture:** `ompRouting` remains declarative data in the stack manifest. New pure helpers validate routing data, parse `omp models --json`, and resolve each role’s first available selector plus its ordered fallback chain. The explicit `configure omp-routing` command alone performs provider discovery and writes managed OMP settings; status observes the same desired state without mutating it.

**Tech Stack:** Python 3.10 standard library, `argparse`, `json`, `unittest`, Oh My Pi CLI.

**Spec:** `docs/superpowers/specs/2026-08-26-omp-routing-design.md`

## Global Constraints

- Keep runtime compatibility with Python 3.10+ and use only the standard library.
- Keep manifests declarative; do not hard-code provider setup behavior outside manifest data.
- Never store, read, or print provider credentials, account data, API keys, or OMP’s credential database.
- `install`, `update`, and `sync` must not run `omp models` or write OMP routing settings.
- Preserve unmanaged OMP record entries and skip idempotent `omp config set` writes.
- Dry runs may discover models and read current OMP config, but must not write machine state.
- Prefix every shell command with `rtk`.
- Run `rtk python3 -m unittest discover -s tests -v` and `rtk python3 dotai.py validate` before declaring work complete.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `stack.schema.json` | Define the optional `ompRouting` manifest object and its constrained nested fields. |
| `stack.example.json` | Supply a Codex-primary, Copilot-worker routing policy for newly initialized manifests. |
| `dotai.py` | Validate routing, discover OMP models, resolve desired settings, reconcile/read status, and expose the new explicit CLI command. |
| `tests/test_dotai.py` | Behavioral tests for parsing, resolution, merging, dry-run/idempotence, status, and install/sync/update isolation. |
| `README.md` | Document post-authentication provider routing and the explicit setup lifecycle. |

### Task 1: Define and validate the routing manifest

**Files:**
- Modify: `stack.schema.json:8-63,126-167`
- Modify: `stack.example.json:124-141`
- Modify: `dotai.py:207-233`
- Test: `tests/test_dotai.py:22-38` and new tests adjacent to manifest-validation tests

**Interfaces:**
- Produces `validate_omp_routing(value: Any) -> dict[str, Any]`, called only by `load_manifest()` when `ompRouting` is present.
- Produces a validated optional `manifest["ompRouting"]` containing `roles`, `agentModelOverrides`, `usageReservePct`, `usageReservePolicy`, and `fallbackRevertPolicy`.
- Later tasks consume the validated section; no later task reparses raw manifest shapes.

- [ ] **Step 1: Add manifest-validation tests before implementation**

Add a compact routing fixture to `DotAiTests` and assert `load_manifest()` rejects each invalid case while accepting the valid policy:

```python
def routing_manifest(self) -> dict:
    manifest = self.minimal_manifest("~/.omp/agent/mcp.json")
    manifest["ompRouting"] = {
        "roles": {"default": ["openai-codex/gpt-5.6-sol"]},
        "agentModelOverrides": {"task": "@task"},
        "usageReservePct": 10,
        "usageReservePolicy": "auto",
        "fallbackRevertPolicy": "cooldown-expiry",
    }
    return manifest

def test_manifest_rejects_invalid_omp_routing(self) -> None:
    cases = [
        {"roles": {}},
        {"roles": {"default": []}},
        {"roles": {"default": [""]}},
        {"roles": {"default": ["openai/model"]}, "usageReservePct": 101},
        {"roles": {"default": ["openai/model"]}, "usageReservePolicy": "later"},
        {"roles": {"default": ["openai/model"]}, "fallbackRevertPolicy": "always"},
    ]
    # Write each case to a temporary manifest and assert DotAiError.
```

Also load `stack.example.json` and assert its `ompRouting.roles["default"][0]` is `openai-codex/gpt-5.6-sol`.

- [ ] **Step 2: Run the focused test to verify failure**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_manifest_rejects_invalid_omp_routing -v
```

Expected: FAIL because the validator and example routing section do not exist.

- [ ] **Step 3: Extend the JSON Schema and example manifest**

Add `ompRouting` to top-level `properties` as an optional reference. Add these definitions:

```json
"ompRouting": {
  "type": "object",
  "required": ["roles"],
  "additionalProperties": false,
  "properties": {
    "roles": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "minLength": 1}
      }
    },
    "agentModelOverrides": {
      "type": "object",
      "additionalProperties": {"type": "string", "minLength": 1},
      "default": {}
    },
    "usageReservePct": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
    "usageReservePolicy": {"enum": ["confirm", "auto", "fail-closed"], "default": "auto"},
    "fallbackRevertPolicy": {"enum": ["cooldown-expiry", "never"], "default": "cooldown-expiry"}
  }
}
```

Insert the default policy from the approved spec after `ompExtensions` in `stack.example.json`. It must use these selectors and order:

```json
"default": ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-terra"],
"task": ["github-copilot/gpt-5.6-terra", "github-copilot/gpt-5.6-luna", "openai-codex/gpt-5.6-sol"],
"smol": ["github-copilot/gpt-5.6-luna", "github-copilot/gpt-5.6-terra", "openai-codex/gpt-5.4-mini"],
"slow": ["openai-codex/gpt-5.6-sol:high", "github-copilot/gpt-5.6-terra:high", "github-copilot/gpt-5.6-luna:high"]
```

- [ ] **Step 4: Implement application-level validation**

Add `validate_omp_routing()` immediately before `load_manifest()`. It must return `{}` for `None`, otherwise raise `DotAiError` for non-object values; empty/non-string role names; empty/non-list role candidates; empty/non-string candidate selectors; non-object or non-string `agentModelOverrides`; non-integer/bool/out-of-range `usageReservePct`; or unsupported policy strings.

Apply defaults in its returned value:

```python
return {
    "roles": roles,
    "agentModelOverrides": overrides,
    "usageReservePct": value.get("usageReservePct", 10),
    "usageReservePolicy": value.get("usageReservePolicy", "auto"),
    "fallbackRevertPolicy": value.get("fallbackRevertPolicy", "cooldown-expiry"),
}
```

In `load_manifest()`, call the helper only when the optional key exists and replace that entry with its normalized result. Preserve current behavior exactly when it does not exist.

- [ ] **Step 5: Run focused validation tests**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_manifest_rejects_invalid_omp_routing tests.test_dotai.DotAiTests.test_default_omp_update_uses_omp_updater_and_marks_dependencies -v
```

Expected: PASS. The existing package test confirms adding the example section did not change OMP package/update configuration.

- [ ] **Step 6: Commit the manifest contract**

```sh
rtk git add stack.schema.json stack.example.json dotai.py tests/test_dotai.py
rtk git commit -m "feat: define OMP routing manifest"
```

### Task 2: Parse OMP’s model catalog and resolve routing candidates

**Files:**
- Modify: `dotai.py:435-481`
- Test: `tests/test_dotai.py` adjacent to `test_omp_extension_reconciliation_preserves_existing_entries`

**Interfaces:**
- Produces `available_omp_models(runner: Runner) -> set[str] | None`, which runs `omp models --json` and returns catalog selectors.
- Produces `selector_identity(selector: str) -> str`, which removes one recognized thinking suffix (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `auto`) for catalog matching.
- Produces `resolve_omp_routing(routing: dict[str, Any], available: set[str]) -> tuple[dict[str, str], dict[str, list[str]], list[str]]`.
- Later tasks consume its `(primaries, fallback_chains, unavailable_roles)` result.

- [ ] **Step 1: Write failing parser and resolver tests**

Use a mocked `Runner.output()` response:

```python
catalog = json.dumps({"models": [
    {"selector": "openai-codex/gpt-5.6-sol"},
    {"selector": "github-copilot/gpt-5.6-terra"},
    {"selector": "github-copilot/gpt-5.6-luna"},
]})
```

Assert:

```python
available = DOTAI.available_omp_models(runner)
self.assertEqual(available, {
    "openai-codex/gpt-5.6-sol",
    "github-copilot/gpt-5.6-terra",
    "github-copilot/gpt-5.6-luna",
})
primaries, chains, unavailable = DOTAI.resolve_omp_routing(routing, available)
self.assertEqual(primaries["default"], "openai-codex/gpt-5.6-sol")
self.assertEqual(primaries["slow"], "openai-codex/gpt-5.6-sol:high")
self.assertEqual(chains["default"], ["openai-codex/gpt-5.6-sol", "github-copilot/gpt-5.6-terra"])
self.assertEqual(unavailable, ["unavailable-role"])
```

Cover malformed model output (`"models"` missing, not a list, non-string selectors) returning `None`, and a candidate list with repeated selectors resolving each selector only once while retaining order.

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_omp_model_catalog_and_routing_resolution -v
```

Expected: FAIL because the discovery and resolution helpers do not exist.

- [ ] **Step 3: Implement catalog parsing and selector identity**

Add the following helpers near the existing OMP extension helpers:

```python
THINKING_SUFFIXES = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "auto"})

def selector_identity(selector: str) -> str:
    base, separator, suffix = selector.rpartition(":")
    return base if separator and suffix in THINKING_SUFFIXES else selector

def available_omp_models(runner: Runner) -> set[str] | None:
    raw = runner.output(["omp", "models", "--json"])
    try:
        models = json.loads(raw)["models"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(models, list):
        return None
    selectors = {item.get("selector") for item in models if isinstance(item, dict)}
    return selectors if all(isinstance(selector, str) and selector for selector in selectors) else None
```

Keep a malformed list member or a missing/invalid selector a parse failure rather than silently treating partial output as authentication state.

- [ ] **Step 4: Implement deterministic role resolution**

Implement `resolve_omp_routing()` using `selector_identity()` for availability checks. For every role, scan its configured candidate list in manifest order, retain available candidates once in first-seen order, set the first retained selector as the primary, and make the complete retained list the fallback chain. Append the role to `unavailable_roles` only when no candidates are retained.

Do not write settings here. This helper must have no side effects and must preserve thinking suffixes in its returned role values/chains.

- [ ] **Step 5: Run focused tests**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_omp_model_catalog_and_routing_resolution -v
```

Expected: PASS.

- [ ] **Step 6: Commit catalog discovery and resolution**

```sh
rtk git add dotai.py tests/test_dotai.py
rtk git commit -m "feat: resolve available OMP routing models"
```

### Task 3: Add explicit routing reconciliation and CLI command

**Files:**
- Modify: `dotai.py:245-340,789-815,987-1142`
- Test: `tests/test_dotai.py` adjacent to existing OMP extension reconciliation tests and command-dispatch tests

**Interfaces:**
- Consumes `manifest["ompRouting"]`, `available_omp_models()`, and `resolve_omp_routing()`.
- Produces `configured_omp_value(runner: Runner, key: str) -> Any | None` for parsing one `omp config get <key> --json` result.
- Produces `configure_omp_routing(manifest: dict[str, Any], runner: Runner) -> int` for direct unit invocation and CLI dispatch.
- Exposes `dotai configure omp-routing [--dry-run]`.

- [ ] **Step 1: Write failing reconciliation and CLI tests**

Mock an available catalog plus existing records:

```python
existing = {
    "modelRoles": {"custom": "local/keep", "default": "old/model"},
    "retry.fallbackChains": {"custom": ["local/keep"], "default": ["old/model"]},
    "task.agentModelOverrides": {"reviewer": "@review"},
}
```

Assert the planned `omp config set` payloads retain `custom` and `reviewer`, while replacing only `default`, `task`, `smol`, `slow`, `sonic`, and `task` entries owned by the manifest. Assert scalar commands set exactly:

```python
["omp", "config", "set", "retry.modelFallback", "true"]
["omp", "config", "set", "retry.usageAwareFallback", "true"]
["omp", "config", "set", "retry.usageReservePct", "10"]
["omp", "config", "set", "retry.usageReservePolicy", "auto"]
["omp", "config", "set", "retry.fallbackRevertPolicy", "cooldown-expiry"]
```

Also cover:

- `--dry-run` emits the primary/chains and calls no `runner.run()`.
- Exact existing records/scalars cause no write.
- unreadable catalog exits `1` without writes;
- no resolved roles exits `1` without writes and names skipped roles;
- the command parser accepts `configure omp-routing --dry-run`.

- [ ] **Step 2: Run the focused test to verify failure**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_configure_omp_routing_merges_settings_and_is_idempotent -v
```

Expected: FAIL because the generic config getter, reconciler, and CLI subcommand do not exist.

- [ ] **Step 3: Implement safe OMP config readers and merging**

Add:

```python
def configured_omp_value(runner: Runner, key: str) -> Any | None:
    raw = runner.output(["omp", "config", "get", key, "--json"])
    try:
        return json.loads(raw)["value"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
```

For record keys, require an object whose keys are strings. Build copies of existing records and update only keys present in the manifest’s `roles` or `agentModelOverrides`. Never delete other keys. Compare desired and current dictionaries directly before calling `runner.run()`.

- [ ] **Step 4: Implement `configure_omp_routing()`**

If `ompRouting` is absent, print `[INACTIVE] OMP routing: not configured in manifest` and return `0`. If catalog parsing fails, print `[FAIL] OMP routing: unable to read OMP model catalog` and return `1`. If every role is unavailable, print `[INACTIVE] OMP routing: no configured model candidates are available` plus the role names and return `1`.

For at least one resolved role:

1. Print the discovered provider names derived from retained selectors, resolved primaries, fallback chains, and unavailable roles.
2. Read all current record and scalar settings before deciding writes; unreadable records/scalars are `FAIL` and cause return `1` before any write.
3. Create desired merged records and desired scalar strings.
4. In dry-run, print one `[RUN]` line per differing setting and return `0` without `runner.run()`.
5. Otherwise call `runner.run()` only for differences, using compact JSON for records. Return `1` if `runner.failures` is non-empty, else `0`.

- [ ] **Step 5: Add CLI parser and dispatch**

Under `build_parser()` add a `configure` parser and nested required subparser:

```python
configure = sub.add_parser("configure", help="Configure explicit post-authentication integrations")
configure_sub = configure.add_subparsers(dest="configure_target", required=True)
configure_routing = configure_sub.add_parser("omp-routing", help="Configure OMP multi-provider model routing")
configure_routing.add_argument("--dry-run", action="store_true")
```

In `main()`, construct `Runner` normally, then dispatch `configure_target == "omp-routing"` to `configure_omp_routing()` before `fix`, `status`, `sync`, `update`, or `reconcile` branches. Do not add this function to `reconcile()`.

- [ ] **Step 6: Prove install/update/sync remain authentication-independent**

Add a test that patches `DOTAI.available_omp_models` to raise `AssertionError("routing must not run")`, invokes `install --dry-run`, `update --dry-run`, and `sync --dry-run` with a manifest containing `ompRouting`, and asserts each command completes its existing mocked flow without triggering that assertion.

- [ ] **Step 7: Run focused reconciliation and isolation tests**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_configure_omp_routing_merges_settings_and_is_idempotent tests.test_dotai.DotAiTests.test_configure_omp_routing_dry_run_and_unavailable_models tests.test_dotai.DotAiTests.test_install_update_and_sync_do_not_configure_omp_routing -v
```

Expected: PASS.

- [ ] **Step 8: Commit explicit routing configuration**

```sh
rtk git add dotai.py tests/test_dotai.py
rtk git commit -m "feat: configure OMP provider routing"
```

### Task 4: Report routing state and document the post-authentication workflow

**Files:**
- Modify: `dotai.py:675-743`
- Modify: `README.md:145-183`
- Test: `tests/test_dotai.py` adjacent to `test_status_color_can_be_forced_or_disabled`

**Interfaces:**
- Produces `omp_routing_status(manifest: dict[str, Any], runner: Runner) -> tuple[str, str]` where label is exactly `OK`, `DRIFT`, `INACTIVE`, or `FAIL`.
- `print_status()` consumes that tuple and includes its health in the existing return value.

- [ ] **Step 1: Write failing status tests**

Create four mocked scenarios for a routing-enabled manifest:

```python
self.assertEqual(DOTAI.omp_routing_status(manifest, runner)[0], "OK")
self.assertEqual(DOTAI.omp_routing_status(manifest, runner)[0], "DRIFT")
self.assertEqual(DOTAI.omp_routing_status(manifest, runner)[0], "INACTIVE")
self.assertEqual(DOTAI.omp_routing_status(manifest, runner)[0], "FAIL")
```

Use: matching catalog/settings for `OK`; an available catalog plus one changed managed chain/scalar for `DRIFT`; an empty parsed catalog for `INACTIVE`; and malformed catalog/config output for `FAIL`. Assert `print_status()` renders the exact corresponding badge and returns false for every non-`OK` case.

- [ ] **Step 2: Run the focused status test to verify failure**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_omp_routing_status_labels -v
```

Expected: FAIL because routing status does not exist.

- [ ] **Step 3: Implement observational status**

Implement `omp_routing_status()` without calling `runner.run()`:

1. Return `OK, "not configured in manifest"` when `ompRouting` is absent, so existing manifests remain healthy.
2. Return `FAIL` when the model catalog or any required OMP config response cannot be parsed.
3. Return `INACTIVE` when a readable catalog has no available candidate for any configured role.
4. Resolve available candidates, derive expected merged managed entries and scalar values, then return `DRIFT` when any resolved managed entry differs, any configured role has no available candidate, or any configured agent override differs.
5. Return `OK` only when every configured role resolves and all managed entries/scalars match.

Update `print_status()` to render an `OMP routing:` section when the manifest contains `ompRouting`, set `healthy` false for any label other than `OK`, and retain the current MCP/extension behavior unchanged.

- [ ] **Step 4: Document the safe lifecycle**

Add a `### Configure OMP provider routing` section under README configuration safety. It must state:

1. `dotai install` does not authenticate providers or configure model routing.
2. Authenticate desired providers inside OMP first.
3. Preview: `./dotai configure omp-routing --dry-run`.
4. Apply: `./dotai configure omp-routing`.
5. The default policy keeps Codex as the main/slow preference, uses Copilot for task/smol workers, enables usage-aware fallback, and preserves unmanaged OMP settings.
6. `dotai status` reports `OK`, `DRIFT`, `INACTIVE`, or `FAIL` without modifying OMP.

Never tell users to put credentials in `stack.json`.

- [ ] **Step 5: Run focused status and documentation-adjacent behavior tests**

Run:

```sh
rtk python3 -m unittest tests.test_dotai.DotAiTests.test_omp_routing_status_labels tests.test_dotai.DotAiTests.test_status_color_can_be_forced_or_disabled -v
```

Expected: PASS.

- [ ] **Step 6: Commit status and documentation**

```sh
rtk git add dotai.py tests/test_dotai.py README.md
rtk git commit -m "docs: explain OMP provider routing"
```

### Task 5: Verify the repository and configure the current machine

**Files:**
- Verify: `dotai.py`, `stack.schema.json`, `stack.example.json`, `tests/test_dotai.py`, `README.md`
- Machine state changed only by: `~/.omp/agent/config.yml` through the explicit DotAi command

**Interfaces:**
- Consumes the completed `dotai configure omp-routing` command.
- Produces verified global OMP settings for this authenticated Codex + Copilot machine.

- [ ] **Step 1: Run the full focused behavior suite**

Run:

```sh
rtk python3 -m unittest discover -s tests -v
```

Expected: PASS with all prior and new behavior tests.

- [ ] **Step 2: Validate the local manifest**

Run:

```sh
rtk python3 dotai.py validate
```

Expected: `[OK] Valid manifest: ...`.

- [ ] **Step 3: Exercise the new command without writes**

Run:

```sh
rtk python3 dotai.py configure omp-routing --dry-run
```

Expected: current `openai-codex` and `github-copilot` candidates are discovered; Codex is `default`/`slow` primary; Copilot is `task`/`smol` primary; every write is previewed only.

- [ ] **Step 4: Apply the routing configuration**

Run:

```sh
rtk python3 dotai.py configure omp-routing
```

Expected: only managed OMP role/fallback/agent-override/retry settings are written; unrelated OMP entries remain intact.

- [ ] **Step 5: Read back and verify the configured OMP records**

Run:

```sh
rtk omp config get modelRoles --json
rtk omp config get retry.fallbackChains --json
rtk omp config get task.agentModelOverrides --json
rtk omp config get retry.usageAwareFallback --json
rtk omp config get retry.usageReservePct --json
rtk omp config get retry.usageReservePolicy --json
rtk omp config get retry.fallbackRevertPolicy --json
rtk python3 dotai.py status
```

Expected: Codex is primary for `default` and `slow`; Copilot is primary for `task` and `smol`; each role has the ordered cross-provider chain; usage-aware fallback is true at 10% with `auto`; revert policy is `cooldown-expiry`; DotAi reports routing `OK`.

- [ ] **Step 6: Commit verified implementation**

```sh
rtk git add dotai.py stack.schema.json stack.example.json tests/test_dotai.py README.md docs/superpowers/specs/2026-08-26-omp-routing-design.md docs/superpowers/plans/2026-08-26-omp-routing.md
rtk git commit -m "feat: add OMP multi-provider routing"
```
