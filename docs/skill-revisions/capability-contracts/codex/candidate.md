---
name: codex
description: "Delegate coding to OpenAI Codex CLI (features, PRs)."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI delegation

Use only when delegation to the standalone Codex CLI is requested or otherwise explicitly permitted. A coding task alone is not permission to start a separate model/session. If workers or Codex sessions are prohibited, continue in the current authorized session instead. This skill is distinct from Hermes using the `openai-codex` provider.

## Preflight

- Check the installed `codex --help` and `codex exec --help`; do not install, log in or widen credential access implicitly.
- Resolve the authorized repository/worktree, model, account and sandbox policy. A missing `OPENAI_API_KEY` does not establish missing auth; the CLI can have its own authorized OAuth state. Never print auth stores.
- Default repo checks protect scope. The CLI may expose `--skip-git-repo-check`, but its existence is not permission to bypass project boundaries. Prefer the requested repository or an isolated approved worktree.
- Keep sandbox and approval controls intact. `workspace-write` is a sandbox mode, not proof that all operations are approved. Do not use bypass flags or full-access modes to cure a sandbox/host failure.

## Bounded execution

`codex exec` is noninteractive; a PTY is not a universal requirement. Interactive Codex requires a suitable terminal; follow the host terminal tool's actual PTY/background contract.

When execution is authorized, a one-shot example is:

```text
terminal(command="codex exec 'Implement the exact scoped change and run its tests'", workdir="/path/to/authorized/repo", timeout=300)
```

Use foreground execution for bounded work when practical. For longer work, use the available terminal tool's tracked background mode with completion notification; retain the returned process identity, output and stop path. Do not invent a `process` tool name: discover the installed process-management schema. Stop must cancel the owned execution, not unrelated sessions.

## Reviews and parallel work

For a read-only review, inspect the installed `codex review --help` or `codex exec review --help` and use the existing GitHub auth/PR workflow. Do not clone, post or publish a review under a bare request to inspect local code.

Parallel worktrees are optional, not a default quota. If explicitly authorized, assign non-overlapping ownership, verify the base revision and preserve all existing work. Give each child a bounded acceptance contract and inspect its actual diff and tests. Do not accept a child summary as independent proof of completion.

## Failure and delivery

A host sandbox failure is not a product-test failure. Keep the denial visible and use only an already-authorized verification surface; no fallback may disable security controls. If read-only access prevents temporary test files, report the tests as unrun rather than passing or failing the product.

After a permitted run, verify exit state, requested files, real test results and any external PR/commit read-back. Distinguish partial work from delivery. Clean up only owned worktrees/processes within the approved scope; never force-remove another contributor's changes.
