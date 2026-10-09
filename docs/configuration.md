# Configuration

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. Any command that needs the default manifest initializes a missing `stack.json` once from that example; `version`, `platform`, and help do not need a manifest. This initialization also occurs before a first-use dry run. Run `./dotai.py validate` first to initialize and check the manifest separately from previewing changes.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations automatically; existing users can opt into recommended skill changes with `./dotai.py sync --recommended-skills`.

To recreate the defaults, first back up your local `stack.json`, then remove it and run:

```sh
./dotai.py validate
```

For a separate manifest, use `./dotai.py --manifest path/to/new-stack.json init`. Normal commands never initialize or overwrite an explicitly selected custom path, and `init` also refuses to overwrite an existing file.

`validate` checks required sections and supported package, skill, plugin, and MCP entry shapes before they can be applied, including valid HTTP(S) server URLs and port numbers. The same validation runs before initializing a manifest or saving changes from `add`, recommended skill synchronization, skill migration, or routing configuration. Invalid additions (for example, an MCP URL with an invalid port, a malformed `plugin@marketplace` ID, or an empty tool check command) exit with code `2` without changing the existing manifest or creating backups; correct the input and retry. User-owned extra fields and credential references remain untouched, and unconfigured routing remains `null` when saved. See [`stack.schema.json`](../stack.schema.json) for the declarative format.

## Configuration safety

DotAi updates configuration conservatively:

