# Personal-stack roadmap

This is the agreed direction and task list, not a description of implemented behavior. Current command contracts remain in [Configuration](configuration.md) and [Extending the stack](extending.md). Runtime implementation has not started during this planning discussion.

## Scope

Manage one explicitly selected personal AI stack across Windows, Linux, and macOS. OMP, Graphify, and RTK are optional managed components, not mandatory prerequisites or automatically adopted installations. Node/npm, uv, Python, Git, curl, and platform package managers remain user-managed prerequisites wherever selected components require them.

Preserve unmanaged installations, configuration, locally modified content, credentials, and other agents' independent copies. Project-specific skills are a longer-term feature; project manifests, layering, and additional harness implementations are outside the immediate scope.

## Workstreams

Each workstream receives its own branch. Branch names below are proposed; the documentation contract is already recorded on `docs/cli-output-contract`.

| Workstream | Proposed branch | Acceptance criteria |
| --- | --- | --- |
| Human-readable output | `feature/human-readable-output` | DotAi controls normal output from every command. Explain proposed actions, reasons, components, resolved targets, progress, results, and next steps in plain language. Capture non-interactive subprocess chatter; avoid JSON dumps and raw manifest diffs. Preserve interactive prompts explicitly, failure evidence, exit codes, and health semantics. Provide sanitized diagnostics through `--verbose`, and summarize partial completion and restart requirements accurately. |
| Prerequisite and ownership boundaries | `feature/prerequisite-boundaries` | Selected components declare their prerequisites. Check presence and compatibility before dependent changes; explain missing requirements without installing or upgrading them. Discovery does not confer management permission. Existing configurations cannot continue running obsolete prerequisite mutation commands unnoticed. |
| Existing-manifest conversion | `feature/manifest-conversion` | Preview a human-readable, explicit conversion of existing manifests and apply accepted changes with a backup. Move prerequisite checks out of managed package operations and remove their mutation permission. Preserve selected AI components and integrations. Ask about ambiguous custom entries rather than guessing. Conversion does not install, update, or uninstall software, and does not reset the manifest to repository defaults. |
| Optional managed AI components | `feature/optional-components` | OMP, Graphify, and RTK are managed only when explicitly selected. Omitted components are not unhealthy merely because a baseline recommends them. OMP-specific integrations declare their harness requirements; unrelated selections remain usable without an unselected harness. |
| Component lifecycle | `feature/component-lifecycle` | Inspect individual components, sources, ownership, versions or revisions, scope, and paths. Support targeted updates and explicit adoption. Distinguish stopping management from uninstalling; enable/disable integrations where supported. Adding one skill retains other selections from the same source. Removal preserves unmanaged and other-agent copies and refuses uncertain ownership. |
| Sync versus upgrades | `feature/sync-update-semantics` | Install makes selected components available; sync reconciles declared selections and configuration without silently advancing versions; update explicitly advances selected managed components. Surface local modifications and uncertain provenance for review rather than treating uncertainty as refresh permission. Previews and execution share planning decisions, and failures report completed and pending operations without claiming universal rollback. Inspection and previews do not implicitly initialize manifests. |
| Portable intent and maintained recipes | `feature/portable-personal-stack` | Separate the portable personal manifest from the application checkout and maintained platform installation recipes. Preserve explicit custom recipes as an escape hatch. Keep credentials outside the manifest, retain portable references, and report platform incompatibilities clearly. Repository recommendations are optional and never replace personal choices automatically. |
| Reproducibility | `feature/stack-lock` | Separate requested versions/update policies from resolved versions and revisions. Record installer versions where relevant and resolve platform-specific artifacts for the same selected stack. Honor pins where supported and report unsupported or unavailable pins instead of claiming equivalent installations. |

## Delivery and verification

- Use a separate branch per workstream; integrate required prerequisite work before starting dependent work. Do not mix unrelated workstreams into one branch or commit.
- Make incremental commits at coherent behavioral checkpoints, with the corresponding tests and documentation. Avoid one final bulk commit or incomplete placeholder commits.
- Apply the command-output contract in `AGENTS.md` to every new or changed command, not only `update`.
- Verify external-tool output with isolated CLI scenarios covering successful, unchanged, failed, and partially completed operations; include noisy stdout/stderr, verbose diagnostics, credential redaction, and dry runs. Exercise actual subprocess execution without installing or updating personal tools as a test side effect.
- Verify platform-specific recipes and semantics on their supported platforms. A local skip does not prove Windows or Linux behavior.
- Keep project-specific management deferred while avoiding new assumptions that a consumer project's directory is the DotAi checkout.
