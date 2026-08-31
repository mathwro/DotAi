# Configuration

## Local stack configuration

`stack.example.json` is the version-controlled baseline for new users. `stack.json` is created automatically from it when `install`, `update`, `sync`, `status`, `doctor`, `validate`, or `add` first needs the default manifest.

The generated `stack.json` is ignored by Git. Pulling repository updates therefore cannot replace personal tools, skills, plugins, MCP servers, or credential references. Changes to `stack.example.json` affect new configurations automatically; existing users can opt into recommended skill changes with `./dotai sync --recommended-skills`.

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
- Recommended skill synchronization preserves user-added and locally modified sources, backs up `stack.json`, and removes installed files only for accepted retirements.
- Release checks run for `install`, `sync`, `status`, and `version`; an available newer release is shown as a warning, while network failures are ignored.
- Dry runs do not modify files or machine state.

## Managed OMP extensions

RTK 0.43 or newer is configured through `rtk init -g --agent pi`. This creates `~/.pi/agent/extensions/rtk.ts`, which DotAi appends to OMP's global extensions without removing user-configured entries. Restart OMP after the first installation; `dotai status` verifies both registration and source availability.

The Pi extension is independent of RTK's optional Codex integration. DotAi also enables OMP's **Hide Secrets** privacy setting (`secrets.enabled`) during installation and updates, so configured secrets are obfuscated before prompts are sent to providers.

## Configure OMP provider routing

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

Credentials belong in environment variables or a secret manager, not in `stack.json` or version control.
