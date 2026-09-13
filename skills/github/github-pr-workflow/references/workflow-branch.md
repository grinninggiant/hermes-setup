# Create an isolated branch

Load `github-auth` and verify repository/remote identity through `references/workflow-prerequisites.md` before fetching. Fetch uses network authentication; branch preparation is not necessarily a purely local operation.

Inspect `git status`, the current branch, staged changes and `git worktree list`. Preserve unrelated work. Do not automatically checkout main, pull into a busy worktree, stash another task's changes, reset or clean the repository.

Determine the intended base from the real task and repository. Fetch that base through the approved remote, resolve its exact commit, and record it. A stale remote-tracking ref is not evidence of the latest remote state; if fetch fails, label the baseline explicitly rather than pretending it was refreshed.

Prefer a separate worktree when the current checkout belongs to another task. After validating the target branch and absolute destination, use the supported Git worktree/branch command with an explicit base commit. Check for an existing branch or worktree first; reuse only when its identity and existing work match this task. Never force-reassign an occupied branch.

Use a descriptive repository-consistent name such as `feat/<capability>`, `fix/<defect>`, `docs/<topic>` or `ci/<check>`. Examples are patterns, not literal branch names to copy.

Verify the new worktree's root, branch and HEAD against the intended base before editing. Confirm the original worktree's unrelated changes remain intact. Creation is not push, PR creation, deployment or acceptance.
