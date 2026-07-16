# DotAi repository guidance

## Purpose

DotAi is a declarative, cross-platform manager for a personal AI development stack. `stack.json` is the source of truth for tools, skills, OMP marketplaces/plugins, and MCP servers.

Supported platforms:

- Native Windows, using Scoop
- Windows Subsystem for Linux with an Ubuntu-based distribution
- Ubuntu Linux
- Arch Linux
- macOS

## Repository map

- `dotai.py` — manifest validation, platform detection, reconciliation, status, doctor, and extension commands
- `stack.json` — managed stack definition
- `stack.schema.json` — JSON Schema for the manifest
- `bootstrap.sh` / `bootstrap.ps1` — first-run installers
- `dotai` / `dotai.ps1` — operational launchers
- `tests/test_dotai.py` — behavioral tests

## Development rules

- Keep the runtime compatible with Python 3.10 or newer and use the standard library unless a dependency is demonstrably necessary.
- Treat `stack.json` as declarative data. Do not hard-code stack-specific tools or integrations into `dotai.py` when the manifest can express them.
- Keep `stack.schema.json`, manifest validation, CLI mutation commands, and `stack.json` aligned when changing the manifest format.
- Preserve unmanaged user configuration. MCP reconciliation must retain unrelated servers and top-level values, create a timestamped backup before changing an existing file, and be idempotent.
- MCP status must use semantic endpoint/configuration matching across OMP-discovered provider files. Do not require an exact server alias, and allow provider-specific fields such as authentication headers.
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

Validate the repository manifest:

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
