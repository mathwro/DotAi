# DotAi

DotAi is a declarative, cross-platform manager for a personal AI development stack. It applies the same tools, skills, plugins, and MCP servers across Windows, WSL, Ubuntu, Arch Linux, and macOS while preserving configuration that DotAi does not own.

Repository defaults live in [`stack.example.json`](stack.example.json). On the first manifest-using command, DotAi copies that template to an ignored, user-owned `stack.json`; later runs install, update, synchronize, and extend the local manifest without overwriting it from the example.

## Installation

Python 3.10 or newer must already be installed and available on `PATH`.

### Linux, WSL, or macOS

```sh
git clone https://github.com/mathwro/DotAi.git
cd DotAi
./dotai install
```

### Windows PowerShell

Install [Scoop](https://scoop.sh/) first, then run:

```powershell
git clone https://github.com/mathwro/DotAi.git
Set-Location DotAi
.\dotai.ps1 install
```

Native Windows uses Scoop for all managed package operations; Winget is intentionally not used.

## Usage

Use `./dotai` on Linux, WSL, and macOS, or `.\dotai.ps1` in PowerShell.

```sh
./dotai install          # Install missing components and synchronize configuration
./dotai update           # Update core components and synchronize configuration
./dotai sync             # Synchronize skills, plugins, and MCP servers only
./dotai sync --recommended-skills  # Review and apply repository skill recommendations
./dotai status           # Show installed, missing, inactive, or drifting components
./dotai doctor           # Check the stack plus platform prerequisites
./dotai validate         # Initialize when absent, then validate stack.json
./dotai version          # Print the version and warn about newer releases
./dotai platform         # Print the detected platform
```

Common options:

```sh
./dotai install --dry-run
./dotai install --force
./dotai update --include-dependencies
./dotai --manifest path/to/stack.json status
./dotai --manifest path/to/new-stack.json init
```

`init` creates only a missing manifest and refuses to overwrite an existing file. Dependency tools such as Node.js and `uv` install when missing but update only with `--include-dependencies`. OMP updates use its version-aware `omp update` command.

### Status output

| Label | Color | Meaning |
| --- | --- | --- |
| `OK` | Green | Installed and active in the intended agent |
| `RUN` | Cyan | An operation is planned or running |
| `INACTIVE` | Yellow | Installed elsewhere, but not active in OMP |
| `DRIFT` | Yellow | Managed configuration is unavailable or differs |
| `MISSING` | Red | A declared component is not installed |
| `FAIL` | Red | An operation or prerequisite check failed |

Color can be controlled with `--color auto|always|never`. Automatic mode respects `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`. `status` and `doctor` return a nonzero exit code when the declared stack is unhealthy.

## Managed stack

| Type | Components |
| --- | --- |
| Harness | [Oh My Pi](https://github.com/can1357/oh-my-pi) |
| Tools | [RTK](https://github.com/rtk-ai/rtk), [Graphify](https://github.com/Graphify-Labs/graphify), [GitHub CLI](https://cli.github.com/), [GitHub Stacked PRs](https://github.com/github/gh-stack), Node.js, `uv`, `curl` |
| Skills | Curated through [skills.sh](https://skills.sh/): Ponytail Review; Verification Before Completion; Receiving Code Review; Grilling; Writing for Agents; Think; Hunt; gh-stack; Find Skills; Frontend Design; Web Design Guidelines; Commit and Document |
| MCP servers | [Context7](https://context7.com/), [Microsoft Learn](https://learn.microsoft.com/training/support/mcp) |

RTK 0.43 or newer is configured for Pi and registered as an OMP global extension without replacing unrelated extensions. DotAi also enables OMP's **Hide Secrets** privacy setting during installation and updates.

## Guides

- [Configuration](docs/configuration.md) — manifest lifecycle, safety guarantees, and OMP provider routing
- [Extending the stack](docs/extending.md) — add skills, MCP servers, plugins, and command-line tools
- [Development](docs/development.md) — repository layout and contributor verification
