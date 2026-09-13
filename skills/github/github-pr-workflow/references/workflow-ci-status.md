# Read CI for the exact revision

Load `github-auth` and use the existing approved broker/CLI. Do not build token-bearing curl commands or widen authorization when a route is denied.

## Establish identity

Read the exact PR and repository, record its current head SHA and base branch, then inspect checks for that revision. The shell's local HEAD may differ from the PR head. If a merge queue or pull-request workflow tests a synthetic merge revision, preserve its relationship to the head/base and evaluate the repository's actual required-check policy rather than assuming all SHAs must be identical.

## Gather complete evidence

Read both commit statuses and check runs through authorized endpoints. Where relevant, read workflow run and attempt metadata. Follow pagination; reconcile returned totals and do not treat the first page as the complete check set.

A combined commit status does not include every check-run conclusion. Do not stop polling merely because that one endpoint says success. Match required check names and provider identities against the repository's applicable rules. If rules cannot be read, report the visible results and keep required-check completeness unverified; do not change permissions or branch protection to make the check pass.

Separate queued/in-progress from completed runs. Completed is not synonymous with successful. Report success, failure, cancelled, timed-out, action-required, neutral, skipped and absent results accurately. Neutral/skipped handling depends on the actual repository policy; never universally label them PASS. No checks recorded means no CI evidence, not green CI.

## Bounded monitoring

Use a supported native watch operation when authorized, targeting the exact PR/repository. Otherwise poll through the approved API surface with bounded waits and rate-limit-aware backoff. Keep monitoring owned by the current execution; do not create an orphan daemon or new model worker.

On each meaningful refresh, re-read the PR head. If it changed, restart evaluation for the new revision while retaining the old result as historical evidence. A timeout, unavailable API or incomplete pagination leaves the outcome pending/PARTIAL, never successful.

## Handoff

For a confirmed failure, use `references/workflow-ci-repair.md`. Before merging, refresh required-check evidence and the PR head, then follow `references/workflow-merge.md` with the reviewed SHA guard.

Report repository, PR, evaluated SHA(s), workflow/run attempts, required-check coverage and remaining unknowns separately. Local tests, CI, merge and deployment are distinct acceptance layers. Read-only monitoring does not authorize a workflow rerun, deployment or settings change.
