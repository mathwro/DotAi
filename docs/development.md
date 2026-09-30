# Development

The runtime uses only the Python standard library and supports Python 3.10 or newer.

## Repository layout

```text
dotai.py             Cross-platform manager implementation
dotai                Unix command wrapper
dotai.ps1            PowerShell command wrapper
stack.example.json   Tracked baseline copied for new users
stack.json           Ignored, user-owned stack configuration
stack.schema.json    JSON Schema for stack manifests
routing-recommendations.json  Tracked provider/model routing recommendations
docs/                User and contributor documentation
tests/               Behavioral test suite
AGENTS.md             Repository guidance for coding agents
```

The public launchers, manifests, and schema stay at the repository root because installation and operational commands address them there directly.

The current user-facing contracts live in [Configuration](configuration.md) and [Extending the stack](extending.md). Dated files under `docs/superpowers/specs/` are historical design records, not current defaults or active instructions. Completed implementation plans have been removed; their history remains in Git.

## Verification

Run the behavioral suite:

```sh
rtk python3 -m unittest discover -s tests -v
```

Validate the tracked example and Unix launcher without initializing a personal manifest:

```sh
rtk python3 dotai.py --manifest stack.example.json validate
rtk sh -n dotai
```

To check a local installation, `rtk python3 dotai.py validate` validates `stack.json` and initializes it from the example only if absent. First-use initialization also happens before dry-run previews.

When changing the manifest format or shared defaults, keep `stack.example.json`, `stack.schema.json`, CLI mutation commands, and runtime validation aligned. Never commit a generated `stack.json`. Behavioral changes should include a focused test that protects the user-visible contract.

Routing behavior tests use the shared synthetic catalog in `tests/test_dotai.py`, not current GPT or Claude versions. Keep expected provider precedence, fallback chains, and configuration effects explicit; do not calculate expected results with the production resolver. The repository catalog is validated separately and exercised for coverage of every managed role without pinning model IDs. Model refreshes should update `routing-recommendations.json` and the routing documentation, not the behavioral fixtures.

## Behavioral fixtures and CLI smoke checks

For package-presence or minimum-version tests, use isolated commands that report known versions and exit codes. Mocking only `package_check` as false does not simulate a missing binary: reconciliation can separately probe command success to distinguish missing from outdated packages.

Healthy-skill refresh fixtures need complete skills.sh v3 source metadata and an independently verified Git tree hash for the configured agent's entire installed folder, including supporting files and executable modes. Isolate `XDG_STATE_HOME` and `GH_HOST`; the authoritative lock path and source host affect ownership. Source-only locks or a matching `SKILL.md` do not prove ownership of an independent agent copy.

Exercise documented mutation commands against an initialized, explicit temporary manifest. Isolate `HOME`, `DOTAI_HOME`, `DOTAI_STATE_DIR`, and `XDG_STATE_HOME` when testing external tools or configuration writes; preserve personal manifests, credentials, and OMP settings. Follow additions with `validate` and the appropriate preview: `sync --dry-run` for skills/MCP/plugins, or `install --dry-run` for packages.

Routing smoke checks require OMP to be installed, opened at least once, and authenticated to a supported provider. Preview against an existing test manifest, not the personal `stack.json`:

```sh
rtk python3 dotai.py configure omp-routing --help
rtk python3 dotai.py --manifest path/to/test-stack.json configure omp-routing --dry-run
```

The preview should show discovered providers, available role primaries, fallback chains, the manifest diff, and pending OMP commands without applying them. Missing providers or genuinely unhealthy components can produce nonzero exits; do not suppress those health failures.
