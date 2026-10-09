# DotAi

DotAi is a declarative, cross-platform manager for a personal AI development stack. It applies the same tools, skills, plugins, and MCP servers across Windows, WSL, Ubuntu, Arch Linux, and macOS while preserving configuration that DotAi does not own.

Repository defaults live in [`stack.example.json`](stack.example.json). Run `init` explicitly to create a user-owned manifest; inspection and dry-run commands never initialize it. Existing manifests are never overwritten from the example.

## Installation

Python 3.10 or newer must already be installed and available on `PATH`.

### Linux, WSL, or macOS

```sh
git clone https://github.com/mathwro/DotAi.git
cd DotAi
./dotai.py init
./dotai.py install
```

Direct execution uses `python3` from `PATH`. If your Python 3.10+ interpreter is available only as `python`, use `python dotai.py install` and replace `./dotai.py` with `python dotai.py` in the examples below.

### Windows PowerShell

Install [Scoop](https://scoop.sh/) first, then run:

```powershell
git clone https://github.com/mathwro/DotAi.git
Set-Location DotAi
python dotai.py init
python dotai.py install
```

If Python is available through the Windows launcher instead, use `py -3 dotai.py install` and replace `python dotai.py` with `py -3 dotai.py` in subsequent commands. If your interpreter is named `python3`, use `python3 dotai.py` instead. The selected interpreter must be Python 3.10 or newer.

Native Windows uses Scoop for all managed package operations; Winget is intentionally not used.

## Optional: configure OMP routing

**Routing configuration is optional and must only run after installation, your first OMP launch, and provider authentication.** Skip it to keep OMP's existing model selection; `install`, `update`, and `sync` do not configure routing automatically.

1. Complete `./dotai.py install` (or `python dotai.py install` on Windows).
2. Run `omp` to open OMP for the first time.
3. Inside OMP, use `/login` and authenticate at least one supported provider: GitHub Copilot, OpenAI Codex, or Anthropic. Finish authentication and return to your shell.
4. Only then preview the proposed routing and, if you want it, apply it:

   ```sh
   ./dotai.py configure omp-routing --dry-run
   ./dotai.py configure omp-routing
   ```

   In PowerShell:

   ```powershell
   python dotai.py configure omp-routing --dry-run
   python dotai.py configure omp-routing
   ```

DotAi selects available models for interactive and worker roles and preserves unrelated OMP settings. See [OMP provider routing](docs/configuration.md#configure-omp-provider-routing) for provider selection, `--primary`, fallbacks, and migration details.

## Usage

Use `./dotai.py` on Linux, WSL, and macOS, or `python dotai.py` in PowerShell. The interpreter alternatives described under [Installation](#installation) apply to every command.

```sh
./dotai.py install          # Install missing components and synchronize configuration
./dotai.py update           # Update explicitly managed components and synchronize configuration
./dotai.py sync             # Reconcile skills, plugins, and MCP servers; no package operations
./dotai.py sync --update-skills  # Explicitly refresh already installed skill sources
./dotai.py sync --recommended-skills  # Review and apply repository skill recommendations
./dotai.py sync --recommended-skills --enforce  # Offer user-skill cleanup, then review exact recommendations
./dotai.py status           # Show installed, missing, inactive, or drifting components
./dotai.py doctor           # Check the stack plus platform prerequisites
./dotai.py validate         # Validate an existing version-2 manifest
./dotai.py version          # Print the version and warn about newer releases
./dotai.py platform         # Print the detected platform
```

`sync`, including `sync --dry-run`, lists pending recommended skill changes by `owner/repository` and selected skill names without changing your manifest. Use `sync --recommended-skills` to review and accept them; locally differing sources remain preserved unless explicitly adopted with `--enforce`.

`--enforce` first lists user-owned sources declared in your manifest but absent from the current recommendations as `owner/repository/skill` (or `owner/repository` for wildcard or unspecified selections), without a JSON diff, and asks whether to remove them. **Yes** removes their manifest entries and source-verified installed copies; **No** (the default) preserves them and skips their installation for this run. It then continues to the recommendation review, including exact selections for locally differing recommended sources. Old defaults without recorded recommendation ownership appear in the cleanup list too. Skills outside the manifest and other agents' independent copies are not removed. Add `--dry-run` to preview optional cleanup and recommendation changes without prompts or mutations.

Common options:

```sh
./dotai.py install --dry-run
./dotai.py install --force
./dotai.py --manifest path/to/stack.json status
./dotai.py --manifest path/to/new-stack.json init
```

`init` creates only a missing manifest and refuses to overwrite an existing file. Node.js/npm, `uv`, Python, Git, curl, and platform package managers are external prerequisites: DotAi checks them but never installs, updates, or configures them. Install missing prerequisites yourself using the reported guidance, then retry. Requirements are checked only for selected, enabled components, before any dependent changes.

Version-1 manifests are rejected by normal commands before environment changes. Use the explicit `convert` command to review the one-way version-2 conversion before applying it; existing entries are preserved and prerequisite mutation commands are retired.

Packages require explicit `managed: true` permission. Disabled components are not reconciled. Dry runs describe changes without creating a manifest or changing files.

### Convert an existing manifest

```sh
./dotai.py --manifest path/to/legacy-stack.json convert --dry-run
./dotai.py --manifest path/to/legacy-stack.json convert
# Noninteractive ownership review must name each package you authorize:
./dotai.py --manifest path/to/legacy-stack.json convert --manage custom-ai --yes
# Keep the original manifest and write the converted intent elsewhere:
./dotai.py --manifest path/to/legacy-stack.json convert --destination path/to/new-stack.json
```

Conversion is file-only: it never installs or updates tools or rewrites their settings. It preserves skills, MCP entries, routing, custom package commands, and user metadata, while moving known general dependencies to checks-only prerequisites and retiring their mutation commands. Review management permission for each retained package. An exact private source backup is created before the confirmed write; an existing destination is never overwritten. See [conversion details](docs/configuration.md#convert-version-1-manifests).


### Status output

| Label | Color | Meaning |
| --- | --- | --- |
| `OK` | Green | Installed and active in the intended agent |
| `RUN` | Cyan | An operation is planned or running |
| `INACTIVE` | Yellow | Installed elsewhere but inactive in OMP, or optional routing not configured |
| `DRIFT` | Yellow | Managed configuration is unavailable or differs |
| `UNVERIFIED` | Yellow | No named skill checks; installation cannot be confirmed |
| `MISSING` | Red | A declared component is not installed |
| `FAIL` | Red | An operation or prerequisite check failed |

Color can be controlled with `--color auto|always|never`. Automatic mode respects `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`; `FORCE_COLOR=0` disables it even on a terminal. Explicit `always` or `never` takes precedence over the environment. `status` and `doctor` return a nonzero exit code when the declared stack is unhealthy.

The `INACTIVE` hint for unconfigured optional routing is informational and does not make the stack unhealthy.

## Extending the stack

Use `add` to record a desired integration in your local `stack.json`; adding it does not install it yet. Replace these example names and URLs with your own:

```sh
./dotai.py add skill owner/repository --skill review
./dotai.py add mcp example --url https://example.com/mcp
./dotai.py add marketplace team owner/marketplace
./dotai.py add plugin review@team --scope user
```

Then preview with `./dotai.py sync --dry-run` and apply with `./dotai.py sync`. For command-line tools, use `./dotai.py add tool` with `--check` and platform-specific `--install` commands, then run `./dotai.py install`; `sync` does not install packages. Use `python dotai.py` instead of `./dotai.py` in PowerShell.

Reusing a skill source, MCP or marketplace name, plugin ID, or tool name replaces that manifest entry. See [Extending the stack](docs/extending.md) for tool examples, stdio/SSE servers, authentication headers and environment references, skill checks, and recommendation management.

## Managed stack

| Type | Components |
| --- | --- |
| Harness | [Oh My Pi](https://github.com/can1357/oh-my-pi) |
| Tools | [RTK](https://github.com/rtk-ai/rtk), [Graphify](https://github.com/Graphify-Labs/graphify) |
| Skills | [Ponytail](https://github.com/DietrichGebert/ponytail), [Superpowers](https://github.com/obra/superpowers), [Grilling and Writing for Agents](https://github.com/mattpocock/skills), [Choose Branch Structure](https://github.com/mathwro/Skills/tree/main/skills/choosing-branch-structure), [Commit and Document](https://github.com/mathwro/Skills), [Emil Design Engineering](https://github.com/emilkowalski/skills/tree/main/skills/emil-design-eng), [Web Design Guidelines](https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines), installed through [skills.sh](https://skills.sh/) |
| MCP servers | [Context7](https://context7.com/), [Microsoft Learn](https://learn.microsoft.com/training/support/mcp) |

Node.js/npm, `uv`, Python, Git, curl, and platform package managers are externally supplied prerequisites, never managed components.

RTK 0.43 or newer is configured for Pi and registered as an OMP global extension without replacing unrelated extensions. DotAi also enables OMP's **Hide Secrets** privacy setting during installation and updates.

DotAi installs the Graphify CLI, but its Pi skill and project graph generation are opt-in. Run `graphify extract . --code-only` in a project when you want a graph. Existing local manifests retain their earlier Graphify configuration; see [Optional Graphify](docs/configuration.md#optional-graphify) before your next `install` or `update`.

Web interface coverage selects only `emil-design-eng` for component and interaction polish and `web-design-guidelines` for interface-quality review, not their repositories' full skill bundles. Existing users can preview these additions with `./dotai.py sync --recommended-skills --dry-run`, then review and apply them with `./dotai.py sync --recommended-skills`. Restart OMP after synchronization and invoke `/skill:emil-design-eng` or `/skill:web-design-guidelines` when relevant. These skills complement, rather than replace, project-specific UX decisions and real browser verification.

## Tests

Run all unit tests from the repository root:

```sh
rtk python3 -m unittest discover -s tests -v
```

RTK is optional for running tests; omit `rtk` if it is not installed. On Windows, replace `python3` with `python` or `py -3`, depending on your installed interpreter.

## Guides

- [Configuration](docs/configuration.md) — manifest lifecycle, safety guarantees, and OMP provider routing
- [Extending the stack](docs/extending.md) — add skills, MCP servers, plugins, and command-line tools
- [Vetted project skills](docs/project-skills.md) — conditional recommendations, project-local installation commands, and runtime boundaries
- [Development](docs/development.md) — repository layout and contributor verification
