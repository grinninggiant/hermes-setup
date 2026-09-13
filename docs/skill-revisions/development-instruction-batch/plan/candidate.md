---
name: plan
description: "Use when the user requests a plan instead of implementation."
version: 2.0.0
author: Hermes Agent (writing-craft adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, plan-mode, implementation, workflow, design, documentation]
    related_skills: [test-driven-development, requesting-code-review]
---

# Plan-only work

Use this skill for an explicit plan-only request. Reading it while auditing skills does not turn an implementation task into plan mode. Do not invoke plan-only mode merely because an authorized implementation has multiple steps.

## Boundaries

For a plan-only request, inspect relevant context with read-only tools. Do not implement code, edit project files other than the plan, commit, push, launch workers or execute external mutations. A plan is not permission to execute its commands.

If the user later authorizes implementation, follow that latest request and its scope. Do not keep stopping at planning checkpoints just because the original request was plan-only.

## Write a useful plan

Resolve the goal, acceptance criteria, existing architecture and important constraints. Read the source and documentation that affect the proposed changes; do not require a full repository survey for a small task.

Include only what the implementer needs:

- Goal and scope, including exclusions.
- Relevant current behavior, assumptions and unresolved facts.
- Coherent implementation steps with dependencies and known file paths.
- Observable acceptance tests and how to run them.
- Risks, real approval boundaries and rollback approach.

Use concrete paths and commands when verified. Label expected outputs as expectations, never execution results. Do not invent test counts, line numbers, APIs or complete implementations to make a plan appear precise.

Choose task size by a meaningful behavior or dependency, not a fixed minute quota. Code examples are useful for an ambiguous contract; a full copy-paste implementation for every step is unnecessary. Use the repository's test workflow and explain the required evidence rather than repeating a universal test/commit recipe after every action.

## Save and deliver

Use a runtime-provided exact target when available. Otherwise, for a local markdown plan, write beneath the active workspace's `.hermes/plans/` directory with a descriptive filename. If a timestamp is needed, obtain it from a tool rather than guessing.

Existing task and knowledge governance takes precedence over this local default. Do not create a competing canonical plan or a new external destination. A local working draft does not authorize a Notion write, schema change or task activation.

After saving, provide the path and the material decisions or open questions. Do not claim the implementation or tests ran. Do not prescribe a fresh subagent per task or promise an automatic execution handoff; the current task's model, ownership and approval constraints determine how implementation may proceed.
