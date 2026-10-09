# Extending the stack

Use `./dotai.py` on Linux, WSL, and macOS, or `python dotai.py` in PowerShell. The `add` commands validate and save entries in your selected manifest; they do not install or start integrations. Reusing an MCP or marketplace name, plugin ID, or tool name replaces that manifest entry. Skill additions merge selections for the same source and agent; use `--replace` to replace that source's selections explicitly.

For skills, MCP servers, marketplaces, and plugins, preview with `./dotai.py sync --dry-run`, then apply with `./dotai.py sync`. For command-line tools, use `./dotai.py install --dry-run` and `./dotai.py install`; `sync` skips package checks, installation, updates, and package configuration. Invalid additions leave the existing manifest unchanged, so correct the input and retry. On a fresh clone, initialize the default manifest with `./dotai.py validate` before previews if you want to separate first-use initialization from a dry run.

## Add a skill source

```sh
./dotai.py add skill owner/repository --skill review
```

Skills default to the `universal` agent target, which installs into `~/.agents/skills/`, the user-level location OMP discovers. Repeat `--skill` to select several skills. DotAi preserves the original selections for installation and infers agent-scoped health-check directories using the skills installer's name normalization: lowercase, replace runs outside `a-z`, `0-9`, `.`, and `_` with `-`, trim leading/trailing dots and hyphens, then cap at 255 characters (falling back to `unnamed-skill` if empty). For example, `--skill ALPHA` checks `~/.agents/skills/alpha/SKILL.md`.

For optional skills that belong only in a particular project, use the [vetted project skills guide](project-skills.md). Its commands run the Skills CLI from the target project and install into local `.agents/skills/`; DotAi's `add skill` and `sync` continue to manage user-global manifest entries.

Use `--check-skill NAME` to override inferred checks when the installed directory differs. Explicit check names are used exactly as provided, without normalization; repeat the option for each expected directory.

If no skills are named (or `--skill '*'` is used), status reports `UNVERIFIED` rather than guessing whether the source installed correctly, and exits nonzero. Specify `--check-skill NAME` for each expected installed skill to make the health check actionable.

After synchronization, restart OMP so it discovers newly installed skills. With OMP's default `skills.enableSkillCommands` setting, invoke them as `/skill:<name>` commands, for example `/skill:grilling` or `/skill:commit-and-document`; the shorter `/<name>` form is not the registered command syntax.

Normal synchronization installs missing skills but never refreshes an existing copy silently. A healthy copy with proven source and full-folder ownership remains unchanged. Unknown provenance, local modifications, partial selections, and unverifiable wildcard selections report actionable `DRIFT` instead of overwriting content. The configured agent's files, including supporting files and executable modes, must match the recorded source; another agent's independent copy is not proof.

DotAi reads `$XDG_STATE_HOME/skills/.skill-lock.json` when `XDG_STATE_HOME` is set; otherwise it reads `~/.agents/.skill-lock.json`. Setting XDG changes only the lock location, not the installed skill directory. It does not fall back to a stale home-directory lock when the XDG lock is missing or unreadable.

Public GitHub repository-root spellings such as `owner/repo`, `github:owner/repo`, and `https://github.com/owner/repo` (with an optional `.git` suffix or trailing slash on the URL) are treated as equivalent for ownership checks. Different repositories, hosts, refs, and subpaths are not collapsed together. Add `--revision COMMIT` and `--installer-version VERSION` to declare a reproducible source and installer; unsupported revision/source combinations refuse rather than fetching latest.

Use `./dotai.py update` to refresh selected skills and plugins explicitly. The compatibility `sync --update-skills` option and accepted recommendation changes are also explicit skill refresh requests. Existing copies with uncertain ownership or local changes still require explicit adoption or manual resolution before refresh. Missing named selections install only when doing so cannot overwrite existing uncertain content. Wildcard sources cannot prove that an upstream add is safe and require explicit review.

Normal `sync`, including `sync --dry-run`, compares your existing manifest with the repository's recommended skills. Pending additions, updates, and removals appear as a human-readable list of `owner/repository` sources and selected skill names, with instructions for reviewing them; locally differing recommended sources are reported and preserved. This notice does not prompt, adopt recommendations, or install skills absent from your manifest. No notice is shown when recommendations already match.

To review and accept skill recommendations added, changed, or removed from `stack.example.json`, run:

```sh
./dotai.py sync --recommended-skills
```

DotAi prints the proposed manifest diff, then lets you accept all changes, review each change, or cancel. Accepted removals also uninstall the retired skills so OMP no longer discovers them; rejected changes are offered again later. User-added sources and locally modified recommended entries are preserved. Use `./dotai.py sync --recommended-skills --dry-run` to print the diff and planned actions without changing files or machine state.

Named retirements and retained selections match installed-directory identities from the Skills CLI listing, not its original frontmatter names. Inferred selections use installer normalization; nonempty explicit `--check-skill` directories remain exact and authoritative. A wildcard retirement resolves every installed directory attributed to that source and configured agent, regardless of its health-check selection. Named dry runs preview directory paths without querying the installer; wildcard resolution is deferred until application.

