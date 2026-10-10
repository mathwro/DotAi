# Development

The runtime uses only the Python standard library and supports Python 3.10 or newer.

## Repository layout

```text
dotai.py             Executable Python CLI entry point
dotai_app/           Purpose-specific application modules
stack.example.json   Empty version-2 baseline for explicit init
stack.json           Personal intent in the user config directory; checkout copy remains untouched
stack.schema.json    JSON Schema for stack manifests
routing-recommendations.json  Tracked provider/model routing recommendations
docs/                User and contributor documentation
tests/               Behavioral test suite
AGENTS.md             Repository guidance for coding agents
```

The Python entry point, manifests, and schema stay at the repository root because installation and operational commands address them there directly.

`dotai.py` delegates to `dotai_app.cli.main`. Its executable mode is tracked in Git: use `./dotai.py` on Unix or `python dotai.py` on Windows. Direct Unix execution requires `python3`; use `python dotai.py` if your Python 3.10+ interpreter is named `python` instead. On Windows, `py -3 dotai.py` or `python3 dotai.py` can be used when those commands provide the compatible interpreter. Running from a checkout needs no package installation.

| Module in `dotai_app/` | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing and command dispatch |
| `runtime.py` | Repository/home/state paths, platform selection, shared errors, and external command execution |
| `terminal.py` | Color policy, status formatting, credential redaction, and human-readable change previews |
| `releases.py` | Application version and best-effort release notices |
| `manifest.py` | Manifest validation, initialization, diffs, backups, and safe JSON writes |
| `integrations.py` | Parse and persist declared integrations from `add` commands |
| `prerequisites.py` | Read-only presence and compatibility checks for selected external prerequisites |
| `catalog.py` | Maintained recipes, effective command resolution, and frozen native artifact binding without rewriting personal intent |
| `portable.py` | Per-user manifest and adjacent stack-lock paths, without initialization |
| `conversion.py` | Explicit reviewed legacy-manifest conversion, backups, and management permission |
| `locking.py` | Immutable skill/npm/tool target preparation, exact private sidecars, concurrency lease, and verified observations |
| `lifecycle.py` | Scoped inspection/selectors, machine-local ownership receipts, adoption, activation, forgetting, and owned uninstall |
| `packages.py` | Package checks, install/update decisions, and verified staged native release installation |
| `skills.py` | Agent-scoped installation, ownership checks, health, retirement, and legacy migration candidate planning |
| `recommendations.py` | Plan, review, and apply recommended skill changes |
| `state.py` | Reconciliation history and recommended-skill ownership persistence |
| `omp.py` | OMP marketplaces, plugins, extension registration, and registry checks |
| `routing.py` | Provider/model policy, availability, explicit routing configuration, and routing health |
| `mcp.py` | Provider discovery, semantic server matching, health, and reconciliation |
| `health.py` | Aggregate read-only status and doctor diagnostics |
| `reconcile.py` | Coordinate selected package/integration reconciliation and confirmed legacy skill migration (`fix`) |

Keep dependencies directed: CLI dispatch and orchestration call the domain modules; domain modules use manifest, runtime, and terminal helpers without importing the CLI or orchestration. Use explicit module imports rather than a package-wide re-export facade. Tests import and patch the module that owns a behavior, so the same patch applies to its callers.

The current user-facing contracts live in [Configuration](configuration.md) and [Extending the stack](extending.md). Dated files under `docs/superpowers/specs/` are historical design records, not current defaults or active instructions. Completed implementation plans have been removed; their history remains in Git.

The [personal-stack roadmap](roadmap.md) records implemented workstreams and remaining verification, not a substitute for current command contracts.

## Command output conventions

DotAi owns the user-facing output. Normal commands describe the component and operation in prose, not exact shell commands, JSON protocols, raw configuration values, or upstream installer chatter. Print progress before starting a subprocess and flush it so a long operation is not silently waiting. Failures identify the operation, retain its nonzero exit status, give a bounded available cause, and offer a concrete next step. An optional probe failure may avoid the required-failure list, but must not be presented as successful work.

