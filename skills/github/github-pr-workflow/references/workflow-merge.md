# Merge and branch cleanup

Load `github-auth` before selecting a transport. Use the existing approved profile-scoped broker or authenticated CLI; never assemble token-bearing curl commands or switch to a personal identity when the broker denies an operation.

## Preflight

1. Read the exact repository and PR. Record PR number, base repository/branch, head repository/ref/SHA, draft state and merge state. Do not infer the PR head from the shell's current branch.
2. Verify the reviewed diff, required checks and applicable approval rules against that head SHA. Distinguish no recorded CI from successful CI. Do not use admin bypass or change branch protection to complete a merge.
3. Re-read the head immediately before mutation. If it changed, review and test the new revision rather than merging the stale one.

## Merge

Use only a command supported by the approved wrapper. Where the wrapper permits GitHub's REST merge endpoint, issue `PUT repos/{owner}/{repo}/pulls/{pull_number}/merge` with explicit `merge_method` and `sha` equal to the reviewed head. The `sha` field prevents silently merging a different revision.

If the authorized CLI supports native PR merge, supply the exact PR and repository and use its supported head-matching option; confirm syntax from installed help. Never omit identity and rely on current-directory inference.

After an error or timeout, read the exact PR before retrying. A request receipt is not proof of merge. Verify `merged`, `merged_at` and `merge_commit_sha`, then inspect the resulting base-branch history. Squash/rebase may produce a different commit from the reviewed head; preserve both identities in evidence.

Auto-merge is a distinct pending state, not completion. Use it only within authorized scope and verify its final outcome. Do not enable repository settings or widen GraphQL/broker permissions as a workaround.

## Cleanup

Only after remote merge read-back succeeds, identify the branch from the PR's head repository and ref. Confirm it is not the default/protected/base branch, is owned within authorized scope, and has not advanced past the reviewed head or acquired unrelated work. A fork branch is not automatically ours to delete.

Delete the exact eligible remote branch through the authorized surface. Read back its absence. A failed deletion is cleanup pending, not merge failure. Never delete the shell's current branch merely because it happens to be checked out.

Preserve unrelated local changes and worktrees. Do not automatically checkout/pull a different branch in a busy worktree. Remove a local merged branch only after verifying it is no longer needed or checked out elsewhere.

## Evidence

Report reviewed head, merged commit, exact PR read-back, CI status and cleanup status separately. Source tests or merge success do not establish deployment, live behavior or human-owned task completion.
