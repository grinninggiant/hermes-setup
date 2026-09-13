---
name: github-issue-to-pr
description: "Carry a GitHub issue to a verified PR with honest CI state."
version: 0.1.0
author: Ben Barclay (benbarclay), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Issues, Coding, Pull-Requests, CI]
    related_skills: [github-issues, github-pr-workflow, systematic-debugging, test-driven-development, requesting-code-review]
---

# GitHub Issue to Pull Request

Turn a GitHub issue into a tested, verified PR. This skill owns the end-to-end discipline — premise validation, duplicate sweeps, class-level fixes, and honest CI reporting; the sibling GitHub and development skills own their own mechanics.

## When to Use

- "Fix issue #123 and open a PR."
- "Implement this GitHub feature request."
- "Take this bug from issue to green CI."

Don't use for: reviewing an existing PR, or answering a code question with no requested change.

## Procedure

### 1. Read the live issue — body AND full thread

Use the already authorized GitHub surface to read the issue and paginate its comments (`gh issue view <N> --comments` when supported). Honor a scoped broker's allowed API routes; do not change credentials to make an example work. The body is a snapshot from filing time; the newest comments carry the live state: partial fixes already merged, new root-cause analyses, maintainer decisions, or questions directed at you that change the task. Also read repository instructions (`AGENTS.md`, contribution docs) with `read_file`. Done when the currently requested behavior, non-goals, and any unanswered thread questions are known.

### 2. Sweep for existing and duplicate work

Before implementation, search existing PRs by issue identifier and relevant symptom/subsystem terms. Broaden an empty or narrow result with useful synonyms rather than enforcing a fixed query quota. Popular issues attract multiple independent fixes; building a duplicate wastes the work and the credit. Also check whether a recent commit already fixed it: `git log --oneline -20 -- <relevant files>`. Record which searches, pages and relevant commits were covered; bounded searches do not establish that every related change was found.

### 3. Validate the premise against current code — and against design intent

Reproduce the bug or demonstrate the missing behavior on the current default branch with a failing test or fixture, using `search_files` and `read_file` to trace the reported path. Then check the second question: is the "bug" actually deliberate design? Run `git log -p -S "<symbol>"` on the code the issue wants changed and read the original commit's intent — a missing link or restriction is often the feature. Challenge stale or flawed issue prose instead of implementing it blindly. Done when the root cause or feature gap is demonstrated in current code AND the change doesn't fight an intentional design.

### 4. Define acceptance and risk

List acceptance criteria, interfaces, migrations/state changes, compatibility, security/privacy, rollout, and rollback. Map every criterion to a test or explicit verification. Done when review has a finite contract.

### 5. Implement the smallest complete change — and fix the class

Work on an isolated branch or worktree, loading `systematic-debugging` or `test-driven-development` when the bug class calls for them. Add regression tests first, then implement. When the fix is in hand, `search_files` for the same bug shape at sibling call sites and fix the whole class in this PR — an incomplete fix that leaves known siblings broken is worse than none. Every changed line must trace to the issue; no drive-by cleanup. Done when targeted tests pass, the original failure no longer reproduces, and sibling sites are fixed or explicitly ruled out.

### 6. Prove the regression test bites (sabotage run)

Use the recorded pre-fix RED result if it already proves the regression test fails for the intended reason. Otherwise compare exact pre-fix and fixed behavior in an isolated fixture or worktree; never sabotage a live deployment or another contributor's active changes. A regression test that passes with and without the fix proves nothing. Done when the test demonstrably fails on pre-fix code.

### 7. Run repository quality gates, then open the PR immediately

Run the formatter, lint, typecheck, and the repo's canonical test entrypoint on affected areas; use `requesting-code-review` on the diff. Then push and open the PR right away — the PR is what dispatches CI, and CI latency is the long pole; do not sit on finished work. Load `github-pr-workflow` for PR mechanics: conventional branch/commit, body linking the issue with problem, approach, tests, risk, and exclusions. Read the PR back and verify head SHA, base, title, and files. Done when the PR exists with the intended diff and the actual CI state is known. Report absent or not-triggered checks explicitly; creating a PR does not guarantee a CI run.

### 8. Shepherd CI honestly and close the loop

Inspect live checks and failure logs via `gh pr checks` / `gh run view --log-failed`. Distinguish failures introduced by your diff from pre-existing baseline or infrastructure failures — reproduce on the default branch when unsure, and use a bounded retry only when evidence supports a transient infrastructure failure. Never say "green," "merged," or "released" without live evidence of that exact state. When the PR lands, comment on the issue with the PR link and a one-line explanation so the reporter gets a traceable resolution. Done when CI state, remaining blockers, and the issue thread all reflect reality.

## Pitfalls

- Coding before reading issue comments, sweeping for duplicate PRs, or reading current code.
- "Fixing" behavior that the original commit shows is intentional design.
- Fixing a symptom at one call site while sibling sites keep the same bug.
- Shipping a regression test that also passes without the fix.
- Opening a PR with unrun tests or unrelated formatting churn.
- Claiming the issue is delivered because a PR exists.

## Verification

- [ ] Full issue thread read; newest comment state reflected in the plan.
- [ ] Duplicate-PR search covers the issue and relevant symptoms; pagination and coverage gaps are stated.
- [ ] Premise reproduced on current code; design intent checked via git history.
- [ ] Regression test proven to fail without the fix.
- [ ] Sibling call sites fixed or explicitly ruled out.
- [ ] Every changed line traces to the issue.
- [ ] CI state reported from live evidence only; issue commented with the PR link.