`runtime.Runner.run(command, label, *, capture=False, required=True, interactive=False)` retains its `CompletedProcess` return contract. Noninteractive execution always captures **separate** `stdout` and `stderr`, including when `capture=False`, and closes stdin. Protocol consumers parse the original returned streams; sanitizing display must never rewrite the captured result. The compatibility `capture` argument does not opt into printing. Use `interactive=True` only when an operation explicitly requires terminal input; this inherits terminal streams and cannot be combined with `capture=True`. Interactive upstream output is not subject to DotAi's capture/redaction, so do not pass credentials to interactive tools or claim that their own terminal output is sanitized.

Only `--verbose` displays sanitized commands and bounded diagnostic lines. Structured JSON responses and JSON configuration arguments are described as captured/structured content instead of dumped, even in verbose mode. `Runner.output` returns successful probe stdout without printing raw protocol responses; failed probes return an empty string. `Runner.succeeds` returns command success without counting observation as a mutation.

Use `terminal.redact(text, secrets=())` at output boundaries. It hides declared secret-bearing environment values, supplied known secrets, common credential flags/assignments, authorization and cookie headers, and URL passwords. Runner also collects credentials from command/header/environment values so echoed credentials are masked. This is display protection for known credentials and recognizable syntax, not a promise to discover arbitrary unlabeled secrets. Do not put credentials in labels or logs; declare secret-bearing values explicitly and keep raw results internal.

Use `terminal.describe_changes(before, after, target)` for previews. It returns prose naming added/removed components and changed fields, without rendering configuration values; an unchanged candidate returns an empty string. Recommendations and routing describe proposed actions, preserve unmanaged configuration, and state that dry runs apply nothing. Cleanup-dependent recommendations must be explicitly conditional on the separate cleanup approval, not described as already accepted.

`Runner.record_outcome(label, status, detail='')` records `changed`, `unchanged`, `skipped`, `failed`, or `planned`; `Runner.summary()` prints and returns concise numeric counts, including partial completion. Successful run operations record `changed`, failures record `failed`, and dry runs record `planned`. Multi-step orchestration accounts for domain decisions such as unchanged/disabled components and invokes the summary once. Simple manifest-only `init`, `add`, and `convert` commands use clear result lines instead of synthetic action counts. Probe commands do not add mutation outcomes.

Use `Runner.fail(label, cause, *, required=True, command=None)` for operational failures so the exit-status list, sanitized cause, and failed-action accounting agree. Ordered `runtime.run_steps` stops after a failed required step and records later steps as skipped; it does not imply rollback of a partially executed external tool.

Orchestration validates and materializes the complete selected recipe plan before package operations, then records observed stack-lock facts only after successful non-preview reconciliation. Accepted skill-recommendation candidates must pass prerequisite checks before manifest backups, writes, retirement, or ownership-state changes. Keep the saved personal manifest as intent, not expanded commands.

The stack concurrency lease is an exclusively created private guard file, not a held file descriptor. Capture its identity and close the creation descriptor before executing actions so Windows can remove the guard on completion; cleanup removes only that same identity and preserves another writer's replacement. Package-version probes capture bytes and decode UTF-8 in the caller: malformed stdout or stderr is unhealthy, without leaking Windows subprocess reader-thread tracebacks.

Native OMP artifact selection and command binding belong in `catalog.py`; Unix download, SHA-256 and staged `--version` verification, and atomic binary replacement belong in `packages.install_native_release`. Fresh/missing and reviewed owned force-installs freeze an exact release. Existing latest-policy native updates instead run guarded `omp update --stable` without plugin updates: preflight availability is informational, and `locking.record` resolves the actual numeric installed release's metadata and verifies its artifact digest after success. A matching exact requested or retained locked target is a no-op; a differing target refuses native update and requires reviewed owned `install --force`, without official-installer bootstrap or fallback. Retained exact locks also constrain latest-policy intent.

Output boundary regressions use isolated Python subprocess scripts, including a parent-process capture to catch inherited file-descriptor leaks. Run them with `rtk python3 -m unittest discover -s tests -p test_output_contract.py -v`; never exercise installers or authenticated personal tools to test presentation.


## Verification

Run the behavioral suite:

```sh
rtk python3 -m unittest discover -s tests -v
```

Focused domain suites also live in `tests/test_*_coverage.py`; select one with `-p`, for example:

