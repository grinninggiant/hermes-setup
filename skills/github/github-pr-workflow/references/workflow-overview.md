# Pull request lifecycle

Use the operation router in `SKILL.md` and load only the reference needed for the next step. Authentication belongs to `github-auth`; an unavailable CLI does not authorize a raw-token fallback or a different identity.

The lifecycle is verified repository/base selection, isolated implementation, reviewed commit, remote push read-back, deduplicated PR creation, revision-specific CI, guarded merge and verified branch cleanup. Each layer needs its own evidence; none automatically establishes deployment or human-owned task completion.

For the linked sequence, read `references/workflow-example.md`. Preserve external provenance and profile-specific differences; these methods do not authorize broad rollout or access changes.
