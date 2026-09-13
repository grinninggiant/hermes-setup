# Repository and authentication prerequisites

Load `github-auth`. Use the existing approved profile-scoped broker and repository credential helper. Bare CLI authentication status may intentionally be empty because the broker injects short-lived credentials only into its child process. Verify access with a metadata-only repository read through that surface; never print helper output or fall back to personal credentials.

## Resolve the exact repository

Inspect the worktree and configured fetch/push destinations without dumping secrets. A remote URL may contain credentials; never echo raw URLs or embed them in evidence. Prefer the approved wrapper's repository metadata for canonical owner/name and host.

If parsing is required, validate the full URL format before extracting fields. Distinguish HTTPS, SCP-style SSH and ssh:// URLs. Reject embedded credentials, unexpected hosts, missing owner/repository segments and malformed paths rather than repairing them with a permissive substitution. GitHub Enterprise hosts require their own explicitly authorized endpoint; never redirect them to github.com by assumption.

A repository can have different fetch/push URLs, multiple remotes and Git URL rewrites. Verify the effective destination for the requested operation. Do not automatically choose origin or change global Git rewrites as a side effect. SSH connectivity proves neither the approved actor nor permission to use that identity.

## Confirm scope

Read canonical repository identity through the authorized API and compare it with the intended target. Repository redirects or renamed owners require explicit reconciliation; a successful lookup does not validate a malformed source identifier.

Confirm the PR's head repository/ref and base repository/ref separately, especially for forks. Local branch names and commit author metadata are not authenticated actor evidence.

Do not create a new OAuth flow, token, GitHub App, installation or wider scope to make a task work without the applicable approval. If the approved access path is unavailable, report its exact metadata-safe error and preserve the boundary.

## Ready to proceed

Require an identified worktree, authorized destination, verified repository identity and known target branch/PR before mutation. Keep credentials, personal runtime state and unrelated files out of commits and logs. Authentication readiness is separate from permission for a particular write, CI status or deployment acceptance.