```sh
rtk python3 -m unittest discover -s tests -p test_installers_terminal_coverage.py -v
```

GitHub Actions runs the behavioral suite and tracked-baseline validation on Ubuntu, Windows, and macOS with Python 3.10 and 3.14. Native PowerShell tests run on Windows; Linux RTK shell tests run on Linux. Platform-specific skips do not establish behavior on another operating system.

The separate Linux coverage job publishes line/branch reports as the `linux-branch-coverage` artifact, without a percentage gate. Coverage is a development-only tool; the application remains standard-library-only. To measure locally without installing it into the runtime environment:

```sh
rtk uv tool run --from 'coverage>=7.6,<8' coverage run --branch --source=dotai_app -m unittest discover -s tests -v
rtk uv tool run --from 'coverage>=7.6,<8' coverage report -m
rtk uv tool run --from 'coverage>=7.6,<8' coverage html -d htmlcov
```

These figures measure application Python executed in the test process, not the entry-point wrapper, child processes, or shell installers. Installer tests execute isolated shell commands and assert their side effects separately.

The test script also supports direct execution, including a single selected test:

```sh
rtk python3 tests/test_dotai.py DotAiTests.test_validate_omp_routing_accepts_compact_intent_and_null
```

For invocation from another working directory, pass the absolute path to `tests/test_dotai.py`; the script establishes its repository import path before loading application modules.

Validate the tracked example and direct executable entry point without initializing a personal manifest:

```sh
rtk python3 dotai.py --manifest stack.example.json validate
rtk ./dotai.py --manifest stack.example.json validate
```

`rtk python3 dotai.py validate` validates an existing default personal manifest; it never initializes one. Use explicit `init` to create a missing manifest, including for isolated previews.

When changing the manifest format or shared defaults, keep `stack.example.json`, `stack.schema.json`, CLI mutation commands, and runtime validation aligned. Never commit a generated `stack.json`. Behavioral changes should include a focused test that protects the user-visible contract.

Routing behavior tests use the shared synthetic catalog in `tests/test_dotai.py`, not current GPT or Claude versions. Keep expected provider precedence, fallback chains, and configuration effects explicit; do not calculate expected results with the production resolver. The repository catalog is validated separately and exercised for coverage of every managed role without pinning model IDs. Model refreshes should update `routing-recommendations.json` and the routing documentation, not the behavioral fixtures.

Release-notice tests use explicit synthetic current and available versions, including numeric ordering, equal/older releases, and network failures. Keep these behavioral fixtures independent of `dotai_app/releases.py`'s release version and exact notice wording; verify a release's real version with `./dotai.py version` before tagging it.

## Behavioral fixtures and CLI smoke checks

For package-presence or minimum-version tests, use isolated commands that report known versions and exit codes. Mocking only `package_check` as false does not simulate a missing binary: reconciliation can separately probe command success to distinguish missing from outdated packages.

Healthy-skill refresh fixtures need complete skills.sh v3 source metadata and an independently verified Git tree hash for the configured agent's entire installed folder, including supporting files and executable modes. Isolate `XDG_STATE_HOME` and `GH_HOST`; the authoritative lock path and source host affect ownership. Source-only locks or a matching `SKILL.md` do not prove ownership of an independent agent copy.

Exercise mutations against an explicitly initialized temporary manifest. Isolate `HOME`, `DOTAI_HOME`, `DOTAI_CONFIG_DIR`, `DOTAI_STATE_DIR`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME`; preserve personal intent, credentials, and OMP settings. Follow additions with `validate` and the appropriate preview. Fixtures resolve mutable metadata through deterministic fake sources, never real network calls; exact locks and receipts serve different purposes.

Routing smoke checks require OMP to be installed, opened at least once, and authenticated to a supported provider. Preview against an existing test manifest, not the personal `stack.json`:

```sh
rtk python3 dotai.py configure omp-routing --help
rtk python3 dotai.py --manifest path/to/test-stack.json configure omp-routing --dry-run
```

The preview should describe discovered providers, role primaries, fallback chains, proposed intent changes, and pending actions in prose without applying them or exposing JSON/credentials. Missing providers or genuinely unhealthy components can produce nonzero exits; preserve those health failures. Local macOS exercises and skipped native suites are not proof of Windows or Linux behavior.
