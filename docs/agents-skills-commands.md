# Choosing agents, skills, and commands

Use an **agent** for a separately configured specialist, a **skill** for reusable knowledge or a method, and a **command** for a deliberate entry point into a workflow. They can work together; they are not competing levels of sophistication.

This guide uses Oh My Pi (OMP) terminology. Here, a custom agent means a native task-agent definition, not an `AGENTS.md` repository instruction file. A command means a Markdown slash command, not a shell command or OMP's executable TypeScript extensions. Other harnesses can attach different behavior to the same terms.

## Compare the mechanisms

| Aspect | Custom task agent | Skill | Markdown slash command |
| --- | --- | --- | --- |
| Main purpose | Give a specialist its own role and execution configuration | Teach a reusable technique, workflow, or reference | Let the user start a repeatable workflow explicitly |
| Instructions | Specialist's system instructions, alongside OMP-generated guidance | Metadata is advertised to the model; the body is loaded when needed or explicitly invoked | Body expands into a prompt in the current conversation |
| Conversation | Separate worker conversation; relevant context must be provided | Uses the conversation of the agent loading it | Uses the conversation where it is invoked |
| Model and tools | Can configure a model, built-in tools, and allowed child-agent spawns | Does not itself select a model or grant tools | Does not itself select a model or change tool availability |
| Entry point | Ask OMP to delegate to the named specialist | Ask for the method naturally, or use `/skill:<name>` when enabled | Type `/<command-name>` with arguments |
| Primary tradeoff | Separation and specialization versus briefing and coordination overhead | Reuse and on-demand loading versus relying on discovery or explicit invocation | Convenience and continuity versus no separate specialist configuration |

A separate conversation does not automatically mean an isolated filesystem. Neither an agent definition, a skill, nor a Markdown command is a complete permission or security boundary.

## Choose what to use

Start with the immediate need rather than creating a new file:

1. **Use a normal prompt** for a one-off request that existing tools and instructions already cover.
2. **Use an existing skill** when the current assistant needs an established method or reference. Invoke it explicitly when you want to ensure the body is loaded.
3. **Use an existing command** when you want a repeatable action with the current conversation's evidence already available.
4. **Use a custom agent** when a separately scoped specialist, independent conversation, or different execution configuration is useful. Supply the objective, relevant evidence, constraints, and expected result.

An independent investigation may justify another agent even if it runs sequentially. Parallelism is a possible benefit, not a requirement. Conversely, wanting a networking perspective does not by itself require a separate worker: the current assistant can apply a networking skill or command.

## When to create an agent

Create a custom agent when the role has a durable responsibility and a meaningful reason to separate its work. Useful signals include:

- A specialist owns a category of assignments, such as Kubernetes operations, networking investigation, or source-grounded documentation maintenance.
- It benefits from its own model selection, built-in tool configuration, output contract, or permitted child agents.
- Its investigation should have a separate conversation, or it should work independently alongside other specialists.
- You want to return to that specialist's conversation and continue the assignment.

Define its responsibility, boundaries, expected inputs, and output. Avoid copying a large procedural playbook into several agent definitions; keep reusable methods in skills. OMP agents can declare `autoloadSkills` to load named skills available in the parent session before their first assignment.

Do not create an agent merely to give a checklist a persona name. A separate worker must be briefed: do not assume it knows the whole parent conversation. Supplying too little evidence loses context; copying everything defeats much of the separation benefit.

### Copilot-style personas versus OMP workers

Selecting a custom chat persona and spawning a custom worker are different interactions. OMP's native custom agents are task workers; a definition does not automatically become the main assistant's selected persona.

After spawning a specialist, open Agent Hub with `Alt+A`, select it, and send messages directly in its conversation. `/agents` is a separate definition/settings hub, not the live conversation roster.

If the actual requirement is one main assistant with a project-specific role, consider project `.omp/APPEND_SYSTEM.md` or `--append-system-prompt <file>` instead. These add instructions without replacing OMP's default system guidance. A Markdown command is not a persistent system-persona switch.

## When to create a skill

Create a skill when a method or body of knowledge should be reusable by the current assistant or by multiple specialists. Examples include an incident-investigation method, a documentation-audit procedure, or safe commit finalization.

A skill is a good fit when:

- The same technique is useful across tasks, repositories, or skill-capable harnesses.
- The assistant should discover the method from its description when a matching request arrives.
- The method needs supporting references, templates, or scripts that belong together.
- You want one maintained source of instructions with on-demand loading.

Write a description that makes the triggering conditions clear. Put the procedure, boundaries, and expected evidence in the body. Supporting scripts are executable code: review their dependencies and side effects separately; installing instructions does not supply their required tools or permissions.

A skill can be deliberately user-invoked. It does not have to run automatically, and a workflow does not stop being a skill because it has ordered steps. Durable rules that apply to every task belong in repository or system guidance rather than depending on optional skill discovery.

### Explicit invocation without conversion

When `skills.enableSkillCommands` is enabled, OMP provides `/skill:<name>` for discovered skills. It reads the skill body and injects it into the current conversation as a user-invoked skill. This does not replace the system prompt or create a separate agent.

For example:

```text
/skill:commit-and-document Only finalize the routing changes; leave unrelated edits untouched.
```

