# Push and open a pull request

Load `github-auth`; use only the existing approved repository credential helper and broker/CLI. Do not construct token-bearing curl commands, change credential scope or fall back to personal authentication.

## Preflight and push

- Inspect the worktree, exact intended commit, repository identity, remote and branch. Preserve unrelated changes. Do not infer the target repository or base branch from a tutorial example.
- Confirm the reviewed diff and local tests. Stage only intended paths; check for secrets and private runtime/personal data before publication.
- Push the exact authorized local branch to its intended remote ref. Do not force-push unless explicitly authorized for the exact history rewrite.
- Read back the remote ref and compare its SHA to the intended commit. Push transport success alone is not verification.

## Deduplicate and create

Read existing pull requests for the exact head repository/ref and base repository/ref, including pagination. Reuse an appropriate open PR rather than creating another. If a matching PR was merged or closed, inspect its state before choosing the next operation.

Use an operation supported by the approved wrapper. For an authorized REST creation route, send `POST repos/{owner}/{repo}/pulls` with explicit `title`, `body`, `head`, `base` and `draft`. For a fork, use the provider-supported qualified head syntax after verifying the owner and repository. Consult the installed CLI help if using native PR creation; supply the exact repository, head and base rather than relying on current-directory defaults.

The body should contain the actual change, test commands/results and remaining limitations. Do not copy fictional features, issue numbers, passing checkboxes or closure keywords from a template. Use `Closes`/`Fixes` only for a real intended GitHub issue whose automatic closure is authorized; a PR must not bypass human-owned Linear Done. Add reviewers and labels only when required and authorized, not as an automatic notification side effect.

## Read-back and uncertain outcomes

After creation, GET the exact PR and verify its number/URL, repository, head ref/SHA, base ref, title/body, draft state and open status. A returned number alone is not sufficient.

On timeout or ambiguous failure, search again for that exact head/base before retrying. Do not create a duplicate because the initial response was lost. Report a rejected operation without changing the broker allowlist or identity.

PR creation is not merge, green CI, deployment or task acceptance. Continue with `references/workflow-ci-status.md` and the reviewed merge procedure when authorized.
