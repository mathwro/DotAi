# DotAi

DotAi is a declarative, cross-platform manager for a personal AI development stack. It applies the same tools, skills, plugins, and MCP servers across Windows, WSL, Ubuntu, Arch Linux, and macOS while preserving configuration that DotAi does not own.

The managed stack lives in [`stack.json`](stack.json). The Python CLI installs or updates missing components, synchronizes AI integrations, reports drift, and provides commands for extending the manifest.

## Managed stack

| Type | Components |
| --- | --- |
| Harness | [Oh My Pi](https://github.com/can1357/oh-my-pi) |
| Tools | [RTK](https://github.com/rtk-ai/rtk), [Graphify](https://github.com/Graphify-Labs/graphify), Node.js, `uv`, `curl` |
| Skills | [Ponytail](https://github.com/DietrichGebert/ponytail), [Superpowers](https://github.com/obra/superpowers), installed through [skills.sh](https://skills.sh/) |
| MCP servers | [Context7](https://context7.com/), [Microsoft Learn](https://learn.microsoft.com/training/support/mcp) |

## Supported platforms

- Native Windows, using [Scoop](https://scoop.sh/)
- Windows Subsystem for Linux with an Ubuntu-based distribution
- Ubuntu Linux
- Arch Linux
- macOS, using [Homebrew](https://brew.sh/)

Python 3.10 or newer is required. The bootstrap scripts install it when possible. Native Windows uses Scoop for all managed package operations; Winget is intentionally not used.

## Quick start

### Linux, WSL, or macOS

```sh
git clone https://github.com/mathwro/DotAi.git
cd DotAi
./bootstrap.sh
```

The bootstrap script locates Python 3.10+, installs it through `apt`, `pacman`, or Homebrew when necessary, and then runs `dotai install`.

### Windows PowerShell

Install Scoop first, then run:

```powershell
git clone https://github.com/mathwro/DotAi.git
Set-Location DotAi
.\bootstrap.ps1
```

The PowerShell bootstrap installs Python through Scoop when needed and then applies the stack.

## Commands

Use `./dotai` on Linux, WSL, and macOS, or `.\dotai.ps1` in PowerShell.

```sh
./dotai install          # Install missing components and synchronize configuration
./dotai update           # Update managed components and synchronize configuration
./dotai sync             # Synchronize skills, plugins, and MCP servers only
./dotai status           # Show installed, missing, inactive, or drifting components
./dotai doctor           # Check the stack plus platform prerequisites
./dotai validate         # Validate stack.json
./dotai platform         # Print the detected platform
```

Preview changes without modifying the machine:

```sh
./dotai install --dry-run
./dotai update --dry-run
./dotai sync --dry-run
```

Reinstall components that already pass their checks:

```sh
./dotai install --force
```

Use a different manifest:

```sh
./dotai --manifest path/to/stack.json status
```

### Status output

Status labels communicate both health and scope:

| Label | Color | Meaning |
| --- | --- | --- |
| `OK` | Green | Installed and active in the intended agent |
| `RUN` | Cyan | An operation is planned or running |
| `INACTIVE` | Yellow | Installed elsewhere, but not active in OMP |
| `DRIFT` | Yellow | Managed configuration is unavailable or differs |
| `MISSING` | Red | A declared component is not installed |
| `FAIL` | Red | An operation or prerequisite check failed |

Color is automatic for interactive terminals. It can be controlled explicitly:

```sh
./dotai --color always status
./dotai --color never status
```

Automatic mode respects `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`.

`status` and `doctor` return a nonzero exit code when a declared component is missing, inactive, drifting, or otherwise unhealthy. This makes them suitable for scripts and machine health checks.

## Configuration safety

DotAi updates configuration conservatively:

- Unmanaged MCP servers and top-level settings are preserved.
- Existing MCP files receive timestamped backups before a managed change.
- MCP servers are matched semantically across configurations OMP can discover, so aliases and provider-specific fields such as authentication headers do not create duplicates.
- Repeated synchronization is idempotent and does not create another backup when nothing changes.
- Skill health is agent-scoped. A skill found only in a Codex plugin cache is reported as `INACTIVE` until installed for Pi/OMP.
- Dry runs do not modify files or machine state.

Credentials and machine-specific MCP headers belong in the user configuration, not in `stack.json` or version control.

## Extending the stack

The `add` commands update `stack.json`. Run `dotai sync` or `dotai install` afterward to apply the new entry.

### Add a skill source

```sh
./dotai add skill owner/repository \
  --skill review \
  --check-skill review
```

Skills default to the `pi` agent and are installed globally through skills.sh. Repeat `--skill` and `--check-skill` when a source provides multiple skills.

### Add a remote MCP server

```sh
./dotai add mcp example --url https://example.com/mcp
```

SSE transport is also supported:

```sh
./dotai add mcp example --url https://example.com/sse --transport sse
```

### Add a local stdio MCP server

```sh
./dotai add mcp local-tools \
  --command npx \
  --arg=-y \
  --arg=@scope/server
```

### Add an OMP marketplace and plugin

```sh
./dotai add marketplace team owner/marketplace
./dotai add plugin review@team --scope user
```

Plugin scope can be `user` or `project`.

### Add a command-line tool

```sh
./dotai add tool Example \
  --check "example --version" \
  --install "windows=scoop install example" \
  --install "macos=brew install example" \
  --install "linux=curl -fsSL https://example.com/install.sh | sh"
```

Add repeatable `--update PLATFORM=COMMAND` options when the tool has a separate update operation. Platform keys are `windows`, `wsl`, `ubuntu`, `arch`, `macos`, `linux`, and `default`.

For more complex entries, edit [`stack.json`](stack.json) directly and validate it against [`stack.schema.json`](stack.schema.json):

```sh
./dotai validate
```

## Repository layout

```text
stack.json          Declarative stack source of truth
stack.schema.json   JSON Schema for stack manifests
dotai.py            Cross-platform manager implementation
dotai               Unix command wrapper
dotai.ps1           PowerShell command wrapper
bootstrap.sh        Linux, WSL, and macOS bootstrap
bootstrap.ps1       Native Windows bootstrap
tests/              Behavioral test suite
AGENTS.md            Repository guidance for coding agents
```

## Development

The runtime uses only the Python standard library and supports Python 3.10 or newer.

Run the behavioral suite:

```sh
rtk python3 -m unittest discover -s tests -v
```

Validate the manifest and Unix launchers:

```sh
rtk python3 dotai.py validate
rtk sh -n bootstrap.sh dotai
```

When changing the manifest format, keep `stack.json`, `stack.schema.json`, CLI mutation commands, and runtime validation aligned. Behavioral changes should include a focused test that protects the user-visible contract.
