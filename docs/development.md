# Development

The runtime uses only the Python standard library and supports Python 3.10 or newer.

## Repository layout

```text
dotai.py             Cross-platform manager implementation
dotai                Unix command wrapper
dotai.ps1            PowerShell command wrapper
bootstrap.sh         Linux, WSL, and macOS bootstrap
bootstrap.ps1        Native Windows bootstrap
stack.example.json   Tracked baseline copied for new users
stack.json           Ignored, user-owned stack configuration
stack.schema.json    JSON Schema for stack manifests
docs/                User and contributor documentation
tests/               Behavioral test suite
AGENTS.md             Repository guidance for coding agents
```

The public launchers, bootstrap scripts, manifests, and schema stay at the repository root because installation and operational commands address them there directly.

## Verification

Run the behavioral suite:

```sh
rtk python3 -m unittest discover -s tests -v
```

Validate the manifest and Unix launchers:

```sh
rtk python3 dotai.py validate
rtk sh -n bootstrap.sh dotai
```

When changing the manifest format or shared defaults, keep `stack.example.json`, `stack.schema.json`, CLI mutation commands, and runtime validation aligned. Never commit a generated `stack.json`. Behavioral changes should include a focused test that protects the user-visible contract.
