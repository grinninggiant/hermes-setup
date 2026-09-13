---
name: github-pr-workflow
description: "GitHub PR lifecycle: branch, commit, open, CI, merge."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git, Automation, Merge]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request Workflow

Use for an existing repository’s branch, commit, PR, CI and merge lifecycle. Authentication setup belongs to `github-auth`; load it before selecting an authentication path. A documentation example does not authorize a new credential or wider scope.

## Procedure

1. Inspect the repository and existing work; identify the exact files and target branch.
2. Load only the reference for the next operation below.
3. Execute through the already authorized Git/GitHub surface. Preserve unrelated work.
4. Read back the exact remote target after a write. Report CI absence, failure and success separately.
5. Continue the authorized lifecycle; retain human-owned approval and completion boundaries.

## Operation references

- `references/workflow-overview.md` — Original introduction.
- `references/workflow-prerequisites.md` — Authentication and owner/repository detection; load github-auth first.
- `references/workflow-branch.md` — Create the branch.
- `references/workflow-commit.md` — Stage exact files and create the commit.
- `references/workflow-open-pr.md` — Push and open a pull request.
- `references/workflow-ci-status.md` — Read commit statuses and check runs.
- `references/workflow-ci-repair.md` — Diagnose and repair a failing check.
- `references/workflow-merge.md` — Merge and remove the merged branch.
- `references/workflow-example.md` — End-to-end example.
- `references/workflow-commands.md` — Individual PR actions.

## Scope of this reorganization

Operation references use exact repository/revision identity, approved authentication, unrelated-work preservation and remote read-back. They do not authorize personal-credential fallback, security bypass or human-owned completion. `references/ci-troubleshooting.md` covers failure diagnosis; `references/conventional-commits.md` and `templates/` are formatting aids, not evidence of tests or authorization to close an issue. Documentation review is separate from live workflow and fleet acceptance.

## Verification

Require the requested GitHub state and exact commit/PR identity to be read back. A local test, created PR, merge response or transport receipt alone is not proof of the entire workflow.
