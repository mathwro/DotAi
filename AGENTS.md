# DotAi repository guidance

## Purpose

DotAi manages explicitly selected personal AI components. `stack.example.json` is an empty version-2 baseline; only explicit `init` creates the user-config-directory manifest. The repository-local `stack.json` remains user-owned and untouched.

Supported platforms:

- Native Windows, using Scoop
- Windows Subsystem for Linux with an Ubuntu-based distribution
- Ubuntu Linux
- Arch Linux
- macOS

## Repository map

- `dotai.py` — executable Python CLI entry point; use `./dotai.py` on Unix or `python dotai.py` on Windows
- `dotai_app/` — purpose-specific application modules; see `docs/development.md` for module responsibilities and dependency direction
- `stack.example.json` — tracked baseline stack definition copied for new users
- Personal `stack.json` and adjacent `stack.lock.json` — portable intent and private observed resolutions; path policy belongs to `portable.py`
- `routing-recommendations.json` — tracked provider/model policy; compact routing intent stays in `stack.json`
- `stack.schema.json` — JSON Schema for both manifests
- `tests/test_dotai.py` — behavioral tests
- `README.md` — concise installation, usage, status, and managed-stack overview
- `docs/configuration.md` — read before changing manifest validation, reconciliation safety, or OMP routing
- `docs/extending.md` — read before changing extension commands, skill ownership/refresh, or recommendation reconciliation
- `docs/development.md` — read before changing application code, tests, or the routing catalog; module ownership, dependency direction, and verification conventions
- `docs/superpowers/specs/` — historical design records, not active agent instructions or current defaults; current contracts are in the guides above

## Module boundaries

- Place application changes in the owning module identified by `docs/development.md`'s responsibility table. Extend an existing responsibility before introducing a new module.
- Keep `dotai.py` an executable entry point that delegates to `dotai_app.cli.main`; application logic belongs in `dotai_app/`, not the root script or new platform wrappers.
- Keep `cli.py` focused on argument parsing and dispatch, `integrations.py` on manifest additions, `reconcile.py` on coordinating reconciliation, and `health.py` on aggregating observational checks. Delegate domain decisions and operations to their owning modules rather than accumulating them in these entry points.
- Keep imports acyclic. Domain modules must not import `cli.py`, `reconcile.py`, or `health.py`; shared manifest, runtime, and terminal helpers must not depend on domain modules.
- Reuse behavior through explicit imports of its owning module. Keep `__init__.py` a package marker rather than a re-export facade, and patch the owning module in behavioral tests.
- Create a module only for a distinct, cohesive responsibility that does not fit an existing owner; avoid generic utility grab bags and one-function forwarding modules. Update the responsibility table when adding a module or moving ownership.

## Development rules

- Keep the runtime compatible with Python 3.10 or newer and use the standard library unless a dependency is demonstrably necessary.
- Treat stack manifests as declarative data. Do not hard-code stack-specific tools or integrations into the Python modules when a manifest can express them.
- Shared defaults belong in `stack.example.json`; never commit or overwrite the user-owned `stack.json`.
- Require explicit `init` for missing manifests. Inspection, validation, and previews remain read-only; normal commands reject version 1 before environment changes. Use reviewed, file-only `convert`, preserving an exact private source backup, for migration.
- Keep `stack.schema.json`, manifest validation, CLI mutation commands, and `stack.example.json` aligned when changing the manifest format.
- Reuse shared in-memory validation for loading, initialization, and every manifest write. Reject invalid candidates before creating directories, temporary files, or backups; persist unconfigured routing as `null`.
- Packages require explicit `managed: true`. Supporting runtimes and managers are checks-only prerequisites: check the selected enabled set before mutations and give repair guidance to the user. Retire dependency install/update/configure paths, including `--include-dependencies`.
- OMP fresh/missing installs and reviewed owned `install --force` replacements use exact release artifacts and SHA-256 digests; `packages.install_native_release` verifies the staged version before atomic replacement on Unix. Do not bootstrap the official installer. Existing latest-policy installs use guarded `omp update --stable` without plugin updates; record the actual numeric release metadata and verify its binary digest rather than claiming the preflight release is frozen. Matching exact requested or retained locked targets are unchanged; differing targets refuse native update and require reviewed owned `install --force`, never installer fallback. Retained exact locks also constrain latest-policy intent.
- `ompExtensions` contains global OMP extension paths managed by DotAi. Reconciliation must append semantically missing paths through `omp config` without replacing unrelated user extensions, and status must verify both registration and source availability.
- RTK is a standalone optional CLI. Run `rtk init -g --agent pi` only when its matching `~/.pi/agent/extensions/rtk.ts` integration is explicitly selected with OMP; Codex setup is independent and user-owned.
- Linux RTK installs/updates use pinned release archives with per-architecture SHA-256 verification before extraction or installation. Update the pinned version and digests together; existing user manifests adopt these changes explicitly.
- Keep routing optional and post-authentication: install, open OMP for the first time, authenticate a supported provider, preview `configure omp-routing --dry-run`, then explicitly apply it. Authentication stays in OMP; `install`, `update`, and `sync` do not discover providers or write routing. Unconfigured routing produces a non-failing `INACTIVE` hint.
- Keep provider/model policy in `routing-recommendations.json`, resolve only available models, and preserve unmanaged OMP records. Legacy static `ompRouting.roles` is accepted only by the explicit configure migration; behavioral fixtures use synthetic catalogs rather than current model IDs.
- Preserve unmanaged user configuration. MCP reconciliation retains unrelated servers and top-level values, creates a timestamped backup before changing an existing file, and is idempotent. Create backups exclusively with mode `0600` on POSIX.
- Match MCP health semantically across OMP-discovered provider files, independent of aliases and extra authentication fields. Equivalent requirements can share one healthy entry. Retain file/section provenance, write only the target `mcpServers` container, distinguish stdio working directories, and report disabled or ambiguous provider conflicts without overwriting user entries.
- `dotai add mcp` supports repeatable `--header NAME=VALUE` options for HTTP/SSE servers and repeatable `--env NAME=VALUE` options for stdio servers. Keep these transport-specific, reject duplicate or invalid names, and preserve values after the first `=`.
- Secret-bearing header and environment values must be references to environment variables or secret commands supported by OMP, never literal credentials in `stack.example.json` or tests. Use placeholders in documentation.
- Skill status is agent-scoped. A skill found only in another agent's plugin cache is not active for OMP and must be reported as `INACTIVE`, not `OK`.
- Named `add skill --skill` selections infer installer-normalized check directories; explicit `--check-skill` values stay authoritative. Missing or wildcard checks report `UNVERIFIED`, not guessed installation health.
- Keep normal sync on observed tool versions, immutable skill sources, and installed plugin versions. Unknown provenance and local modifications produce actionable drift, not overwrite permission. Explicit updates and accepted recommendation changes still require current source/scope ownership proof before replacement.
- Use `skill-recommendations.json` for optional recommendations. Keep cleanup consent separate from recommendation acceptance, preserve undeclared skills and independent agent copies, and make dry-run cleanup conditional. Immutable source/installer facts belong in the private stack lock; scoped lifecycle permission belongs in machine-local receipts.
- Resolve GitHub skills to immutable commits and complete selected-folder trees using an exact supported npm installer. Record successful observations only in private atomic adjacent locks, hold a concurrent-reconciliation lease through actions and recording, and leave locks unchanged on failed/preview runs. `status`, `list`, and `show` never resolve mutable source metadata.
- Windows reviewed recipes use minimal per-release Scoop files, verified digests, no dependencies/hooks, and no global Scoop or bucket updates. OMP uses standalone binaries and its guarded native updater; Graphify uses externally supplied uv and the current compatible Python with downloads disabled.
- Filter repeated `--only kind:id` selectors, including tools, before materialization and preflight. Lifecycle removal defaults to forgetting intent; `--uninstall`, enable, and disable require exact unchanged owned scope snapshots and refuse shared/redirected content. Project skill management remains deferred.
- Generated uv/Scoop operations require an unchanged complete backend snapshot, not just a binary or copied launcher. Hash dependency links without following them, including Windows junctions; ignore generated Python cache directories/bytecode but still detect other user files there. Custom installers must not inherit unrelated catalog metadata, pin commands, or backend ownership. A truly absent reviewed destination may receive a separate copy while preserving a foreign installation.
- Do not commit generated Python caches, local state, credentials, MCP secrets, or machine-specific configuration.
- Prefix shell commands with `rtk`.