Keep `commit-and-document` as the authoritative skill if it should remain reusable and discoverable. Its workflow can inspect scope, audit documentation, validate changes, stage intended paths, and commit only after an explicit user request. A dirty worktree alone is not consent to run it.

Do not duplicate the procedure into a separate command merely to obtain slash invocation. The native skill command already provides it. Natural-language requests can also lead the assistant to load the skill, but explicit slash invocation loads the body directly.

## When to create a command

Create a Markdown command when the main missing capability is a convenient, repeatable user action in the current conversation:

- A short procedure you regularly initiate, such as a situation report or focused triage pass.
- An entry point with arguments for the cluster, symptom, file, or desired output.
- A workflow that should reuse evidence already gathered without briefing another worker.
- A shortcut that asks the assistant to apply an existing skill or delegate to an existing agent.

For example, `/network-triage <symptoms>` can expand to instructions for investigating DNS, transport, Cilium, and ingress. The same assistant performs that work with its existing model and tools. Follow-ups remain in the same conversation; there is no newly configured Network Troubleshooter identity or permission boundary.

A command can request an ongoing approach in its text, but that text remains conversational instructions, not a durable system configuration change. Markdown expansion also does not execute a deterministic program: the assistant still interprets and carries out the request. If exact enforcement or automation is required, use an appropriate executable tool or runtime control rather than relying solely on prompt wording.

Before creating a command, check whether `/skill:<name>` already covers the action. If a separate shortcut is justified, keep it thin and reference the authoritative skill or agent instead of maintaining a second copy of the workflow. That shortcut requests the behavior; it does not itself enforce tool dispatch.

## Apply the choice to the Kubernetes workflow

| Need | Suitable choice | Reason |
| --- | --- | --- |
| An operations specialist responsible for general incident investigation | K8 Operator agent | Durable role with its own assignment and specialist delegation policy |
| A networking specialist investigating independently | Network Troubleshooter agent | Separate investigation and conversation, with evidence handed back to the operator |
| Apply a networking checklist to findings already discussed | Networking skill or `/network-triage` command | The current assistant can keep the existing incident context |
| A documentation specialist owning source-grounded documentation assignments | Documentation Writer agent | Distinct responsibility, not just a one-time formatting step |
| Reuse the same source-validation method across agents | Documentation-audit skill | One method shared by multiple roles |
| Turn this conversation's confirmed resolution into a runbook | Existing skill invocation or a thin `/document-findings` command | Reuses the evidence already gathered without requiring a new worker |
| Safely finalize the completed repository work | `/skill:commit-and-document` | Explicitly starts the existing workflow without duplicating it |

These agent and command names illustrate a possible setup; this guide does not install or register them. A Copilot `.github/agents/*.agent.md` file is not automatically an OMP-native task-agent definition. Translate tool names and supported frontmatter rather than copying VS Code-specific handoffs unchanged.

## Scope, ownership, and safety

Default native locations are:

| Mechanism | Project | Personal, default profile |
| --- | --- | --- |
| Task agent | `.omp/agents/<name>.md` | `~/.omp/agent/agents/<name>.md` |
| Skill | `.omp/skills/<name>/SKILL.md` | `~/.omp/agent/skills/<name>/SKILL.md` |
| Markdown command | `.omp/commands/<name>.md` | `~/.omp/agent/commands/<name>.md` |

OMP also discovers universal skills in `.agents/skills/` and `~/.agents/skills/`, used by this repository's [project skill recommendations](project-skills.md) and global skill installation respectively. Native user paths can change with profiles and configuration-directory overrides. Prefer project scope for repository-specific knowledge and personal scope for methods intended across repositories.

DotAi's `add skill` and `sync` manage declared user-global skills; they are not a generic installer for the native agent and command files above. See [Extending the stack](extending.md) for managed integrations.

Preserve these boundaries regardless of the chosen mechanism:

- OMP task subagents do not inherit `AGENTS.md` context files. Ensure critical operational constraints reach each specialist explicitly; do not rely on the parent having read them.
- A task agent's `tools` list selects built-ins but does not by itself remove inherited MCP or extension tools. Use runtime permission controls and appropriately scoped credentials for enforced restrictions.
- A skill or command saying "read-only" or "ask before changes" is guidance, not an enforcement mechanism. Explicit skill invocation starts the workflow; it does not authorize unrelated changes or override repository rules.
- Maintain one source for each reusable method. An agent can use a skill, and a command can request either, without repeating their full instructions.

## OMP references

OMP-specific behavior above follows the current upstream contracts; check them when upgrading or adapting another harness:

- [Task-agent discovery, frontmatter, and spawn constraints](https://github.com/can1357/oh-my-pi/blob/main/docs/task-agent-discovery.md)
- [Skills, discovery, and explicit slash invocation](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md)
- [Markdown slash-command discovery and expansion](https://github.com/can1357/oh-my-pi/blob/main/docs/slash-command-internals.md)
- [Agent Hub and direct specialist conversations](https://github.com/can1357/oh-my-pi/blob/main/docs/agent-hub.md)
- [System-prompt customization and subagent input inheritance](https://github.com/can1357/oh-my-pi/blob/main/docs/system-prompt-customization.md)
