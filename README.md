# DotAi

DotAi is a declarative, cross-platform manager for a personal AI development stack. It applies the same tools, skills, plugins, and MCP servers across Windows, WSL, Ubuntu, Arch Linux, and macOS while preserving configuration that DotAi does not own.

Repository defaults live in [`stack.example.json`](stack.example.json). On the first manifest-using command, DotAi copies that template to an ignored, user-owned `stack.json`; later runs install, update, synchronize, and extend the local manifest without overwriting it from the example.

## Installation

Supported platforms:

- Native Windows, using [Scoop](https://scoop.sh/)
- Windows Subsystem for Linux with an Ubuntu-based distribution
- Ubuntu Linux
- Arch Linux
- macOS, using [Homebrew](https://brew.sh/)

Python 3.10 or newer is required. The bootstrap scripts install it when possible. Native Windows uses Scoop for all managed package operations; Winget is intentionally not used.

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

## How to use

Use `./dotai` on Linux, WSL, and macOS, or `.\dotai.ps1` in PowerShell.

```sh
./dotai install          # Install missing components and synchronize configuration
./dotai update           # Update core components and synchronize configuration
./dotai sync             # Synchronize skills, plugins, and MCP servers only
./dotai status           # Show installed, missing, inactive, or drifting components
./dotai doctor           # Check the stack plus platform prerequisites
./dotai validate         # Initialize when absent, then validate stack.json
./dotai --manifest path/to/new-stack.json init  # Generate a new manifest from the example
./dotai version            # Print the version and warn about newer releases
./dotai platform           # Print the detected platform
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

Update dependency tools such as Node.js and `uv` explicitly:

```sh
./dotai update --include-dependencies
```

Dependency updates are skipped by default, but missing dependencies are still installed. OMP updates use `omp update`, so OMP performs its own version check and leaves an up-to-date installation unchanged.

Use a different manifest:

```sh
./dotai --manifest path/to/stack.json status
```

Generate a new manifest from the repository example:

```sh
./dotai --manifest path/to/new-stack.json init
```

`init` creates only a missing manifest and refuses to overwrite an existing file.

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

## Managed stack

| Type | Components |
| --- | --- |
| Harness | [Oh My Pi](https://github.com/can1357/oh-my-pi) |
| Tools | [RTK](https://github.com/rtk-ai/rtk), [Graphify](https://github.com/Graphify-Labs/graphify), Node.js, `uv`, `curl` |
| Skills | [Ponytail](https://github.com/DietrichGebert/ponytail), [Superpowers](https://github.com/obra/superpowers), [Grill Me](https://github.com/mattpocock/skills) (`grill-me`, `grill-with-docs`), [Commit and Document](https://github.com/mathwro/Skills) (`commit-and-document`), installed through [skills.sh](https://skills.sh/) |
| MCP servers | [Context7](https://context7.com/), [Microsoft Learn](https://learn.microsoft.com/training/support/mcp) |

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi adds to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `dotai status` verifies both the registration and the extension source.
DotAi enables OMP's **Hide Secrets** privacy setting (`secrets.enabled`) during installation and updates, so configured secrets are obfuscated before prompts are sent to providers.

The Pi extension is independent of RTK's optional Codex integration.

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. `stack.json` is created automatically from it when `install`, `update`, `sync`, `status`, `doctor`, `validate`, or `add` first needs the default manifest.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations only; existing users can merge desired template changes into their local file.

To recreate the defaults, remove the local `stack.json` and run:

```sh
./dotai validate
```

For a separate manifest, use `./dotai --manifest path/to/new-stack.json init`. Normal commands never initialize or overwrite an explicitly selected custom path, and `init` also refuses to overwrite an existing file.

## Configuration safety

DotAi updates configuration conservatively:

- Unmanaged MCP servers and top-level settings are preserved.
- Existing MCP files receive timestamped backups before a managed change.
- MCP servers are matched semantically across configurations OMP can discover, so aliases and provider-specific fields such as authentication headers do not create duplicates.
- Repeated synchronization is idempotent and does not create another backup when nothing changes.
- Managed `ompExtensions` are appended to OMP's global extension list; unrelated user extensions are retained.
- Skill health is agent-scoped. A skill found only in a Codex plugin cache is reported as `INACTIVE` until installed for the configured OMP skill target.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network failures are ignored.
- Dry runs do not modify files or machine state.

### Configure OMP provider routing

After installation, configure routing from the providers already authenticated in OMP:

1. Run `./dotai install`.
2. Authenticate GitHub Copilot, OpenAI Codex, Anthropic, or any combination of them inside OMP.
3. Preview the detected providers, resolved roles, manifest diff, and pending OMP commands:

   ```sh
   ./dotai configure omp-routing --dry-run
   ```

4. If both Anthropic and OpenAI Codex are authenticated, choose the interactive primary when prompted or pass `--primary anthropic` or `--primary openai-codex`. Use the same flag while previewing and applying when a non-interactive shell cannot prompt.
5. Apply the routing:

   ```sh
   ./dotai configure omp-routing
   ```

The `default` and `slow` interactive roles prefer the selected premium primary, then the other available premium provider, then Copilot. The `task` and `smol` worker roles prefer Copilot, then Anthropic, then Codex. When no premium provider is available, Copilot serves as the primary.

DotAi stores only compact routing intent in `stack.json`: the detected provider set, selected primary, agent overrides, and usage/fallback policies. Expanded model routes stay in OMP. DotAi discovers availability from OMP without reading provider credentials, and it preserves unrelated OMP roles, fallback chains, agent overrides, extensions, and other settings.

If an existing manifest still contains static `ompRouting.roles`, run `./dotai configure omp-routing` to perform the backed-up, one-way migration to compact intent. Provider authentication changes appear as `DRIFT` while at least one persisted provider remains available; if none remains, `dotai status` reports `INACTIVE`. Rerun `./dotai configure omp-routing` to refresh the persisted intent and managed OMP routes. Status is observational and never prompts or writes.

## Extending the stack

The `add` commands update `stack.json`. Run `dotai sync` or `dotai install` afterward to apply the new entry.

### Add a skill source

```sh
./dotai add skill owner/repository \
  --skill review \
  --check-skill review
```

Skills default to the `universal` agent target, which installs into `~/.agents/skills/`, the user-level location OMP discovers. Repeat `--skill` and `--check-skill` when a source provides multiple skills.
After synchronization, restart OMP so it discovers newly installed skills. With OMP's default `skills.enableSkillCommands` setting, invoke them as `/skill:<name>` commands, for example `/skill:grill-me`, `/skill:grill-with-docs`, or `/skill:commit-and-document`; the shorter `/<name>` form is not the registered command syntax.

Existing `stack.json` files are not rewritten by `sync`, so existing configurations remain safe. To migrate all legacy skill entries that still target Pi, run:

```sh
./dotai fix
```

DotAi shows the exact manifest diff and the skill installation commands, then waits for confirmation. Answer `y` to apply the changes. Use `./dotai fix --dry-run` to preview the diff and commands without modifying the manifest or machine. The command changes only `"agent": "pi"` skill entries to `"agent": "universal"`, creates a timestamped manifest backup, and leaves the old Pi-installed files in place.

After the migration, future `dotai sync` runs use `~/.agents/skills/`, the user-level location OMP discovers. `sync` alone intentionally does not change an existing manifest's agent selections.

### Add a remote MCP server

```sh
./dotai add mcp example --url https://example.com/mcp
```

SSE transport is also supported:

```sh
./dotai add mcp example --url https://example.com/sse --transport sse
```

Add repeatable HTTP headers with `--header NAME=VALUE`. For secrets, use the environment-variable name as the value:

```sh
./dotai add mcp context7 \
  --url https://mcp.context7.com/mcp \
  --header CONTEXT7_API_KEY=CONTEXT7_API_KEY

export CONTEXT7_API_KEY="your-key"
./dotai sync
```

PowerShell uses the same environment-variable reference:

```powershell
$env:CONTEXT7_API_KEY = "your-key"
.\dotai.ps1 sync
```

OMP resolves a header value as an environment-variable name first and uses a literal value only when that variable is absent. Do not expand the secret in the command or pass the key itself: that would write the credential into `stack.json`. Ensure referenced variables are defined before starting OMP.

### Add a local stdio MCP server

```sh
./dotai add mcp local-tools \
  --command npx \
  --arg=-y \
  --arg=@scope/server \
  --env API_TOKEN=LOCAL_API_TOKEN \
  --env LOG_LEVEL=warning
```

Use repeatable `--env NAME=VALUE` options to define the environment passed to the stdio server. Values may be environment-variable references, secret commands supported by OMP, or non-sensitive literals.

`--header` is valid only with `--url`; `--env` is valid only with `--command`. Both options are repeatable, reject duplicate names, and preserve values containing additional `=` characters.

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

Use `--update-group dependency` for supporting tools that should be installed when missing but updated only by `dotai update --include-dependencies`.

For more complex entries, edit the local `stack.json` directly and validate it against [`stack.schema.json`](stack.schema.json):

```sh
./dotai validate
```

## Repository layout

```text
stack.example.json  Tracked baseline copied for new users
stack.json          Ignored, user-owned stack configuration
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

When changing the manifest format or shared defaults, keep `stack.example.json`, `stack.schema.json`, CLI mutation commands, and runtime validation aligned. Never commit a generated `stack.json`. Behavioral changes should include a focused test that protects the user-visible contract.
