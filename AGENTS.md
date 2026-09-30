# DotAi repository guidance

## Purpose

DotAi is a declarative, cross-platform manager for a personal AI development stack. `stack.example.json` is the tracked baseline; each user owns an ignored `stack.json` generated from that example on first use.

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
- `stack.json` — ignored, user-owned stack configuration generated on first use
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
- When the default `stack.json` is absent, initialize it once from `stack.example.json`. Existing local manifests must remain untouched, and missing explicitly selected custom manifests must still fail.
- Keep `stack.schema.json`, manifest validation, CLI mutation commands, and `stack.example.json` aligned when changing the manifest format.
- Reuse shared in-memory validation for loading, initialization, and every manifest write. Reject invalid candidates before creating directories, temporary files, or backups; persist unconfigured routing as `null`.
- Packages marked with `updateGroup: "dependency"` must install when missing, but normal updates skip present binaries unless `--include-dependencies` is supplied, even below `minimumVersion` or with unparseable versions. Keep Node.js and `uv` in this group; skipped unhealthy dependencies remain unhealthy.
- OMP updates must use its version-aware `omp update` command instead of rerunning the installer.
- `ompExtensions` contains global OMP extension paths managed by DotAi. Reconciliation must append semantically missing paths through `omp config` without replacing unrelated user extensions, and status must verify both registration and source availability.
- RTK integration requires version 0.43 or newer and must use `rtk init -g --agent pi` plus `~/.pi/agent/extensions/rtk.ts` in `ompExtensions`. Do not replace it with the Codex rules integration; Codex setup is independent and user-owned.
- Linux RTK installs/updates use pinned release archives with per-architecture SHA-256 verification before extraction or installation. Update the pinned version and digests together; existing user manifests adopt these changes explicitly.
- Keep routing optional and post-authentication: install, open OMP for the first time, authenticate a supported provider, preview `configure omp-routing --dry-run`, then explicitly apply it. Authentication stays in OMP; `install`, `update`, and `sync` do not discover providers or write routing. Unconfigured routing produces a non-failing `INACTIVE` hint.
- Keep provider/model policy in `routing-recommendations.json`, resolve only available models, and preserve unmanaged OMP records. Legacy static `ompRouting.roles` is accepted only by the explicit configure migration; behavioral fixtures use synthetic catalogs rather than current model IDs.
- Preserve unmanaged user configuration. MCP reconciliation retains unrelated servers and top-level values, creates a timestamped backup before changing an existing file, and is idempotent. Create backups exclusively with mode `0600` on POSIX.
- Match MCP health semantically across OMP-discovered provider files, independent of aliases and extra authentication fields. Equivalent requirements can share one healthy entry. Retain file/section provenance, write only the target `mcpServers` container, distinguish stdio working directories, and report disabled or ambiguous provider conflicts without overwriting user entries.
- `dotai add mcp` supports repeatable `--header NAME=VALUE` options for HTTP/SSE servers and repeatable `--env NAME=VALUE` options for stdio servers. Keep these transport-specific, reject duplicate or invalid names, and preserve values after the first `=`.
- Secret-bearing header and environment values must be references to environment variables or secret commands supported by OMP, never literal credentials in `stack.example.json` or tests. Use placeholders in documentation.
- Skill status is agent-scoped. A skill found only in another agent's plugin cache is not active for OMP and must be reported as `INACTIVE`, not `OK`.
- Named `add skill --skill` selections infer installer-normalized check directories; explicit `--check-skill` values stay authoritative. Missing or wildcard checks report `UNVERIFIED`, not guessed installation health.
- Skip routine skill refreshes only when v3 lock source metadata and the configured agent's full GitHub folder tree hash prove ownership. Read the authoritative XDG lock when set; global name-only ownership cannot prove an independent agent copy. Unknown provenance refreshes conservatively; `sync --update-skills`, `install --force`, and accepted recommendation changes refresh explicitly.
- Recommended skill reconciliation preserves locally modified entries by default. `sync --recommended-skills --enforce` may adopt and exactly reconcile only sources present in `stack.example.json`; remove retired skills from those sources while preserving unrelated user sources.
- Keep status labels consistent: `OK` is green, `RUN` is cyan, `INACTIVE`, `DRIFT`, and `UNVERIFIED` are yellow, and `MISSING` and `FAIL` are red. Respect `--color auto|always|never`, `NO_COLOR`, and `FORCE_COLOR`.
- Windows package operations must use Scoop. Do not introduce Winget commands.
- Installation, update, and synchronization must remain safe to rerun. After manifest initialization, dry runs must not modify files or machine state.
- Do not commit generated Python caches, local state, credentials, MCP secrets, or machine-specific configuration.
- Prefix shell commands with `rtk`.

## Verification

Run the focused behavior suite after changes:

```sh
rtk python3 -m unittest discover -s tests -v
```

Validate the local manifest (this initializes it from the example when absent):

```sh
rtk python3 dotai.py validate
```

Also validate the tracked baseline with `rtk python3 dotai.py --manifest stack.example.json validate`. Exercise mutation examples with an initialized, explicit temporary manifest and isolated home/state directories, never the user's `stack.json`. First-use default-manifest initialization also precedes dry-run previews.

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
