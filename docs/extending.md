# Extending the stack

The `add` commands update `stack.json`. Run `dotai sync` or `dotai install` afterward to apply the new entry.

## Add a skill source

```sh
./dotai add skill owner/repository \
  --skill review \
  --check-skill review
```

Skills default to the `universal` agent target, which installs into `~/.agents/skills/`, the user-level location OMP discovers. Repeat `--skill` and `--check-skill` when a source provides multiple skills.

After synchronization, restart OMP so it discovers newly installed skills. With OMP's default `skills.enableSkillCommands` setting, invoke them as `/skill:<name>` commands, for example `/skill:grill-me`, `/skill:grill-with-docs`, or `/skill:commit-and-document`; the shorter `/<name>` form is not the registered command syntax.

To review skill recommendations added, changed, or removed from `stack.example.json`, run:

```sh
./dotai sync --recommended-skills
```

DotAi prints the proposed manifest diff, then lets you accept all changes, review each change, or cancel. Accepted removals also uninstall the retired skills so OMP no longer discovers them; rejected changes are offered again later. User-added sources and locally modified recommended entries are preserved. Use `./dotai sync --recommended-skills --dry-run` to print the diff and planned actions without changing files or machine state.

The first run on an existing installation establishes recommendation ownership conservatively from exact matches in the current example. Entries DotAi cannot prove it previously recommended remain user-owned and are not removed.

Normal `sync` runs do not rewrite existing `stack.json` skill entries. To migrate all legacy skill entries that still target Pi, run:

```sh
./dotai fix
```

DotAi shows the exact manifest diff and skill installation commands, then waits for confirmation. Answer `y` to apply the changes. Use `./dotai fix --dry-run` to preview the diff and commands without modifying the manifest or machine. The command changes only `"agent": "pi"` skill entries to `"agent": "universal"`, creates a timestamped manifest backup, and leaves the old Pi-installed files in place.

After the migration, future `dotai sync` runs use `~/.agents/skills/`. `sync` alone intentionally does not change an existing manifest's agent selections.

## Add a remote MCP server

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

## Add a local stdio MCP server

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

## Add an OMP marketplace and plugin

```sh
./dotai add marketplace team owner/marketplace
./dotai add plugin review@team --scope user
```

Plugin scope can be `user` or `project`.

## Add a command-line tool

```sh
./dotai add tool Example \
  --check "example --version" \
  --install "windows=scoop install example" \
  --install "macos=brew install example" \
  --install "linux=curl -fsSL https://example.com/install.sh | sh"
```

Add repeatable `--update PLATFORM=COMMAND` options when the tool has a separate update operation. Platform keys are `windows`, `wsl`, `ubuntu`, `arch`, `macos`, `linux`, and `default`.

Use `--update-group dependency` for supporting tools that should be installed when missing but updated only by `dotai update --include-dependencies`.

For more complex entries, edit the local `stack.json` directly and validate it against [`stack.schema.json`](../stack.schema.json):

```sh
./dotai validate
```