DotAi retires source-proven copies directly inside the configured agent's skill root instead of invoking the installer's removal command, which can affect other agents' canonical copies even with an explicit scope. Independent Pi and universal copies are preserved. Each source's complete deletion set is checked before removing any directory. Shared links, redirected roots, or canonical associations whose independent ownership cannot be proven fail conservatively and require manual resolution. The shared Skills CLI lock is left untouched so other agents' provenance is retained; an absent directory is not considered installed merely because its lock entry remains.

The Skills CLI can include other agents' directories even with `--agent` selected. Post-removal verification ignores entries outside the configured and canonical skill roots; source-specific deletion checks still reject foreign paths. An unrelated Codex skill therefore cannot make a completed universal retirement fail verification.

If listing, directory removal, or verification fails, the manifest and recommendation ownership are restored so retirement remains pending; directories already removed are not restored. Remaining files cannot be reported as a successful retirement even if their source metadata disappears. An installation failure after an accepted addition or migration retains the saved configuration for retry and does not record successful reconciliation.

The first run on an existing installation establishes recommendation ownership conservatively from exact matches in the current example. Entries DotAi cannot prove it previously recommended remain user-owned. To review exact recommendations for locally differing sources, run `./dotai.py sync --recommended-skills --enforce`.

Enforced sync starts by listing user-owned sources declared in the selected manifest but absent from the current recommendations, including old defaults whose recommendation ownership was never recorded. The cleanup list shows one `owner/repository/skill` per named selection, or just `owner/repository` for wildcard or unspecified selections, rather than a JSON diff. It asks whether to remove those entries and their source-verified installed copies. Answer `y` or `yes` to apply cleanup immediately with a manifest backup; answer anything else, press Enter, or leave input unavailable to preserve them. Preserved sources are skipped during this enforced sync, even with `--update-skills`; ordinary sync still manages them. Undeclared skills and other agents' independent copies remain untouched. Shared or ambiguous copies cannot be removed automatically.

After that choice, DotAi continues to the existing recommendation diff and acceptance prompt. Cleanup consent and recommendation acceptance are separate: declining the recommendation changes does not undo previously confirmed cleanup. A cleanup failure restores the manifest, leaves ownership unchanged, and stops before recommendation installation; already removed directories are not restored. `--dry-run` does not prompt: it previews cleanup as conditional on confirmation and then previews recommendation changes without modifying the manifest, state, or installed skills.

Normal `sync` runs do not rewrite existing `stack.json` skill entries. To migrate all legacy skill entries that still target Pi, run:

```sh
./dotai.py fix
```

DotAi shows the exact manifest diff and skill installation commands, then waits for confirmation. Answer `y` to apply the changes. Use `./dotai.py fix --dry-run` to preview the diff and commands without modifying the manifest or machine. The command changes only `"agent": "pi"` skill entries to `"agent": "universal"`, creates a timestamped manifest backup, and leaves the old Pi-installed files in place.

After the migration, future `./dotai.py sync` runs use `~/.agents/skills/`. `sync` alone intentionally does not change an existing manifest's agent selections.

## Add a remote MCP server

```sh
./dotai.py add mcp example --url https://example.com/mcp
```

SSE transport is also supported:

```sh
./dotai.py add mcp example --url https://example.com/sse --transport sse
```

Add repeatable HTTP headers with `--header NAME=VALUE`. For secrets, use the environment-variable name as the value:

```sh
./dotai.py add mcp context7 \
  --url https://mcp.context7.com/mcp \
  --header CONTEXT7_API_KEY=CONTEXT7_API_KEY

export CONTEXT7_API_KEY="your-key"
./dotai.py sync
```

PowerShell uses the same environment-variable reference:

```powershell
$env:CONTEXT7_API_KEY = "your-key"
python dotai.py sync
```

OMP resolves a header value as an environment-variable name first and uses a literal value only when that variable is absent. Do not expand the secret in the command or pass the key itself: that would write the credential into `stack.json`. Ensure referenced variables are defined before starting OMP.

## Add a local stdio MCP server

```sh
./dotai.py add mcp local-tools \
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
./dotai.py add marketplace team owner/marketplace
./dotai.py add plugin review@team --scope user
```

Plugin scope can be `user` or `project`.

`status` and `doctor` check marketplace registration and the plugin's selected user or project registry. Missing, malformed, or unreadable registries, including invalid UTF-8, are unhealthy rather than successful installations. Extension health independently requires both global registration and an available source file; registering a path alone does not make the extension healthy.

Synchronization leaves registered marketplaces and installed plugins at their current versions; missing plugins install without `--force`. Use `update` for explicit marketplace/plugin refresh. An exact plugin version that OMP cannot install is rejected rather than silently replaced with latest. Keep a pinned plugin's marketplace source reproducible.

## Add a command-line tool

```sh
./dotai.py add tool Example \
  --check "example --version" \
  --install "windows=scoop install example" \
  --install "macos=brew install example" \
  --install "linux=curl -fsSL https://example.com/install.sh | sh"
```

