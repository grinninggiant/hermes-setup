---
name: requesting-code-review
description: "Use when verifying code changes before commit or merge."
version: 2.0.0
author: Hermes Agent (adapted from obra/superpowers + MorAlekss)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [code-review, security, verification, quality, pre-commit, auto-fix]
    related_skills: [plan, test-driven-development, github-code-review]
---

# Verify code before delivery

Review the requested code changes for security, correctness and regressions. For reviewing another contributor's GitHub PR, use `github-code-review`. Documentation/configuration changes need their applicable contract/loader checks rather than an automatic full code pipeline.

## Scope and baseline

Inspect staged and working-tree diffs, status and relevant untracked files. Review the requested change rather than silently falling back to an unrelated prior commit. Do not ask the user to stage files just to make review possible. Never stage unrelated work.

Use an isolated baseline worktree or fixture when a before/after comparison is needed; do not automatically stash/reset another contributor's active changes. Compare actual failing cases, not only failure counts, because equal counts can hide a new regression.

## Security and correctness

Inspect changed paths and relevant callers for hardcoded credentials, shell/SQL injection, unsafe deserialization, user-controlled eval/exec, path traversal, XSS, invalid authorization, error handling and concurrency/lifecycle defects. Use the project's installed scanners where applicable. Treat pattern matches as findings to investigate, not automatic proof of exploitability. Never print raw matching secrets into the review output.

Check observable behavior against the request, including failure paths. Run affected tests, installed lint/type checks and repository-required gates; broaden coverage for broad changes. Report unavailable checks explicitly. Do not install dependencies or contact production implicitly to fill a missing check.

## Review execution and fail-closed results

Review in the current session when separate workers are prohibited. Label this as self-review, not independent review. An independent reviewer is optional only when authorized and supported; if a repository or security policy requires independent approval, that gate remains unmet until an authorized reviewer actually provides it.

Unresolved security concerns, logic errors, regressions or an unparseable required reviewer verdict block a passing result. Missing evidence remains missing; a local test success cannot substitute for an independent approval required elsewhere.

## Repair and reverify

Fix confirmed defects within the authorized scope and rerun affected checks. Do not launch a third agent or impose a fixed attempt quota. If progress requires a new scope, security/credential change, destructive operation or unavailable human decision, identify the exact blocker. Preserve unrelated work and respect Stop.

If review passes and commit/push is authorized, stage only named task files, inspect the staged diff and commit using repository conventions. Do not add a `[verified]` label that implies an independent review did not occur. Review success does not itself authorize merging, deployment or human-owned issue completion.

## Delivery

Report actual changes, tested scope, results, review mode and unresolved findings. Separate pre-existing failures, new regressions, skipped checks and live acceptance. Continue through authorized fixes and verification; do not stop at the first implementation or a self-imposed review-count limit.
