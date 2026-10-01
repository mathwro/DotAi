# Vetted project-scoped skills

Optional recommendations for a specific project, not additions to DotAi's global baseline. Start with the existing tools and the baseline's `emil-design-eng` interaction guidance and `web-design-guidelines` review skill. Add a project skill only for a concrete gap; choose one design workflow for a task rather than activating competing design authorities together.

## Install in the target project

Run the commands below **from the root of the project that needs the skill**, not from the DotAi checkout. Node.js and npm are required for the Skills CLI. Examples use RTK; omit the `rtk proxy` prefix if RTK is unavailable.

These commands use the [Skills CLI](https://github.com/vercel-labs/skills), select one exact skill, and target `universal`. Without `--global`, the skill is copied into that project's `.agents/skills/<name>/`, which OMP discovers. `--copy` retains the full skill directory, including supporting references and scripts. Review installer prompts and existing same-named skills before accepting a replacement; do not substitute `--all`, wildcard selections, or `--global`.

Project scope limits availability, not activation cost or permissions. Large playbooks, retrieved context, screenshots, and executable helpers still have a cost when used. Inspect the current upstream instructions and supporting files before installation or updates; the recommendations below are based on source review, not measured design-quality improvements or a security certification.

DotAi's `add skill` and `sync` manage user-global manifest entries, not these project installations. Do not add these optional sources to `stack.json` just to use them in one project. Review the generated project `skills-lock.json` and skill files before committing them for teammates; keep runtime caches, screenshots, credentials, and browser profiles out of commits.

Restart OMP after installation, then use `/skill:<name> <task>`. Installation does not create an image generator, browser runtime, or new tool permissions.

## Choose by the missing capability

| Skill | Recommended when | Not a reason to install it |
| --- | --- | --- |
| [Taste](#taste-marketing-site-art-direction) | A landing page, portfolio, or marketing-site redesign needs deliberate visual direction. | Dashboard, data-table, or multi-step application design. |
| [Impeccable](#impeccable-product-aware-design-workflow) | A substantial application surface needs task discovery, design strategy, holistic critique, or persistent product/design context. | Another global polish or accessibility checklist. |
| [Brandkit](#brandkit-brand-identity-image-boards) | A branding or rebranding project needs visual identity reference boards and an image generator is already available. | Implementing an application or obtaining image-generation capability. |
| [Playwright CLI](#playwright-cli-browser-execution) | The project needs a portable browser CLI workflow or lacks a usable existing browser interface. | Duplicating working OMP browser automation or merely adding another MCP server. |

## Taste: marketing-site art direction

**Use for:** distinctive landing pages, portfolios, and marketing-site redesigns where layout, typography, imagery, and composition are the missing capability.

**What it adds:** brief-based art direction, visual consistency rules, reference vocabulary, and an audit-first redesign process. Narrowing to one skill avoids the repository's other styles and image-generation skills.

```sh
rtk proxy npx skills@latest add Leonxlnx/taste-skill --skill design-taste-frontend --agent universal --copy
```

Invoke `/skill:design-taste-frontend` with the target, audience, brand constraints, and whether existing identity must be preserved.

**Limits:** the current default is experimental v2. It explicitly excludes dashboards, data tables, and multi-step product UI. Its long instruction body and prescriptive aesthetics can be expensive or inappropriate for small tasks. Do not combine it with Impeccable as a second art director for the same task, or infer that its image-generation preferences supply an image tool.

Sources: [repository and install names](https://github.com/Leonxlnx/taste-skill), [actual skill](https://github.com/Leonxlnx/taste-skill/blob/main/skills/taste-skill/SKILL.md).

## Impeccable: product-aware design workflow

**Use for:** substantial new application screens, ambiguous flows, scoped redesigns, or projects that need durable product and design decisions across sessions. Its Operate mode supports dashboards, dense tables, familiar controls, and task-oriented interaction.

**What it adds:** task/audience discovery, UX briefs, whole-surface design direction, heuristic critique, onboarding and state hardening. `PRODUCT.md` records product truth; `DESIGN.md` records visual decisions. This goes beyond Emil's component craft and Vercel's code-level review, although their animation, polish, and audit guidance overlaps.

Install the generated universal skill through the Skills CLI, rather than Impeccable's native installer:

```sh
rtk proxy npx skills@latest add https://github.com/pbakaus/impeccable/tree/main/.agents/skills/impeccable --skill impeccable --agent universal --copy
```

This copies the skill, references, and launchers without installing Impeccable's provider-native hook manifests. It is not a Markdown-only runtime: normal activation runs a native engine, bundled or downloaded on first use, and its cache can live in `~/.impeccable/` even though the skill is project-local. Approve that executable dependency separately before running helpers. The skill has a disclosed direct-context fallback when its launcher is refused or unavailable; advanced helpers are not available through that fallback.

Start with `/skill:impeccable shape <screen-or-flow>` for planning, or `/skill:impeccable critique <existing-surface>` for evaluation. Specify the existing design system, required states, and untouched areas. For a lean initial trial, ask for code-first work (`buildPath: "code"` in the existing Impeccable configuration) rather than generated image comps, preserving unrelated settings. Missing product context can route through initialization and create `PRODUCT.md` after confirmation; critique can also write project-local reports.

**Initial trial boundaries:** leave native hooks off and avoid live mode and direct image-generation helpers. Set these in the agent's environment before starting the session:

| Variable | Value | Effect |
| --- | --- | --- |
| `IMPECCABLE_NO_UPDATE_CHECK` | `1` | Disable the runtime's update check. |
| `IMPECCABLE_NO_TELEMETRY` | `1` | Disable the concept-selection telemetry ping. |
| `IMPECCABLE_LIVE_COPY_AGENT` | `off` | Disable automatic external live copy-edit agent selection. |

These settings do not make the engine offline or prevent bootstrap downloads and concept-roll requests. Live copy editing can otherwise launch authenticated Codex or Claude with approval/sandbox-bypassing flags; enabling live mode needs a separate review. Image-generation helpers can send prompts and reference images to an external provider.

**Limits:** references are loaded by workflow, but new-work and critique playbooks are large. The aesthetic bans and concept-selection process are more opinionated than a small design checklist. Detector findings and critique scores need contextual judgment, not treatment as usability or WCAG certification. Generic OMP skill discovery does not establish upstream hook, subagent, or live-browser compatibility; those remain separate prerequisites to verify. URL inspection requires a working installed browser.

Sources: [actual universal skill](https://github.com/pbakaus/impeccable/blob/main/.agents/skills/impeccable/SKILL.md), [Operate guidance](https://github.com/pbakaus/impeccable/blob/main/.agents/skills/impeccable/reference/operate.md), [runtime and hooks](https://github.com/pbakaus/impeccable), [live copy-edit runners](https://github.com/pbakaus/impeccable/blob/main/crates/live/src/copy_edit_agent.rs).

## Brandkit: brand-identity image boards

**Use for:** a launch, rebrand, or identity exploration that needs a coherent visual reference board before implementation.

**What it adds:** art direction for logo concepts, palette, typography, and digital/physical identity applications. The deliverable is an image board, not coded components or an accessible application.

```sh
rtk proxy npx skills@latest add Leonxlnx/taste-skill --skill brandkit --agent universal --copy
```

Invoke `/skill:brandkit` with the brand brief and available image-generation tool. This recommendation refers specifically to Brandkit in the Taste repository, not similarly named products.

**Limits:** use only with an already-available image generator. The skill does not install or integrate one. Image boards do not establish vector-logo quality, trademark clearance, asset rights, responsive behavior, or application flows. Review provider data handling before sending confidential brand material.

Sources: [repository's image-generation category](https://github.com/Leonxlnx/taste-skill), [actual Brandkit skill](https://github.com/Leonxlnx/taste-skill/blob/main/skills/brandkit/SKILL.md).

## Playwright CLI: browser execution

**Use for:** cross-agent CLI portability, a Playwright-oriented debugging workflow, or browser interactions and screenshots when the existing browser interface is unavailable. It is execution and verification capability, not a visual designer.

**What it adds:** persistent browser sessions, targeted structural observations, interactions, screenshots, and diagnostic artifacts. File-backed snapshots can limit routine context output; reading full snapshots or images still consumes context. Structural snapshots do not prove visual layout.

Install only the skill:

```sh
rtk proxy npx skills@latest add microsoft/playwright-cli --skill playwright-cli --agent universal --copy
```

The skill alone does not install its executable. If the project does not already provide a compatible CLI, an npm project can add it locally:

```sh
rtk proxy npm install --save-dev @playwright/cli
rtk proxy npx --no-install playwright-cli --help
```

Use the project's package-manager convention instead of introducing a second lockfile. The npm example creates or changes `package.json` and its lockfile; review those changes. Node.js 18 or newer and compatible browser binaries/system dependencies are separate prerequisites. Do not install browsers or change the machine automatically just because the skill is present.

Invoke `/skill:playwright-cli` with a specific local/staging flow to exercise. With the local package, use `npx --no-install playwright-cli <command>` rather than assuming a global `playwright-cli` binary. Launch only after reviewing browser prerequisites; the help check does not prove browser startup or an interactive flow.

**Limits:** do not add a duplicate browser integration when OMP's browser already works. Use isolated test accounts and browser profiles; form submissions and authenticated actions are real. Keep `.playwright-cli/` screenshots, snapshots, saved authentication state, and other sensitive artifacts out of commits. No additional Playwright MCP server is required for this CLI workflow.

Sources: [official CLI and prerequisites](https://github.com/microsoft/playwright-cli), [actual skill](https://github.com/microsoft/playwright-cli/blob/main/skills/playwright-cli/SKILL.md).

## Deliberately not included

UI/UX Pro Max remains a candidate for app-design guidance, but its reviewed stock installer adds seven skills and its slimmer repository skill contains Claude-plugin-specific script paths. A clean, selective OMP installation has not been verified, so this guide does not recommend a command that silently adds the bundle or requires undocumented repairs.

Awesome DESIGN.md is a reference collection, not an installable skill. A single reviewed, adapted reference can inform a project's own design document; do not install the whole collection or assume a brand's marketing-site analysis specifies its application's UX.

`find-skills` is not recommended here: its reviewed workflow encourages popularity-based selection and global installation. Discovery/curation simulations did not demonstrate an incremental benefit over the existing tools with an explicit project-scope request.
