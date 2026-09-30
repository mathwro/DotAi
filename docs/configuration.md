# Configuration

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. `stack.json` is created automatically from it when `install`, `update`, `sync`, `status`, `doctor`, `validate`, or `add` first needs the default manifest.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations automatically; existing users can opt into recommended skill changes with `./dotai sync --recommended-skills`.

To recreate the defaults, remove the local `stack.json` and run:

```sh
./dotai validate
```

For a separate manifest, use `./dotai --manifest path/to/new-stack.json init`. Normal commands never initialize or overwrite an explicitly selected custom path, and `init` also refuses to overwrite an existing file.

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
- Managed `ompExtensions` are appended to OMP's global extension list; unrelated user extensions are retained.
- Skill health is agent-scoped. A skill found only in a Codex plugin cache is reported as `INACTIVE` until installed for the configured OMP skill target.
- Avoiding skill fetches requires matching source metadata and a matching GitHub folder tree hash for the configured agent's installed files; the name-only global lock cannot establish ownership of another agent's independent copy. Missing or uncertain provenance triggers a refresh, not a silent skip.
- The skills.sh lock is read from `$XDG_STATE_HOME/skills/.skill-lock.json` when set, otherwise from `~/.agents/.skill-lock.json`. Installed universal skills remain in `~/.agents/skills/`; DotAi adds no separate ownership database. See [skill refresh behavior and limits](extending.md#add-a-skill-source).
- Recommended skill synchronization preserves user-added and locally modified sources, backs up `stack.json`, and removes installed files only for accepted retirements.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network failures are ignored.
- Dry runs do not modify files or machine state.

Backups retain the previous complete file, including any literal credentials already present. Newly created backups are private to the current user on POSIX, but DotAi does not delete historical backups: after rotating a credential, review and remove old `mcp.json.bak.*` and `stack.json.bak.*` copies yourself. Custom manifest names and backup paths inside other Git repositories need their own ignore rules; keep credentials as environment or secret-manager references rather than literals.

## Managed OMP extensions

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi appends to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `dotai status` verifies both registration and source availability.

The Linux RTK commands in `stack.example.json` use the reviewed v0.50.0 binary archives and architecture-specific SHA-256 digests. Updating the pinned release requires updating its version and archive digests together; existing user-owned `stack.json` files never receive such baseline changes automatically. Windows and macOS continue to use Scoop and Homebrew.

The manifest declares RTK's `minimumVersion` as `0.43`. Package checks compare the command's reported `major.minor[.patch]` version from stdout or stderr; an older installed binary is upgraded, while a missing binary is installed. Status does not silently accept an unsupported or unparseable RTK. Other packages may declare the same optional constraint.

For packages with `updateGroup: "dependency"`, normal `update` leaves a present binary unchanged unless `--include-dependencies` is supplied, even when its version is below `minimumVersion` or cannot be parsed. This opt-in takes precedence over minimum-version upgrades during updates; missing dependencies are still installed. A skipped dependency with an unresolved minimum-version check remains unhealthy and causes reconciliation verification to fail. `install` still upgrades present packages below their minimum, and normal updates still upgrade core packages.

The Pi extension is independent of RTK's optional Codex integration. DotAi also enables OMP's **Hide Secrets** privacy setting (`secrets.enabled`) during installation and updates, so configured secrets are obfuscated before prompts are sent to providers.

## Optional Graphify

The recommended stack installs the Graphify CLI, but does not install its Pi skill or generate project graphs automatically. Invoke `graphify extract . --code-only` in a project only when you want a graph; it writes `graphify-out/` there.

Existing `stack.json` files are user-owned and are not updated from `stack.example.json` by `sync` or `sync --recommended-skills`. If your Graphify package still has a `configure` command that runs `graphify install --platform pi`, remove that command from your local manifest before your next `install` or `update`. To retire the previously installed broad Pi skill, run `graphify pi uninstall` yourself. These steps leave the CLI available for explicit use.

## Configure OMP provider routing

After installation, configure routing from the providers already authenticated in OMP:

Routing is optional and never enabled automatically. Until it is configured, `status` and `doctor` display a non-failing `INACTIVE` hint with the preview command; they do not inspect credentials or change OMP configuration.

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

The tracked `routing-recommendations.json` selects the first available model in each role's ordered list. Recommendations refreshed on September 30, 2026:

| Provider | `default` | `task` | `smol` | `slow` |
| --- | --- | --- | --- | --- |
| GitHub Copilot | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| OpenAI Codex | GPT-6.1 Sol | GPT-6.1 Sol | GPT-6 Luna | GPT-6 Astra, high effort |
| Anthropic | Claude Opus 5.5 | Claude Sonnet 5.5 | Claude Haiku 4.5 | Claude Opus 5.5, high effort |

[GPT-6.1 Sol](https://openai.com/index/introducing-gpt-6-1-sol/) offers near-Astra coding capability at lower API cost; Astra remains the recommendation for the most demanding reasoning. [Opus 5.5](https://www.anthropic.com/claude-opus-5-5) replaces Opus 5 for interactive work and precedes Fable 5.1 for `slow`; [Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5) replaces Sonnet 5 for workers. Haiku 5.5 is not yet released, so the small Anthropic role remains on Haiku 4.5. These are curated role choices, not runtime price comparisons.

Older recommendations remain fallbacks for staged rollouts or restricted subscriptions; unavailable models are omitted. Exact IDs follow OMP's [model catalog](https://github.com/can1357/oh-my-pi/blob/main/packages/catalog/src/models.json) and live `omp models --json` output, not display names (Anthropic uses `claude-opus-5-5` and `claude-sonnet-5-5`). After pulling recommendation updates, preview and rerun `./dotai configure omp-routing` to apply them; pulling alone does not change your manifest or OMP settings.

DotAi stores only compact routing intent in `stack.json`: the detected provider set, selected primary, agent overrides, and usage/fallback policies. Expanded model routes stay in OMP. DotAi discovers availability from OMP without reading provider credentials, and it preserves unrelated OMP roles, fallback chains, agent overrides, extensions, and other settings.

If an existing manifest still contains static `ompRouting.roles`, run `./dotai configure omp-routing` to perform the backed-up, one-way migration to compact intent. Provider authentication changes appear as `DRIFT` while at least one persisted provider remains available; if none remains, `dotai status` reports `INACTIVE`. Rerun `./dotai configure omp-routing` to refresh the persisted intent and managed OMP routes. Status is observational and never prompts or writes.

Credentials belong in environment variables or a secret manager, not in `stack.json` or version control.