Add repeatable `--update PLATFORM=COMMAND` options when the tool has a separate update operation. Platform keys are `windows`, `wsl`, `ubuntu`, `arch`, `macos`, `linux`, and `default`.

Declare checks-only supporting prerequisites with repeatable `--requires NAME`. DotAi never installs Node.js, npm, uv, Git, curl, or platform package managers as an incidental dependency.

For more complex entries, edit the local `stack.json` directly and run the runtime validator; [`stack.schema.json`](../stack.schema.json) also describes the format for editors:

```sh
./dotai.py validate
```

## Inspect and manage one component

Selectors use `TYPE:ID`. Supported types are `tool`, `skill`, `plugin`, `marketplace`, `mcp`, and `extension`. Skill IDs are sources; plugin IDs include the marketplace; extension IDs are their declared paths. When a source is declared for several agents, qualify it as `skill:owner/repository@universal`. Qualify an ambiguous plugin scope as `plugin:review@team#user`.

```sh
./dotai.py list
./dotai.py show skill:owner/repository
./dotai.py list mcp:docs plugin:review@team
./dotai.py sync --only skill:owner/repository --dry-run
./dotai.py update --only plugin:review@team
```

`list` and `show` describe desired and observed state, enabled status, management, ownership, scope, source, known versions/revisions, and paths. Declaring a component is not proof that DotAi owns the installed copy. Unknown provenance and local changes remain visible rather than becoming ownership merely because a file or registration exists. Targeted reconciliation retains checks-only prerequisite definitions but excludes unrelated tools, skills, plugins, marketplaces, MCP requirements, extensions, and routing.

### Adopt matching existing content

```sh
./dotai.py adopt skill:owner/repository --dry-run
./dotai.py adopt skill:owner/repository
./dotai.py adopt mcp:docs
```

Explicit adoption grants permission only for existing content matching the declaration and its scope/source. Skill adoption verifies installer source records and independent configured-agent paths; it does not adopt an unrelated folder with the same name. MCP adoption requires one matching registration in the selected target's `mcpServers` container, preserving its actual alias and additional authentication fields. External provider files, ambiguous matches, redirected directories, and unrelated content are not writable through adoption.

Machine-local ownership receipts live in DotAi's state directory as `component-receipts.json`. They record scoped identities and content fingerprints, not MCP authentication values. Receipts are separate from the portable manifest and version lock: adopting a copy does not invent an immutable source revision or installer version. Installation/configuration records ownership only after a successful permitted operation; a healthy skipped unmanaged copy is never silently adopted. Synchronization can use explicit adoption receipts as well as the authoritative installer ownership proof and refuses later modifications.

### Forget or uninstall

```sh
./dotai.py remove skill:owner/repository
./dotai.py remove mcp:docs --uninstall --dry-run
./dotai.py remove mcp:docs --uninstall
```

Removal without `--uninstall` forgets only the selected declaration and leaves installed content and other components untouched. With `--uninstall`, DotAi preflights ownership, unchanged fingerprints, source/scope, and sharing before deleting files or invoking an upstream command. Modified, unmanaged, redirected, or shared content is refused. Skills are removed directly from their independently owned agent directory instead of using an installer removal command that could affect other agents. Owned disabled-skill storage is also removed when explicitly uninstalling that source.

MCP removal affects only the owned target alias; unrelated servers, provider sections, extra fields, and other providers survive. A registration satisfying another declared requirement cannot be disabled or removed through just one requirement. Plugins use OMP's explicitly scoped uninstall command only when their cache and runtime package association remain independent and unchanged. A retargeted runtime link to unrelated npm content is not permission to remove it. Marketplaces with installed plugin dependents cannot be unregistered.

Tool removal requires an owned executable fingerprint and a reviewed catalog/custom uninstall command; it never guesses a package-manager operation. Unsupported operations fail with a next step rather than reporting a no-op as uninstall success.

### Enable and disable actual activation

```sh
./dotai.py disable skill:owner/repository
./dotai.py enable skill:owner/repository
./dotai.py disable plugin:review@team
./dotai.py disable extension:~/extension.ts
```

Disabling is not merely a flag that skips future sync. Owned skills move out of the active agent root into scoped disabled storage and can be restored only when that storage is unchanged and the active destination is safe. OMP plugins use scoped `enable`/`disable`; extensions lose only their owned registration, not their source file; MCP aliases change their actual provider enabled state without dropping authentication fields. Marketplaces unregister only when no installed plugins depend on them. Tools need a supported owned uninstall operation to become inactive.

Every collection honors `enabled: false`; actual active state that cannot be changed safely produces actionable drift. Unmanaged and other-agent copies are preserved. Existing project-scoped plugin registrations and read-only MCP discovery use the invocation directory, never the DotAi checkout; this does not introduce project manifest layering or project skill management.

Mutation previews change neither declarations, receipts, backups, nor runtime files. Applied manifest/config changes receive backups. If an external command, file operation, or persistence step fails, already completed runtime changes are not universally rolled back: DotAi stops and reports the failure without claiming the whole component transaction succeeded.
