# DotAi

DotAi is a declarative, cross-platform manager for a personal AI development stack. It applies the same tools, skills, plugins, and MCP servers across Windows, WSL, Ubuntu, Arch Linux, and macOS while preserving configuration that DotAi does not own.

Repository defaults live in [`stack.example.json`](stack.example.json). On the first manifest-using command, DotAi copies that template to an ignored, user-owned `stack.json`; later runs install, update, synchronize, and extend the local manifest without overwriting it from the example.

## Installation

Python 3.10 or newer must already be installed and available on `PATH`.

### Linux, WSL, or macOS

```sh
git clone https://github.com/mathwro/DotAi.git
cd DotAi
./dotai.py install
```

### Windows PowerShell

Install [Scoop](https://scoop.sh/) first, then run:

```powershell
git clone https://github.com/mathwro/DotAi.git
Set-Location DotAi
python dotai.py install
```

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

Use `./dotai.py` on Linux, WSL, and macOS, or `python dotai.py` in PowerShell.

```sh
./dotai.py install          # Install missing components and synchronize configuration
./dotai.py update           # Update core components and synchronize configuration
./dotai.py sync             # Reconcile skills, plugins, and MCP servers; no package operations
./dotai.py sync --update-skills  # Explicitly refresh already installed skill sources
./dotai.py sync --recommended-skills  # Review and apply repository skill recommendations
./dotai.py sync --recommended-skills --enforce  # Fully adopt and clean recommended skill sources
./dotai.py status           # Show installed, missing, inactive, or drifting components
./dotai.py doctor           # Check the stack plus platform prerequisites
./dotai.py validate         # Initialize when absent, then validate stack.json
./dotai.py version          # Print the version and warn about newer releases
./dotai.py platform         # Print the detected platform
```

Common options:

```sh
./dotai.py install --dry-run
./dotai.py install --force
./dotai.py update --include-dependencies
./dotai.py --manifest path/to/stack.json status
./dotai.py --manifest path/to/new-stack.json init
```

`init` creates only a missing manifest and refuses to overwrite an existing file. Dependency tools such as Node.js and `uv` install when missing but update only with `--include-dependencies`. OMP updates use its version-aware `omp update` command.

Preview commands still initialize a missing default `stack.json` on first use. Run `./dotai.py validate` first if you want to separate manifest initialization from a dry-run preview.

Linux RTK installs and updates use the reviewed v0.50.0 release archives with pinned SHA-256 digests rather than executing an installer from a moving branch. To adopt this change on an existing installation, update the RTK commands in your personal `stack.json` from `stack.example.json`; DotAi does not overwrite that file.

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

Color can be controlled with `--color auto|always|never`. Automatic mode respects `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`. `status` and `doctor` return a nonzero exit code when the declared stack is unhealthy.

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
| Tools | [RTK](https://github.com/rtk-ai/rtk), [Graphify](https://github.com/Graphify-Labs/graphify), Node.js, `uv`, `curl` |
| Skills | [Ponytail](https://github.com/DietrichGebert/ponytail), [Superpowers](https://github.com/obra/superpowers), [Grilling and Writing for Agents](https://github.com/mattpocock/skills), [Choose Branch Structure](https://github.com/mathwro/Skills/tree/main/skills/choosing-branch-structure), [Commit and Document](https://github.com/mathwro/Skills), installed through [skills.sh](https://skills.sh/) |
| MCP servers | [Context7](https://context7.com/), [Microsoft Learn](https://learn.microsoft.com/training/support/mcp) |

RTK 0.43 or newer is configured for Pi and registered as an OMP global extension without replacing unrelated extensions. DotAi also enables OMP's **Hide Secrets** privacy setting during installation and updates.

DotAi installs the Graphify CLI, but its Pi skill and project graph generation are opt-in. Run `graphify extract . --code-only` in a project when you want a graph. Existing local manifests retain their earlier Graphify configuration; see [Optional Graphify](docs/configuration.md#optional-graphify) before your next `install` or `update`.

## Guides

- [Configuration](docs/configuration.md) — manifest lifecycle, safety guarantees, and OMP provider routing
- [Extending the stack](docs/extending.md) — add skills, MCP servers, plugins, and command-line tools
- [Development](docs/development.md) — repository layout and contributor verification
