# Configuration

## Local stack configuration

`stack.example.json` is an empty version-2 baseline. Run `./dotai.py init` explicitly to create your personal manifest at the user configuration location described under [Portable personal stack](#portable-personal-stack). `version`, `platform`, and help do not need a manifest; inspection and dry-run commands never create one.

No packages, harnesses, skills, plugins, extensions, or MCP servers are selected automatically. Personal intent is separate from repository defaults and is not overwritten by pulls. The old repository-local `stack.json` remains untouched. Skill suggestions live in `skill-recommendations.json`, not the baseline, and require explicit review with `./dotai.py sync --recommended-skills`.

To create a separate manifest, use:

```sh
./dotai.py --manifest path/to/new-stack.json init
```

`init` refuses to overwrite an existing file. To recreate a manifest, first move the previous configuration to a safe backup and explicitly initialize the desired path.

`validate` checks required sections and supported package, skill, plugin, and MCP entry shapes before they can be applied, including valid HTTP(S) server URLs and port numbers. The same validation runs before initializing a manifest or saving changes from `add`, recommended skill synchronization, skill migration, or routing configuration. Invalid additions (for example, an MCP URL with an invalid port, a malformed `plugin@marketplace` ID, or an empty tool check command) exit with code `2` without changing the existing manifest or creating backups; correct the input and retry. User-owned extra fields and credential references remain untouched, and unconfigured routing remains `null` when saved. See [`stack.schema.json`](../stack.schema.json) for the declarative format.

## Convert version-1 manifests

Normal commands accept version 2 only. Conversion is a separate, one-way, file-only operation:

```sh
./dotai.py --manifest path/to/legacy-stack.json convert --dry-run
./dotai.py --manifest path/to/legacy-stack.json convert
```

The preview identifies general prerequisites whose installation, update, and configuration commands will be retired, and packages whose management permission needs review. Existing skills, marketplaces, plugins, extensions, routing, MCP entries (including credential references), custom component commands, and user metadata are preserved. Unknown/custom component ownership is never inferred from installed presence.

The interactive command asks permission for each retained package, then confirms the complete write. For automation, use repeatable `--manage NAME` to authorize the reviewed package names and `--yes` to confirm; `--yes` alone cannot grant custom ownership. Declining review leaves the complete original untouched.

Use `--destination path/to/new-stack.json` to preserve the source file and write converted intent to a new location. The destination must not exist (except when it is the source itself). After validation and confirmation, conversion makes a timestamped exact source backup, private (`0600`) on POSIX, then writes the converted manifest. Preview, refused ownership, invalid input, and destination conflicts detected before writing create no backup, directories, or environment changes. A destination created concurrently during the write is never overwritten; a completed source backup may remain after that refusal. No component commands, package manager operations, OMP lookups, or lock creation occur during conversion.

Legacy static routing roles remain intact for the separate explicit `configure omp-routing` migration; conversion does not discover providers or configure routing.

## Select optional components

```sh
# A fresh manifest may select zero or several reviewed recipes:
./dotai.py --manifest path/to/new-stack.json init --component graphify
# Add a recipe to an existing manifest, without installing it yet:
./dotai.py add component rtk
./dotai.py add component omp
./dotai.py install --dry-run
```

Selections save portable intent (`name`, `recipe`, `managed: true`, and optional version policy), not expanded installer commands. Catalog commands and checks-only prerequisite metadata are materialized only in memory. Custom tools remain declarative `add tool` entries, with explicit check/install commands and repeatable `--requires NAME` checks.

OMP, RTK, and Graphify are independent optional selections. No OMP lookup, manager check, or configuration-target check runs for omitted integrations. Component identity is unique by tool name, skill source plus agent, marketplace name, plugin ID plus scope, and semantic extension path; ambiguous duplicates are rejected before applying anything.

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
- Normal sync retains proven existing skill sources. Unknown provenance, local changes, or incomplete proof produce actionable `DRIFT`, never automatic replacement.
- The skills.sh lock is read from `$XDG_STATE_HOME/skills/.skill-lock.json` when set, otherwise from `~/.agents/.skill-lock.json`. Universal skills remain in `~/.agents/skills/`. Immutable resolutions belong in the adjacent private stack lock; machine-local scoped ownership belongs in `component-receipts.json`. Neither is interchangeable with the upstream installer lock.
- Recommended skill synchronization preserves user-added and locally modified sources by default, backs up `stack.json`, and removes installed files only for accepted retirements. With `--enforce`, an initial cleanup prompt can explicitly remove user-owned sources outside the recommendations; declining preserves them and skips their installation for that run before recommendation review continues.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network failures are ignored.
- Dry runs never initialize a manifest or modify existing files or managed machine state.

Backups retain the previous complete file, including any literal credentials already present. Newly created backups are private to the current user on POSIX, but DotAi does not delete historical backups: after rotating a credential, review and remove old `mcp.json.bak.*` and `stack.json.bak.*` copies yourself. Custom manifest names and backup paths inside other Git repositories need their own ignore rules; keep credentials as environment or secret-manager references rather than literals.

Manifest and reconciliation-state writes use temporary files before replacement. A failed copy, write, or replacement preserves the original file and removes newly created incomplete backup or temporary files; a completed backup remains available. An existing backup with the same timestamp is never overwritten. State files are updated individually, not as a multi-file transaction.

## Managed OMP extensions

RTK 0.43 or newer can be configured through `rtk init -g --agent pi`. The reviewed recipe runs this command only when the selected, enabled OMP extension is explicitly `~/.pi/agent/extensions/rtk.ts`; installing RTK alone does not write Pi configuration. DotAi appends the selected extension to OMP without removing user-configured entries. Restart OMP after the first configuration; `./dotai.py status` verifies both registration and source availability.

The RTK recipe uses reviewed v0.50.0 release archives and architecture-specific SHA-256 digests: Linux and macOS install only the standalone binary, while Windows uses a minimal local Scoop manifest. Updating the reviewed release requires changing its version and digests together; existing user-owned manifests never receive such changes automatically. No RTK recipe updates Homebrew, Scoop, their buckets, or unrelated packages.

Recipes may declare `minimumVersion` constraints. Package checks compare the reported numeric `major.minor[.patch]` version from stdout or stderr. Effective exact versions must match as well: an installed newer version does not satisfy a requested older pin. Missing or unparseable versions remain unhealthy.

## Checks-only prerequisites and management permission

Version 2 separates managed components from external prerequisites. Package entries require `managed: true`; general dependencies such as Node.js/npm, `uv`, Python, Git, curl, and platform package managers cannot be managed packages. The obsolete `updateGroup` and `--include-dependencies` paths are removed.

Declare external requirements in `prerequisites` as `{name, check, minimumVersion?, hint?}`. Inert user metadata such as notes is preserved, but operation and management fields are forbidden: these objects accept checks only, never install, update, configure, or uninstall commands. A component's `requires` names the checks it needs (a list, or platform-keyed lists). Only enabled, selected components contribute requirements. DotAi checks the complete selected set before dependent mutations, reports missing or unsupported requirements with guidance, and refuses the run without attempting prerequisite repair.

`status` and `doctor` check only relevant prerequisites. An unused definition or an unselected platform manager does not make a stack unhealthy. Packages and integration entries can use `enabled: false` to exclude them from reconciliation and health checks.

Repeated `--only kind:id` selections (including `tool`) filter the plan before recipe resolution and prerequisite preflight. Unrelated components and their requirements do not block the selected operation. Inspection with `status`, `list`, or `show` uses existing observations and never resolves mutable upstream source versions or revisions.

Operation-specific `updateRequires` checks apply only when the selected package will actually use its updater. A healthy normal installation does not run updater checks; `install --force` deliberately selects the installer instead, permitting a reviewed standalone cutover without modifying an old package-manager-owned copy. A missing unsafe-updater requirement blocks the complete selected run before any dependent changes.

`configureWhen` is a declarative field map: expected selections must be a subset of enabled actual selections before a package's configure commands run. RTK's extension condition is one catalog example, not special-case Python logic. Disabled extension objects do not satisfy it.

On Windows, command execution refreshes the machine and user `PATH` so newly installed shims are visible to later commands. Refreshing repeatedly preserves other inherited entries without accumulating another copy of the registry paths on each command.

The Pi extension is independent of RTK's optional Codex integration. When OMP itself is explicitly managed, its recipe enables **Hide Secrets** (`secrets.enabled`) during installation and updates; a stack without OMP never writes this setting.

## Optional Graphify

Graphify is selected explicitly with `add component graphify` (or `init --component graphify`). Its recipe manages only the CLI, not its Pi skill or project graphs. Supply the reported compatible Python and `uv` prerequisites externally; DotAi never bootstraps or updates either interpreter or installer. Invoke `graphify extract . --code-only` in a project only when you want a graph; it writes `graphify-out/` there.

Existing `stack.json` files are user-owned and are not updated from `stack.example.json` by `sync` or `sync --recommended-skills`. If your Graphify package still has a `configure` command that runs `graphify install --platform pi`, remove that command from your local manifest before your next `install` or `update`. To retire the previously installed broad Pi skill, run `graphify pi uninstall` yourself. These steps leave the CLI available for explicit use.

## Configure OMP provider routing

**Optional, post-authentication setup:** run the routing commands only after installing the stack, opening OMP for the first time, and authenticating at least one supported provider. DotAi does not perform provider login.

Routing is never enabled automatically by `install`, `update`, or `sync`. A selected OMP integration without routing receives a non-failing `INACTIVE` hint; a stack without OMP integrations does not inspect OMP or display routing guidance. Status does not inspect credentials or change OMP configuration.

1. Explicitly select OMP with `./dotai.py add component omp`, then run `./dotai.py install` (use `python dotai.py` on Windows).
2. Run `omp` to open OMP for the first time.
3. Inside OMP, use `/login` to authenticate GitHub Copilot, OpenAI Codex, Anthropic, or any combination of them, then return to your shell. See [OMP's provider authentication guide](https://github.com/can1357/oh-my-pi/blob/main/packages/ai/README.md#oauth-providers) for supported login flows. Authentication belongs to OMP, not to `stack.json`.
4. Only after those prerequisites, preview the detected providers, resolved roles, proposed intent changes, and pending actions in prose:

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
Custom install/pin commands do not inherit a catalog installer's source-resolution
metadata or supported-version restrictions. Declare `versionMetadata` explicitly
when that resolver also applies to the custom installer. An overridden installer
needs its own reviewed `pinInstall` to support exact replay; DotAi does not replace
it silently with the catalog installer.
Neither recipe selection nor installed presence grants management permission:
package operations require explicit `managed:true`; version 2 rejects `managed:false`
or missing permission. Disabled or unselected recipe intent remains inert, so its
unknown recipe or unsupported desired pin cannot block unrelated selected
components. Explicit uninstall resolves the removal recipe independently of a
desired installation pin.

The default personal manifest is `%APPDATA%/DotAi/stack.json` on Windows
(falling back to `~/AppData/Roaming/DotAi/stack.json` when `APPDATA` is unset),
or `$XDG_CONFIG_HOME/dotai/stack.json` (normally `~/.config/dotai/stack.json`)
on Unix. `DOTAI_CONFIG_DIR` overrides the directory; `DOTAI_HOME` overrides
the home used by DotAi. Resolving or inspecting paths does not create them.
An explicit manifest keeps its adjacent lock: `my-stack.json` uses
`my-stack.lock.json`. `DOTAI_STATE_DIR` overrides machine-local state storage.
The repository-local `stack.json` is not moved or overwritten.

Prerequisites are checks only: Node/npm/npx, uv, Python, Git, curl, Homebrew,
Scoop, and archive/checksum utilities must be installed by the user. Recipes
select only the checks needed on the current platform; they never repair or
update an external runtime or package manager.

On Linux and macOS, fresh or missing OMP installations and reviewed owned
`install --force` replacements use an exact GitHub release artifact and SHA-256
digest frozen during preparation. DotAi's `packages.install_native_release`
downloads to a sibling staging directory, verifies the digest and staged
`--version`, then atomically replaces `~/.local/bin/omp`. Redirected destinations
are refused. DotAi does not bootstrap OMP's official installer or use a Bun/source
fallback.

Existing latest-policy OMP installations use guarded `omp update --stable` on all
platforms. The explicit stable channel matches the reviewed release policy; plugin
updates are not requested. This vendor updater selects latest at execution, not a
frozen exact target, so the preflight available release can differ. DotAi records
the actual numeric version and its release metadata after success, verifying that
the installed binary matches that release's artifact digest.
An already matching exact requested or locked target is unchanged; a differing
target refuses native update and directs you to review an owned
`install --force` replacement, never an installer fallback. This includes a
resolved lock retained for latest-policy intent. The checks-only `updateRequires` guard rejects
manager-routed launchers before the updater can mutate an external manager.

Windows installs OMP as Scoop app `dotai-omp` using the resolved exact release's
native executable and SHA-256 digest. RTK uses reviewed release 0.50.0;
its Windows artifact is x64 only. No foreign bucket, hooks, or install scripts
are supplied. [`scoop download`](https://github.com/ScoopInstaller/Scoop/blob/master/libexec/scoop-download.ps1)
verifies the transient local manifest before a scoped RTK replacement.
[`scoop install`](https://github.com/ScoopInstaller/Scoop/blob/master/libexec/scoop-install.ps1)
uses `--independent --no-update-scoop`; download also uses `--no-update-scoop`.
Existing app manifests and directories are checked before uninstalling: hooks,
foreign binary aliases, and redirected paths refuse the cutover. Uninstall never
uses `--purge`; persisted data and unrelated apps are preserved. A replacement
failure after uninstall is reported as partial completion, not a successful
upgrade or an automatic rollback.

RTK's [reviewed 0.50.0 checksums](https://github.com/rtk-ai/rtk/releases/download/v0.50.0/checksums.txt)
cover x64/arm64 Linux and macOS archives. These recipes install only
`~/.local/bin/rtk`, with no Homebrew update, dependencies, or cleanup.
Exact RTK pins support only this reviewed release on supported architectures.
OMP pins require an available exact release and verified artifact for the selected
platform/architecture; unsupported or unavailable pins fail before installation.

Exact Graphify pins use the supported [uv tool requirement syntax](https://docs.astral.sh/uv/concepts/tools/):
`uv tool install --python CURRENT_CLI_PYTHON --no-python-downloads graphifyy==VERSION`.
The running DotAi interpreter must satisfy the source's declared Python
requirement; DotAi does not download another interpreter. No `--force` is used
to overwrite an unmanaged executable. Recipe `versionMetadata` declares the
PyPI latest/exact endpoints and JSON field paths consumed by the lock resolver,
not a second catalog networking path. Custom numeric pins require an explicit
`pinInstall` template containing `{version}`. Compatibility and pin support
are checked before installation.

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
visibly, not as assumed compatibility. Exact installation paths use the resolved
target; cached requirements can be reused for locked reinstalls without looking
up latest. Existing latest-policy OMP native updates are the exception: the vendor
updater chooses latest at execution and successful recording resolves metadata
for the actual observed release, not necessarily the preflight available release.

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
