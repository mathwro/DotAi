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

- `dotai.py` — manifest validation, platform detection, reconciliation, status, doctor, and extension commands
- `stack.example.json` — tracked baseline stack definition copied for new users
- `stack.json` — ignored, user-owned stack configuration generated on first use
- `stack.schema.json` — JSON Schema for both manifests
- `bootstrap.sh` / `bootstrap.ps1` — first-run installers
- `dotai` / `dotai.ps1` — operational launchers
- `tests/test_dotai.py` — behavioral tests

## Development rules

- Keep the runtime compatible with Python 3.10 or newer and use the standard library unless a dependency is demonstrably necessary.
- Treat stack manifests as declarative data. Do not hard-code stack-specific tools or integrations into `dotai.py` when a manifest can express them.
- Shared defaults belong in `stack.example.json`; never commit or overwrite the user-owned `stack.json`.
- When the default `stack.json` is absent, initialize it once from `stack.example.json`. Existing local manifests must remain untouched, and missing explicitly selected custom manifests must still fail.
- Keep `stack.schema.json`, manifest validation, CLI mutation commands, and `stack.example.json` aligned when changing the manifest format.
- `ompExtensions` contains global OMP extension paths managed by DotAi. Reconciliation must append semantically missing paths through `omp config` without replacing unrelated user extensions, and status must verify both registration and source availability.
- RTK integration requires version 0.43 or newer and must use `rtk init -g --agent pi` plus `~/.pi/agent/extensions/rtk.ts` in `ompExtensions`. Do not replace it with the Codex rules integration; Codex setup is independent and user-owned.
- Preserve unmanaged user configuration. MCP reconciliation must retain unrelated servers and top-level values, create a timestamped backup before changing an existing file, and be idempotent.
- MCP status must use semantic endpoint/configuration matching across OMP-discovered provider files. Do not require an exact server alias, and allow provider-specific fields such as authentication headers.
- `dotai add mcp` supports repeatable `--header NAME=VALUE` options for HTTP/SSE servers and repeatable `--env NAME=VALUE` options for stdio servers. Keep these transport-specific, reject duplicate or invalid names, and preserve values after the first `=`.
- Secret-bearing header and environment values must be references to environment variables or secret commands supported by OMP, never literal credentials in `stack.example.json` or tests. Use placeholders in documentation.
- Skill status is agent-scoped. A skill found only in another agent's plugin cache is not active for OMP and must be reported as `INACTIVE`, not `OK`.
- Keep status labels consistent: `OK` is green, `RUN` is cyan, `INACTIVE` and `DRIFT` are yellow, and `MISSING` and `FAIL` are red. Respect `--color auto|always|never`, `NO_COLOR`, and `FORCE_COLOR`.
- Windows package operations must use Scoop. Do not introduce Winget commands.
- Installation, update, and synchronization must remain safe to rerun. Dry runs must not modify files or machine state.
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

For CLI behavior changes, exercise the affected command directly. Useful smoke checks:

```sh
rtk python3 dotai.py status
rtk python3 dotai.py doctor
rtk python3 dotai.py sync --dry-run
rtk python3 dotai.py --platform windows install --force --dry-run
```

A nonzero `status` or `doctor` result is expected when a declared component is genuinely missing or inactive; do not weaken health semantics merely to make these commands exit zero.
