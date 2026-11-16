# Configuration

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. Any command that needs the default manifest initializes a missing `stack.json` once from that example; `version`, `platform`, and help do not need a manifest. This initialization also occurs before a first-use dry run. Run `./dotai.py validate` first to initialize and check the manifest separately from previewing changes.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations automatically; existing users can opt into full baseline changes (`packages`, `skills`, `mcp.servers`, and `ompExtensions`) with `./dotai.py install --recommended`, `./dotai.py update --recommended`, or `./dotai.py sync --recommended`, or review skills alone with `./dotai.py sync --recommended-skills`.

To recreate the defaults, first back up your local `stack.json`, then remove it and run:

```sh
./dotai.py validate
```

For a separate manifest, use `./dotai.py --manifest path/to/new-stack.json init`. Normal commands never initialize or overwrite an explicitly selected custom path, and `init` also refuses to overwrite an existing file.

`validate` checks required sections and supported package, skill, plugin, and MCP entry shapes before they can be applied, including valid HTTP(S) server URLs and port numbers. The same validation runs before initializing a manifest or saving changes from `add`, `remove`, recommended skill synchronization, skill migration, or routing configuration. Invalid additions or removals (for example, an MCP URL with an invalid port, a malformed `plugin@marketplace` ID, an empty tool check command, or a missing `remove` target) exit with code `2` without changing the existing manifest or creating backups; correct the input and retry. User-owned extra fields and credential references remain untouched, and unconfigured routing remains `null` when saved. See [`stack.schema.json`](../stack.schema.json) for the declarative format.

## Configuration safety

DotAi updates configuration conservatively:

- Unmanaged MCP servers and top-level settings are preserved.
- Existing manifest and MCP files receive private timestamped backups before a managed change, including `add` mutations that modify `stack.json`; unchanged additions create no redundant backup.
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
- Normal `install` and `sync` avoid skill fetches only with matching source metadata and a matching GitHub folder tree hash for the configured agent's installed files; the name-only global lock cannot establish ownership of another agent's independent copy. Missing or uncertain provenance triggers a refresh, not a silent skip. `update` always refreshes declared skill sources: a local lock match does not prove upstream freshness.
- The skills.sh lock is read from `$XDG_STATE_HOME/skills/.skill-lock.json` when set, otherwise from `~/.agents/.skill-lock.json`. Installed universal skills remain in `~/.agents/skills/`; DotAi adds no separate ownership database. See [skill refresh behavior and limits](extending.md#add-a-skill-source).
- Baseline recommendation synchronization (`--recommended` on `install`, `update`, or `sync`) tracks accepted baseline snapshots in `recommended-baseline.json` and `recommended-skills.json`, preserves user-added custom tools, custom MCP servers, custom extensions, and locally modified entries by default, backs up `stack.json` with private `0600` permissions on POSIX, and removes installed skill files only for accepted retirements. With `--recommended --enforce`, locally differing baseline entries are proposed for update to the exact `stack.example.json` recipes while custom user entries remain preserved. On `sync --recommended-skills --enforce`, an initial cleanup prompt can explicitly remove user-owned skill sources outside the recommendations; declining preserves them and skips their installation for that run before recommendation review continues.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network and cache I/O failures are ignored. Successful lookups are cached in `$DOTAI_STATE_DIR/release-check.json` (or the platform default state directory) for 24 hours during normal `install`, `sync`, and `status` runs; `version` always checks fresh upstream, dry runs do not write cache files, and setting `DOTAI_NO_UPDATE_CHECK=1` (or `true`) skips release checks and warnings entirely.
- After manifest initialization, dry runs do not modify existing files or managed machine state.

Backups retain the previous complete file, including any literal credentials already present. Newly created backups are private to the current user on POSIX, but DotAi does not delete historical backups: after rotating a credential, review and remove old `mcp.json.bak.*`, `.env.bak.*`, and `stack.json.bak.*` copies yourself (`./dotai.py doctor` shows a non-failing `INACTIVE` count when backup files accumulate alongside those targets). Custom manifest names and backup paths inside other Git repositories need their own ignore rules; keep credentials as environment or secret-manager references rather than literals.

Manifest and reconciliation-state writes use temporary files before replacement. A failed copy, write, or replacement preserves the original file and removes newly created incomplete backup or temporary files; a completed backup remains available. An existing backup with the same timestamp is never overwritten. State files are updated individually, not as a multi-file transaction.

## Managed OMP extensions

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi appends to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `./dotai.py status` verifies both registration and source availability.

The Linux RTK commands in `stack.example.json` use the reviewed v0.50.0 binary archives and architecture-specific SHA-256 digests. Updating the pinned release requires updating its version and archive digests together; existing user-owned `stack.json` files never receive such baseline changes automatically, and can review and adopt them explicitly with `./dotai.py update --recommended --enforce`. Windows and macOS continue to use Scoop and Homebrew.

The manifest declares RTK's `minimumVersion` as `0.43`. Package checks compare the command's reported `major.minor[.patch]` version from stdout or stderr; an older installed binary is upgraded, while a missing binary is installed. Status does not silently accept an unsupported or unparseable RTK. Other packages may declare the same optional constraint.

For packages with `updateGroup: "dependency"`, normal `update` leaves a present binary unchanged unless `--include-dependencies` is supplied, even when its version is below `minimumVersion` or cannot be parsed. This opt-in takes precedence over minimum-version upgrades during updates; missing dependencies are still installed. A skipped dependency with an unresolved minimum-version check remains unhealthy and causes reconciliation verification to fail. `install` still upgrades present packages below their minimum, and normal updates still upgrade core packages.

On Windows, command execution refreshes the machine and user `PATH` so newly installed shims are visible to later commands. Refreshing repeatedly preserves other inherited entries without accumulating another copy of the registry paths on each command.

The Pi extension is independent of RTK's optional Codex integration. DotAi also enables OMP's **Hide Secrets** privacy setting (`secrets.enabled`) during installation and updates, so configured secrets are obfuscated before prompts are sent to providers.

## Optional Graphify

The recommended stack installs the Graphify CLI, but does not install its Pi skill or generate project graphs automatically. Invoke `graphify extract . --code-only` in a project only when you want a graph; it writes `graphify-out/` there.

Existing `stack.json` files are user-owned and are not updated automatically. If your Graphify package still has a `configure` command that runs `graphify install --platform pi`, adopt the updated baseline recipe with `./dotai.py update --recommended --enforce` (preview first with `--dry-run`) before your next `install` or `update`. To retire the previously installed broad Pi skill, run `graphify pi uninstall` yourself. These steps leave the CLI available for explicit use.

## Skill environment defaults

Skill entries may declare `ompEnvironment`, an object of environment defaults for OMP child commands. Names must match `[A-Za-z_][A-Za-z0-9_]*`; values must be nonempty strings containing only letters, digits, `_`, `.`, `/`, `:`, or `-`. These are literal, non-secret settings, not shell expressions or credential storage.

During `install`, `update`, and `sync`, DotAi reconciles these defaults before installing skills, including when an installed skill needs no refresh. It also supplies them to the installer process. The persistent defaults live in a DotAi-managed block in `~/.omp/agent/.env`; unrelated content is preserved, existing files receive private timestamped backups before changes, and unchanged runs create no backup. Dry runs preview without writing. Conflicting declarations fail rather than choosing a source silently.

The recommended `modern-web-guidance` entry sets `DISABLE_TELEMETRY=1`. This disables its default-on search-query and usage telemetry for commands launched by a restarted OMP session. OMP preserves nonempty inherited environment variables: remove a contrary shell export such as `DISABLE_TELEMETRY=0` rather than expecting this default to override it. Direct CLI use outside OMP requires the same opt-out in that process's environment. The managed block is rebuilt from the active manifest; removing a declaration removes it from that block on the next reconciliation, without changing unrelated dotenv content.

When enforced recommendation sync skips preserved user-owned sources, it leaves their existing managed defaults intact and applies only active declarations. A later ordinary full sync rebuilds the block exactly from the manifest.

Keep the generated DotAi block last in the file. Reconciliation refuses malformed, duplicated, nonterminal, or symlinked managed files instead of rewriting ambiguous user content; resolve the reported conflict before retrying.

Modern Web Guidance requires Node.js 22 or newer and invokes its npm CLI at runtime; the skill installation alone does not establish that runtime requirement. Update an older Node installation explicitly using the platform's supported installation method; normal DotAi updates skip present dependency tools unless `--include-dependencies` is supplied. No experimental API opt-ins or Chrome Extensions skill are enabled by this recommendation.

## Doctor diagnostics

`./dotai.py doctor` runs every read-only `status` check and adds secret-safe prerequisite diagnostics:

- **Config target and package manager:** verifies the nearest existing ancestor of `mcp.target` is writable and checks the platform package manager (`scoop`, `brew`, `apt-get`, or `pacman` when applicable).
- **Interactive shell `PATH`:** on Linux, WSL, and macOS, reports `DRIFT` when `~/.local/bin` contains installed binaries (such as `rtk` or `uv`) while absent from the caller's shell `PATH`, warning that tools installed there are invisible outside DotAi.
- **Skill runtime and inherited environment conflicts:** checks `node --version` is 22.0 or newer when `modern-web-guidance` is declared (`FAIL` if missing or older), and reports `DRIFT` naming any declared `ompEnvironment` variable whose non-empty inherited shell value overrides `~/.omp/agent/.env` (without displaying environment values).
- **MCP environment readiness and backup hygiene:** checks boolean presence in the caller's environment for uppercase environment-variable references declared in MCP `headers` or `env` (without inspecting or logging secret values, and ignoring non-sensitive literals such as `LOG_LEVEL=warning`), and notes retained `*.bak.*` counts alongside `mcp.target`, `~/.omp/agent/.env`, and `stack.json` as non-failing `INACTIVE` reminders.

## Configure OMP provider routing

**Optional, post-authentication setup:** run the routing commands only after installing the stack, opening OMP for the first time, and authenticating at least one supported provider. DotAi does not perform provider login.

Routing is never enabled automatically by `install`, `update`, or `sync`. Until it is configured, `status` and `doctor` display a non-failing `INACTIVE` hint with the preview command; they do not inspect credentials or change OMP configuration.

1. Run `./dotai.py install` (or `python dotai.py install` on Windows).
2. Run `omp` to open OMP for the first time.
3. Inside OMP, use `/login` to authenticate GitHub Copilot, OpenAI Codex, Anthropic, or any combination of them, then return to your shell. See [OMP's provider authentication guide](https://github.com/can1357/oh-my-pi/blob/main/packages/ai/README.md#oauth-providers) for supported login flows. Authentication belongs to OMP, not to `stack.json`.
4. Only after those prerequisites, preview the detected providers, resolved roles, changed routing-intent fields, and pending OMP setting names (without dumping configuration values):

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

The tracked `routing-recommendations.json` selects the first available model in each role's ordered list. Recommendations refreshed on October 27, 2026:

| Provider | `default` | `task` | `smol` | `slow` |
| --- | --- | --- | --- | --- |
| GitHub Copilot | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| OpenAI Codex | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| Anthropic | Claude Opus 5.5 | Claude Sonnet 5.5 | Claude Haiku 5.5 | Claude Opus 5.5, high effort |

[GPT-6.1 Sol](https://openai.com/index/introducing-gpt-6-1-sol/) offers near-Astra coding capability at lower API cost; Astra remains the recommendation for the most demanding reasoning. [Opus 5.5](https://www.anthropic.com/claude-opus-5-5) precedes Fable 5.1 for `slow`; [Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5) remains the Anthropic worker recommendation. [Haiku 5.5, released October 7](https://www.anthropic.com/claude-haiku-5-5), now leads Anthropic `smol`, with Haiku 4.5 retained for restricted availability. These are curated role choices, not runtime price comparisons.

Copilot `task` retains both Sol recommendations ahead of [GPT-6.1 Terra, released on Copilot October 26](https://github.blog/changelog/2026-10-26-gpt-6-1-terra-in-github-copilot/), which precedes GPT-5.6 Terra. Terra targets well-scoped coding at lower token rates; adding a fallback does not reduce normal costs while Sol is selected. Copilot `smol` adds `github-copilot/claude-haiku-5.5` immediately after GPT-6 Luna. Haiku's higher pricing tier begins above 100k input tokens, versus Luna's 272k threshold, so Luna remains first pending workload-specific comparisons. See [Copilot pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing).

Codex no longer recommends `gpt-5.4-mini`, which [retired for ChatGPT-sign-in Codex on August 31](https://learn.chatgpt.com/docs/models#deprecated-codex-models); API-key access is unaffected. Although official Codex supports GPT-6.1 Terra, its OMP provider selector was not confirmed in the bundled catalog during this review, so adding it remains deferred until OMP discovery establishes availability.

Older recommendations remain fallbacks for staged rollouts or restricted subscriptions; unavailable models are omitted. Exact IDs follow OMP's [model catalog](https://github.com/can1357/oh-my-pi/blob/main/packages/catalog/src/models.json) and live `omp models --json` output, not display names (Anthropic uses `claude-opus-5-5` and `claude-sonnet-5-5`). After pulling recommendation updates, preview and rerun `./dotai.py configure omp-routing` to apply them; pulling alone does not change your manifest or OMP settings.

DotAi stores only compact routing intent in `stack.json`: the detected provider set, selected primary, agent overrides, and usage/fallback policies. Expanded model routes stay in OMP. DotAi discovers availability from OMP without reading provider credentials, and it preserves unrelated OMP roles, fallback chains, agent overrides, extensions, and other settings.

Routing intent is saved and backed up before OMP settings are applied. If a setting command fails, successful settings and the saved intent remain; status exposes the resulting drift. Rerun `configure omp-routing` to apply the missing settings. Unchanged intent does not create another manifest backup, and a fully converged configuration performs no setting writes. Managed fallback flags must be JSON booleans and the usage reserve an integer; numerically equal values of the wrong type are drift and are corrected by explicit configuration.

If an existing manifest still contains static `ompRouting.roles`, run `./dotai.py configure omp-routing` to perform the backed-up, one-way migration to compact intent. Provider authentication changes appear as `DRIFT` while at least one persisted provider remains available; if none remains, `./dotai.py status` reports `INACTIVE`. Rerun `./dotai.py configure omp-routing` to refresh the persisted intent and managed OMP routes. Status is observational and never prompts or writes.

Credentials belong in environment variables or a secret manager, not in `stack.json` or version control.