## Command output

- Apply this contract to every existing or new command, including previews, prompts, progress, results, and failures. DotAi owns the user-facing output; use concise plain-language headings and component-level messages rather than JSON dumps, raw manifest diffs, or unfiltered subprocess chatter.
- Before a mutation, explain the component, intended action, reason, and resolved target or scope. Dry runs describe proposed changes and conditional actions without implying they were applied.
- Capture output from non-interactive external tools such as `npx`, installers, and package managers. Present DotAi progress and outcome messages by default; reserve sanitized diagnostic detail and exact commands for explicit `--verbose` output. Handle required interactive prompts explicitly rather than hiding them.
- Keep failures actionable: identify the failed component and operation, explain the available cause, and give a concrete next step. Preserve nonzero exit codes and health semantics; quieter output must not turn failures into success or discard the evidence needed to diagnose them.
- End multi-step mutations with one numeric action-outcome summary: changed, unchanged, skipped, planned, and failed actions. Simple manifest-only commands such as `init`, `add`, and `convert` use a clear result line instead of synthetic action counts. State partial completion and restart requirements accurately; nested domain operations must not print duplicate summaries.
- Keep credential values out of all output, including verbose diagnostics, command arguments displayed to users, and configuration previews; show names or references instead.

- Keep status labels consistent: `OK` is green, `RUN` is cyan, `INACTIVE`, `DRIFT`, and `UNVERIFIED` are yellow, and `MISSING` and `FAIL` are red. Respect `--color auto|always|never`, `NO_COLOR`, and `FORCE_COLOR`.

## Verification

Run the focused behavior suite after changes:

```sh
rtk python3 -m unittest discover -s tests -v
```

Validate an existing explicit manifest (validation does not initialize it):

```sh
rtk python3 dotai.py --manifest path/to/test-stack.json validate
```

Also validate the tracked baseline with `rtk python3 dotai.py --manifest stack.example.json validate`. Exercise mutations with an explicitly initialized temporary manifest and isolated home/config/state directories, never personal configuration. Local platform skips establish no native Windows or Linux proof.

For CLI behavior changes, exercise the affected command directly. Useful smoke checks:

```sh
rtk python3 dotai.py status
rtk python3 dotai.py doctor
rtk python3 dotai.py sync --dry-run
rtk python3 dotai.py version
rtk python3 dotai.py fix --dry-run
rtk python3 dotai.py --platform windows install --force --dry-run
rtk python3 dotai.py configure omp-routing --help
```

After installing OMP, opening it, and authenticating a supported provider, exercise routing with an existing test manifest: `rtk python3 dotai.py --manifest path/to/test-stack.json configure omp-routing --dry-run`. Do not apply routing to personal OMP configuration as a verification side effect.

A nonzero `status` or `doctor` result is expected for missing, inactive, unverified, or drifting declared components; preserve health semantics. The optional unconfigured-routing hint is informational, while configured routing whose providers are unavailable is unhealthy.
