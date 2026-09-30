# Extending the stack

Use `./dotai` on Linux, WSL, and macOS, or `.\dotai.ps1` in PowerShell. The `add` commands validate and save entries in your user-owned `stack.json`; they do not install or start integrations. Reusing a skill source, MCP or marketplace name, plugin ID, or tool name replaces that entire manifest entry, so repeat every skill selection or option you want to retain.

For skills, MCP servers, marketplaces, and plugins, preview with `./dotai sync --dry-run`, then apply with `./dotai sync`. For command-line tools, use `./dotai install --dry-run` and `./dotai install`; `sync` skips package checks, installation, updates, and package configuration. Invalid additions leave the existing manifest unchanged, so correct the input and retry. On a fresh clone, initialize the default manifest with `./dotai validate` before previews if you want to separate first-use initialization from a dry run.

## Add a skill source

```sh
./dotai add skill owner/repository --skill review
```

Skills default to the `universal` agent target, which installs into `~/.agents/skills/`, the user-level location OMP discovers. Repeat `--skill` to select several skills. DotAi preserves the original selections for installation and infers agent-scoped health-check directories using the skills installer's name normalization: lowercase, replace runs outside `a-z`, `0-9`, `.`, and `_` with `-`, trim leading/trailing dots and hyphens, then cap at 255 characters (falling back to `unnamed-skill` if empty). For example, `--skill ALPHA` checks `~/.agents/skills/alpha/SKILL.md`.

Use `--check-skill NAME` to override inferred checks when the installed directory differs. Explicit check names are used exactly as provided, without normalization; repeat the option for each expected directory.

If no skills are named (or `--skill '*'` is used), status reports `UNVERIFIED` rather than guessing whether the source installed correctly, and exits nonzero. Specify `--check-skill NAME` for each expected installed skill to make the health check actionable.

After synchronization, restart OMP so it discovers newly installed skills. With OMP's default `skills.enableSkillCommands` setting, invoke them as `/skill:<name>` commands, for example `/skill:grilling` or `/skill:commit-and-document`; the shorter `/<name>` form is not the registered command syntax.

Normal `install`, `update`, and `sync` leave named skills untouched only when their configured-agent files match the GitHub folder tree hash in the skills.sh v3 global lock and its source metadata matches the requested repository. The lock is keyed by skill name, not agent: another agent's independent copy does not prove ownership of the configured target. DotAi checks the entire installed folder, including supporting files and executable modes, rather than trusting a matching name or `SKILL.md` alone.

DotAi reads `$XDG_STATE_HOME/skills/.skill-lock.json` when `XDG_STATE_HOME` is set; otherwise it reads `~/.agents/.skill-lock.json`. Setting XDG changes only the lock location, not the installed skill directory. It does not fall back to a stale home-directory lock when the XDG lock is missing or unreadable.

Public GitHub repository-root spellings such as `owner/repo`, `github:owner/repo`, and `https://github.com/owner/repo` (with an optional `.git` suffix or trailing slash on the URL) are treated as equivalent. Different repositories, hosts, refs, and subpaths are not collapsed together. Refresh avoidance is conservative: non-GitHub sources, ref/subpath selections, missing or invalid hashes, changed content or modes, and copies whose original tree cannot be reconstructed are reconciled rather than attributed to the wrong source. Such sources may be fetched again on each run.

Use `./dotai sync --update-skills` to refresh healthy skills explicitly; `./dotai install --force` refreshes them too. Accepting a changed recommended source also refreshes it.

Sources without named `checkSkills` cannot be confirmed installed and are still reconciled on each run. Naming the expected skills enables source and content verification; it does not override missing or ambiguous ownership evidence.

To review skill recommendations added, changed, or removed from `stack.example.json`, run:

```sh
./dotai sync --recommended-skills
```

DotAi prints the proposed manifest diff, then lets you accept all changes, review each change, or cancel. Accepted removals also uninstall the retired skills so OMP no longer discovers them; rejected changes are offered again later. User-added sources and locally modified recommended entries are preserved. Use `./dotai sync --recommended-skills --dry-run` to print the diff and planned actions without changing files or machine state.

The first run on an existing installation establishes recommendation ownership conservatively from exact matches in the current example. Entries DotAi cannot prove it previously recommended remain user-owned and are not removed. To adopt existing entries whose sources match the repository recommendations and remove skills those sources no longer recommend, run `./dotai sync --recommended-skills --enforce`. DotAi still shows the diff and requires acceptance; unrelated user-added sources remain untouched.

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

Define referenced variables in the environment where OMP starts; adding a reference to the manifest does not define the variable. Keep credential values out of command arguments and version control.

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

For more complex entries, edit the local `stack.json` directly and run the runtime validator; [`stack.schema.json`](../stack.schema.json) also describes the format for editors:

```sh
./dotai validate
```