- Unmanaged MCP servers and top-level settings are preserved.
- Existing MCP files receive timestamped backups before a managed change.
- MCP servers are matched semantically across configurations OMP can discover, so aliases and provider-specific fields such as authentication headers do not create duplicates. One healthy provider entry can satisfy multiple equivalent managed requirements.
- Required stdio `cwd`, server `timeout`, and enabled state participate in health matching. Reconciliation prefers exact managed names, then matching working directories when distinguishing stdio instances with the same command and arguments. An enabled target alias can be reconciled without dropping its unrelated settings, but cannot be overwritten for another requirement after it has been selected. Ambiguous write candidates require manual resolution.
- Discovery retains each entry's file and section. DotAi writes only the target file's `mcpServers` container; entries in `servers`, `mcp`, or other provider files remain untouched. A drifting provider entry without a writable target match requires manual resolution rather than overwriting a same-named unrelated target server.
- Malformed argument lists on unrelated stdio entries do not match managed servers or prevent reconciliation; those entries remain unchanged, including during dry runs.
- If a managed server name in the target file contains a non-object entry, reconciliation replaces that entry after backing up the original file; unrelated servers and top-level settings are retained.
- A matching server disabled in its provider configuration is a conflict: `sync` reports `DRIFT` and exits nonzero rather than enabling it, duplicating an alias, or reporting success. Resolve the provider's `disabledServers` setting yourself.
- MCP status confirms configured provider entries, not network reachability or successful server startup.
- Repeated synchronization is idempotent and does not create another backup when nothing changes.
- `install`, `update`, and `sync` report MCP configuration errors as failures and exit nonzero without overwriting invalid JSON.
- MCP targets must be readable UTF-8 JSON objects. When present, `mcpServers` must be an object and `disabledServers` an array of server-name strings. Invalid targets are unhealthy and fail reconciliation before writes or backups, including dry runs. This container validation does not prevent replacing a non-object managed entry. Independently malformed provider files are ignored without modification and cannot satisfy managed requirements.
- Required header and environment references participate in MCP health. Writable target aliases can be corrected while preserving extra references; drifting external-only entries require manual resolution.
- Managed `ompExtensions` are appended to OMP's global extension list; unrelated user extensions are retained.
- Skill health is agent-scoped. A skill found only in a Codex plugin cache is reported as `INACTIVE` until installed for the configured OMP skill target.
- Avoiding skill fetches requires matching source metadata and a matching GitHub folder tree hash for the configured agent's installed files; the name-only global lock cannot establish ownership of another agent's independent copy. Missing or uncertain provenance triggers a refresh, not a silent skip.
- The skills.sh lock is read from `$XDG_STATE_HOME/skills/.skill-lock.json` when set, otherwise from `~/.agents/.skill-lock.json`. Installed universal skills remain in `~/.agents/skills/`; DotAi adds no separate ownership database. See [skill refresh behavior and limits](extending.md#add-a-skill-source).
- Recommended skill synchronization preserves user-added and locally modified sources by default, backs up `stack.json`, and removes installed files only for accepted retirements. With `--enforce`, an initial cleanup prompt can explicitly remove user-owned sources outside the recommendations; declining preserves them and skips their installation for that run before recommendation review continues.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network failures are ignored.
- After manifest initialization, dry runs do not modify existing files or managed machine state.

Backups retain the previous complete file, including any literal credentials already present. Newly created backups are private to the current user on POSIX, but DotAi does not delete historical backups: after rotating a credential, review and remove old `mcp.json.bak.*` and `stack.json.bak.*` copies yourself. Custom manifest names and backup paths inside other Git repositories need their own ignore rules; keep credentials as environment or secret-manager references rather than literals.

Manifest and reconciliation-state writes use temporary files before replacement. A failed copy, write, or replacement preserves the original file and removes newly created incomplete backup or temporary files; a completed backup remains available. An existing backup with the same timestamp is never overwritten. State files are updated individually, not as a multi-file transaction.

## Managed OMP extensions

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi appends to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `./dotai.py status` verifies both registration and source availability.

The Linux RTK commands in `stack.example.json` use the reviewed v0.50.0 binary archives and architecture-specific SHA-256 digests. Updating the pinned release requires updating its version and archive digests together; existing user-owned `stack.json` files never receive such baseline changes automatically. Windows and macOS continue to use Scoop and Homebrew.

The manifest declares RTK's `minimumVersion` as `0.43`. Package checks compare the command's reported `major.minor[.patch]` version from stdout or stderr; an older installed binary is upgraded, while a missing binary is installed. Status does not silently accept an unsupported or unparseable RTK. Other packages may declare the same optional constraint.

For packages with `updateGroup: "dependency"`, normal `update` leaves a present binary unchanged unless `--include-dependencies` is supplied, even when its version is below `minimumVersion` or cannot be parsed. This opt-in takes precedence over minimum-version upgrades during updates; missing dependencies are still installed. A skipped dependency with an unresolved minimum-version check remains unhealthy and causes reconciliation verification to fail. `install` still upgrades present packages below their minimum, and normal updates still upgrade core packages.

On Windows, command execution refreshes the machine and user `PATH` so newly installed shims are visible to later commands. Refreshing repeatedly preserves other inherited entries without accumulating another copy of the registry paths on each command.

The Pi extension is independent of RTK's optional Codex integration. DotAi also enables OMP's **Hide Secrets** privacy setting (`secrets.enabled`) during installation and updates, so configured secrets are obfuscated before prompts are sent to providers.

## Optional Graphify

The recommended stack installs the Graphify CLI, but does not install its Pi skill or generate project graphs automatically. Invoke `graphify extract . --code-only` in a project only when you want a graph; it writes `graphify-out/` there.

Existing `stack.json` files are user-owned and are not updated from `stack.example.json` by `sync` or `sync --recommended-skills`. If your Graphify package still has a `configure` command that runs `graphify install --platform pi`, remove that command from your local manifest before your next `install` or `update`. To retire the previously installed broad Pi skill, run `graphify pi uninstall` yourself. These steps leave the CLI available for explicit use.

## Configure OMP provider routing

**Optional, post-authentication setup:** run the routing commands only after installing the stack, opening OMP for the first time, and authenticating at least one supported provider. DotAi does not perform provider login.

Routing is never enabled automatically by `install`, `update`, or `sync`. Until it is configured, `status` and `doctor` display a non-failing `INACTIVE` hint with the preview command; they do not inspect credentials or change OMP configuration.

1. Run `./dotai.py install` (or `python dotai.py install` on Windows).
2. Run `omp` to open OMP for the first time.
3. Inside OMP, use `/login` to authenticate GitHub Copilot, OpenAI Codex, Anthropic, or any combination of them, then return to your shell. See [OMP's provider authentication guide](https://github.com/can1357/oh-my-pi/blob/main/packages/ai/README.md#oauth-providers) for supported login flows. Authentication belongs to OMP, not to `stack.json`.
4. Only after those prerequisites, preview the detected providers, resolved roles, manifest diff, and pending OMP commands:

   ```sh
   ./dotai.py configure omp-routing --dry-run
   ```

5. If both Anthropic and OpenAI Codex are authenticated, choose the interactive primary when prompted or pass `--primary anthropic` or `--primary openai-codex`. Use the same flag while previewing and applying when a non-interactive shell cannot prompt.
6. Apply the routing only if you want DotAi to manage it:

   ```sh
   ./dotai.py configure omp-routing
   ```

In PowerShell, substitute `python dotai.py` for `./dotai.py`; `omp` is the same command on all supported platforms.

The `default` and `slow` interactive roles prefer the selected premium primary, then the other available premium provider, then Copilot. The `task` and `smol` worker roles prefer Copilot, then Anthropic, then Codex. When no premium provider is available, Copilot serves as the primary.

The tracked `routing-recommendations.json` selects the first available model in each role's ordered list. Recommendations refreshed on September 30, 2026:

| Provider | `default` | `task` | `smol` | `slow` |
| --- | --- | --- | --- | --- |
| GitHub Copilot | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| OpenAI Codex | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| Anthropic | Claude Opus 5.5 | Claude Sonnet 5.5 | Claude Haiku 4.5 | Claude Opus 5.5, high effort |

[GPT-6.1 Sol](https://openai.com/index/introducing-gpt-6-1-sol/) offers near-Astra coding capability at lower API cost; Astra remains the recommendation for the most demanding reasoning. [Opus 5.5](https://www.anthropic.com/claude-opus-5-5) replaces Opus 5 for interactive work and precedes Fable 5.1 for `slow`; [Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5) replaces Sonnet 5 for workers. Haiku 5.5 is not yet released, so the small Anthropic role remains on Haiku 4.5. These are curated role choices, not runtime price comparisons.

Older recommendations remain fallbacks for staged rollouts or restricted subscriptions; unavailable models are omitted. Exact IDs follow OMP's [model catalog](https://github.com/can1357/oh-my-pi/blob/main/packages/catalog/src/models.json) and live `omp models --json` output, not display names (Anthropic uses `claude-opus-5-5` and `claude-sonnet-5-5`). After pulling recommendation updates, preview and rerun `./dotai.py configure omp-routing` to apply them; pulling alone does not change your manifest or OMP settings.

DotAi stores only compact routing intent in `stack.json`: the detected provider set, selected primary, agent overrides, and usage/fallback policies. Expanded model routes stay in OMP. DotAi discovers availability from OMP without reading provider credentials, and it preserves unrelated OMP roles, fallback chains, agent overrides, extensions, and other settings.

Routing intent is saved and backed up before OMP settings are applied. If a setting command fails, successful settings and the saved intent remain; status exposes the resulting drift. Rerun `configure omp-routing` to apply the missing settings. Unchanged intent does not create another manifest backup, and a fully converged configuration performs no setting writes. Managed fallback flags must be JSON booleans and the usage reserve an integer; numerically equal values of the wrong type are drift and are corrected by explicit configuration.

If an existing manifest still contains static `ompRouting.roles`, run `./dotai.py configure omp-routing` to perform the backed-up, one-way migration to compact intent. Provider authentication changes appear as `DRIFT` while at least one persisted provider remains available; if none remains, `./dotai.py status` reports `INACTIVE`. Rerun `./dotai.py configure omp-routing` to refresh the persisted intent and managed OMP routes. Status is observational and never prompts or writes.

Credentials belong in environment variables or a secret manager, not in `stack.json` or version control.

## Portable personal stack

Version 2 manifests describe intent, not expanded installation scripts. Opt in to
reviewed components with, for example,
`{"name":"Graphify","recipe":"graphify","managed":true,"version":"0.4.2","updatePolicy":"pinned"}`.
Recipes `omp`, `rtk`, and `graphify` live in `component-recipes.json`; materialization
only changes an in-memory copy. Explicit command fields override recipe fields,
and a custom package without `recipe` is the escape hatch for a reviewed installer.
Neither recipe selection nor installed presence grants management permission:
package operations require explicit `managed:true`.

The default personal manifest is `%APPDATA%/DotAi/stack.json` on Windows, or
`$XDG_CONFIG_HOME/dotai/stack.json` (normally `~/.config/dotai/stack.json`) on Unix.
`DOTAI_CONFIG_DIR` overrides the directory. Resolving or inspecting these paths
does not create them. An explicit manifest path keeps its own adjacent lock:
`my-stack.json` uses `my-stack.lock.json`. The repository-local `stack.json` is
not moved or overwritten.

Prerequisites are checks only: Node/npm/npx, uv, Git, curl, Homebrew, Scoop, and
archive/checksum utilities must be installed by the user. Recipes select the
checks needed on the current platform. OMP uses Scoop on Windows, Homebrew on
macOS, and its installer/version-aware update on Linux. RTK keeps the reviewed
Linux 0.50.0 release with architecture-specific SHA-256 verification; Windows
and macOS use their package managers.

Exact Graphify pins use the supported [uv tool requirement syntax](https://docs.astral.sh/uv/concepts/tools/),
`uv tool install graphifyy==VERSION`, without `--force` that could overwrite
an unmanaged executable. RTK pins support only the reviewed Linux 0.50.0
release. Arbitrary RTK package-manager pins and OMP pins are rejected rather
than silently installing latest. Custom numeric pins require an explicit
`pinInstall` template containing `{version}`. Platform compatibility and pin
support are checked before installation.

## Reproducibility and observed locks

The manifest's `version` and `updatePolicy` are requested intent. The adjacent
lock's versions, revisions, installer version, and source trees are successful
observations, not guesses or rewritten intent. DotAi preflights the entire
selected plan before making a change. A missing mutable source without a lock
requires explicit `install` or `update`; `sync` cannot silently resolve latest.
An existing unpinned tool can establish a lock from its observed version without
upgrading it. Stale intent or changed catalog provenance requires explicit review
and install/update, not an automatic sync upgrade.
A valid lock is reused when installing a missing component on another machine
or repairing an owned copy. A healthy latest-policy installation retains its
current observed version, even when newer than a transferred lock; explicit
update is the operation that advances source resolution. Exact pinned targets
remain authoritative. Force-install honors valid resolved targets rather than
substituting a newer release. Healthy unchanged installations need no network
resolution.

Reviewed recipes may declare source metadata endpoints and JSON field paths.
Before a needed installation/update, DotAi resolves the actual source version
and verifies its declared Python requirement against the interpreter running
DotAi. Numeric comparison constraints (`>=`, `>`, `<=`, `<`, `==`, `!=`, including
comma-separated combinations) are supported; other requirement grammars fail
visibly, not as assumed compatibility. The execution copy uses the resolved
exact installer path. Cached, previously resolved requirements can be reused
for locked reinstalls without looking up latest.

Skills resolve public GitHub repository roots to immutable 40-character commits
and complete selected-folder tree hashes through the GitHub API. Retrieval errors,
truncated trees, ambiguous folders, and wildcard/unverified selections refuse
resolution rather than fall back to latest. `revision` may declare a ref that is
resolved before execution; the execution copy carries an immutable revision and
an exact `installerVersion`. The supported upstream [skills source parser](https://github.com/vercel-labs/skills/blob/958f4b7389ba698b0a6a26a1e505ae2af82364d2/src/source-parser.ts)
accepts GitHub tree refs, and its [Git implementation](https://github.com/vercel-labs/skills/blob/958f4b7389ba698b0a6a26a1e505ae2af82364d2/src/git.ts)
can fetch a full commit SHA. [npm's exact package invocation](https://docs.npmjs.com/cli/v11/commands/npx/)
selects `skills@VERSION`; immutable commit support requires the reviewed 1.7.1
installer or newer and its declared Node engine prerequisite. Older installers
are rejected. A healthy unchanged locked sync reuses existing immutable facts
without looking up mutable HEAD or the latest installer again.

Explicit skill-refresh requests and accepted recommendation changes can advance
only their selected sources during a sync. Existing copies must first pass
current source/agent ownership proof (including full folder hashes or a valid
machine-local adoption receipt) before target lookup and replacement. A receipt
does not supply an invented revision: target commit and trees are independently
resolved, then matched against successful installed observations. The reviewed
execution plan is frozen across manifest review/write/reconciliation so a
moving upstream HEAD cannot silently change the approved target.

After success, actual tool versions, actual installer version, selected installed
skill trees plus upstream v3 source metadata, and scoped plugin registry records
must match the prepared resolution. Local modifications are drift, not new
resolution. Plugin locks contain observed registry version, Git commit when
available, and installed tree. OMP's marketplace plugin installer does not expose
an exact-version override: a missing locked plugin or mismatched explicit pin
fails before changes instead of installing latest.
Plugin tree observations are confined to OMP's user cache
`~/.omp/plugins/cache/plugins`, including project-scope entries whose registry
lives under the current directory. Redirected cache roots, outside paths, and
ambiguous shared ID/scope references refuse lock provenance before reading tree
contents. Only the trusted configured-home prefix is canonicalized; symlinks
below that prefix are not treated as independent installed copies.

Locks are private atomic sidecars (mode `0600` on POSIX), ignored by default.
Dry runs, failed operations, unobservable versions, and provenance failures never
replace the lock. A changed preflight snapshot or concurrent reconciliation
refuses execution before component actions. Applied actions and recording hold
one exclusive stack lease; retry after the other reconciliation finishes.
Selected updates replace only selected records, retaining unrelated records.
An unchanged lock retains its exact bytes and modification time.

You may deliberately transfer a reviewed lock alongside its manifest: source
versions/revisions do not contain absolute home or repository paths. The recorded
platform is the observation's origin, not an ownership grant. Another machine
must re-observe its installation or use a reviewed exact installer for that
version/platform; unsupported platform pins fail explicitly. A tree/version lock
never grants uninstall rights. Machine-local adoption and ownership receipts
remain separate, and must be established independently on the destination.
