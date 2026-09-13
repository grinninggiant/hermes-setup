# End-to-end workflow map

This is an operation map, not a copy-and-run shell script or proof that any step succeeded. Use actual task identifiers, source files and test results; never copy fictional issue closure or commit claims.

1. Resolve the worktree, repository, approved authentication surface and intended head/base: `references/workflow-prerequisites.md`.
2. Preserve unrelated work and establish an isolated branch at the verified base commit: `references/workflow-branch.md`.
3. Implement the real change using repository conventions and relevant tests. Review and commit only the intended diff: `references/workflow-commit.md`.
4. Push, verify the remote SHA, deduplicate and create/read back the exact PR: `references/workflow-open-pr.md`.
5. Evaluate all applicable CI surfaces for the correct revision, including pagination and required-check coverage: `references/workflow-ci-status.md`.
6. Diagnose and repair confirmed failures without loosening tests or access controls: `references/workflow-ci-repair.md`.
7. When authorized, merge with the reviewed head guard, read back the merged state and clean only the verified eligible branch: `references/workflow-merge.md`.

A tool being installed does not authorize a new authentication path. Do not choose raw curl or personal credentials merely because the approved broker lacks a command. Respect its boundary and report the exact blocker.

Keep local tests, remote push, CI, merge, deployment and human-owned task completion separate. Resume safely from read-back after uncertain outcomes; do not rerun this sequence blindly.
