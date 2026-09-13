---
name: simplify-code
description: "Use when asked to simplify recent code changes."
version: 1.1.0
author: Hermes Agent (inspired by Claude Code /simplify)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [code-review, cleanup, refactor, simplify]
    related_skills: [requesting-code-review, test-driven-development]
---

# Simplify recent code changes

Improve the requested changes without turning cleanup into a whole-repository rewrite. Use this when simplification is requested, not automatically after every edit. For a correctness/security review, use `requesting-code-review`.

## Resolve scope

Honor an explicit file, staged diff, commit, branch or focus. Otherwise inspect uncommitted tracked changes with `git diff`; use `git diff HEAD` to include staged changes. Check untracked files relevant to the request separately. If no changes exist, use only files the user actually named; do not invent a cleanup target.

Read the diff and enough surrounding code to understand behavior and contracts. For a large diff, review coherent groups while retaining cross-file context. Do not ask for a smaller scope merely because splitting the work internally is necessary.

A dry-run request prohibits edits. A focus limits the review to that angle unless a material correctness issue must be reported.

## Review angles

Perform the useful angles in the current session. Delegation is optional only when authorized, supported by the actual tool schema and justified by task size; a worker/model restriction requires inline review, not another launch mechanism.

- **Reuse:** Find existing helpers, constants and mechanisms before introducing duplicates. Name the actual reusable implementation; hypothetical helpers are not findings.
- **Quality:** Look for redundant state, parameter sprawl, copy-paste variation, leaky abstractions, stringly typed contracts and needless nesting. Avoid cosmetic churn and restating obvious code in comments.
- **Efficiency:** Examine repeated work, duplicate I/O, N+1 access, blocking hot paths, unbounded caches, listener/handle leaks and excessive closure capture. Preserve synchronization and error semantics; an apparent pre-check may implement a deliberate safety boundary.
- **Design depth:** Distinguish a root-mechanism fix from a caller-specific patch or stacked workaround. Compatibility shims, staged migrations and vendor isolation may be intentional. Investigate their purpose before removing them.

Use surrounding code, tests and targeted history when intent is unclear. Dead-code tools and a failed text search are leads, not proof: dynamic imports, reflection and external callers may still depend on the symbol.

## Findings and application

For each material finding record:

`file:line → problem → concrete cost → suggested fix → confidence → risk`

Deduplicate overlapping findings and drop weak or style-only suggestions. Prioritize correctness, then the requested focus, then clarity/reuse, then micro-optimization. A conflicting suggestion is not an excuse to apply both.

- **SAFE:** Proven behavior-preserving cleanup within the requested scope. Apply unless dry-run.
- **CAREFUL:** Refactors requiring targeted coverage or caller verification. Apply only with that evidence and within the authorized scope.
- **RISKY:** Public API, schema, concurrency, lifecycle or uncertain behavior changes. Flag for human review; cleanup alone does not authorize them.

Do not remove error handling merely because an error looks benign. Do not rename exported identifiers or configuration keys without checking their contracts. A deeper fix outside scope should be reported, not silently converted into a new architecture project.

## Verification and completion

Run targeted tests for changed paths and relevant repository lint/type checks. Broaden testing when the impact warrants it. Diagnose failures and reverse only this task's faulty edits when needed; never discard unrelated user work.

Complete the requested authorized cleanup and verification, then summarize actual changes, checks, skipped findings and remaining risks. State whether review was inline or delegated; never report reviewers that did not run. If a correctness bug is discovered, distinguish it from behavior-preserving cleanup and report the evidence explicitly.
