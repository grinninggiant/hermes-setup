# Review and commit exact changes

Before editing, read the relevant files and preserve unrelated work. Use `patch` for targeted edits and `write_file` for deliberate complete writes. Follow repository tests and load `requesting-code-review` when preparing a commit or merge.

## Review and stage

1. Verify the worktree and branch belong to this task. Inspect both working-tree and already-staged diffs; a pre-existing index entry must not be swept into this commit.
2. Run the relevant tests and inspect the complete intended diff, including new files. A filename or diffstat alone is not review. Check for credentials, private runtime state, personal data and generated artifacts before publication.
3. Stage only exact approved paths. For mixed-task edits within one file, isolate the intended change without discarding the other work; do not assume path-level staging is sufficiently narrow.
4. Inspect the staged diff and run `git diff --cached --check`. Whitespace checks do not replace behavior tests or content review.

## Commit

Use the repository's commit-message convention. Describe the change actually made, not a fictional feature from a tutorial. A typical form is `type(scope): short description`; common types are feat, fix, refactor, docs, test, ci, chore and perf.

Preserve the configured authorized identity and signing policy. Do not change global user settings, disable hooks, use verification bypasses or amend unrelated commits to make a commit succeed. Diagnose a failed hook; security/credential changes require their own approval.

After committing, read back the exact commit SHA and changed paths, inspect the resulting diff, and verify remaining working-tree/index state. Commit author metadata is not authenticated GitHub actor evidence.

A local commit is not a remote write receipt, green CI, deployment or task completion. Continue through the authorized push/PR workflow and exact remote read-back. Report any uncommitted or failing state honestly.
