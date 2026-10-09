# Configuration

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. Run `./dotai.py init` explicitly to create a missing manifest. `version`, `platform`, and help do not need a manifest; inspection and dry-run commands never create one.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations automatically; existing users can opt into recommended skill changes with `./dotai.py sync --recommended-skills`.

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
- Dry runs never initialize a manifest or modify existing files or managed machine state.

Backups retain the previous complete file, including any literal credentials already present. Newly created backups are private to the current user on POSIX, but DotAi does not delete historical backups: after rotating a credential, review and remove old `mcp.json.bak.*` and `stack.json.bak.*` copies yourself. Custom manifest names and backup paths inside other Git repositories need their own ignore rules; keep credentials as environment or secret-manager references rather than literals.

Manifest and reconciliation-state writes use temporary files before replacement. A failed copy, write, or replacement preserves the original file and removes newly created incomplete backup or temporary files; a completed backup remains available. An existing backup with the same timestamp is never overwritten. State files are updated individually, not as a multi-file transaction.

## Managed OMP extensions

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi appends to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `./dotai.py status` verifies both registration and source availability.

The Linux RTK commands in `stack.example.json` use the reviewed v0.50.0 binary archives and architecture-specific SHA-256 digests. Updating the pinned release requires updating its version and archive digests together; existing user-owned `stack.json` files never receive such baseline changes automatically. Windows and macOS continue to use Scoop and Homebrew.

The manifest declares RTK's `minimumVersion` as `0.43`. Package checks compare the command's reported `major.minor[.patch]` version from stdout or stderr; an older installed binary is upgraded, while a missing binary is installed. Status does not silently accept an unsupported or unparseable RTK. Other packages may declare the same optional constraint.

### Checks-only prerequisites and management permission

Version 2 separates managed components from external prerequisites. Package entries require `managed: true`; general dependencies such as Node.js/npm, `uv`, Python, Git, curl, and platform package managers cannot be managed packages. The obsolete `updateGroup` and `--include-dependencies` paths are removed.

Declare external requirements in `prerequisites` as `{name, check, minimumVersion?, hint?}`. Inert user metadata such as notes is preserved, but operation and management fields are forbidden: these objects accept checks only, never install, update, configure, or uninstall commands. A component's `requires` names the checks it needs (a list, or platform-keyed lists). Only enabled, selected components contribute requirements. DotAi checks the complete selected set before dependent mutations, reports missing or unsupported requirements with guidance, and refuses the run without attempting prerequisite repair.

`status` and `doctor` check only relevant prerequisites. An unused definition or an unselected platform manager does not make a stack unhealthy. Packages and integration entries can use `enabled: false` to exclude them from reconciliation and health checks.

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
