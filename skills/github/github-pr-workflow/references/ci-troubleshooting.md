# CI failure diagnosis

Use `references/workflow-ci-repair.md` for exact revision/run selection, approved log access, safe artifact handling, repair and remote verification. This reference classifies symptoms; it does not provide a second authentication or retry workflow. Load `systematic-debugging` for a reproducible failure.

## Tests

An assertion failure is evidence of a mismatch, not proof that the assertion is wrong. Establish the intended contract from the requirement and reproduce the failure before changing code or expectations. Do not weaken assertions, delete tests or add retries simply to get green CI. For flaky tests, identify the race, timing, shared state or external dependency and verify the repair with a focused reproduction. A mock must preserve the contract being tested.

## Imports and dependencies

A module-not-found error may mean an incorrect interpreter, missing installation step, wrong package path, undeclared dependency or incompatible environment. Compare local and CI runtimes and lockfiles before adding packages. Update dependencies with the repository's package manager and narrowly scoped lockfile changes; do not use a whole-environment freeze as a generic lockfile repair. Do not silently install untrusted packages or incur external spending.

## Formatting and type errors

Identify the exact configured formatter/type checker and failing files. Run the repository-supported command on the intended scope; inspect any generated diff before staging. Do not format unrelated files by default.

For a type error, inspect the real value, signature and control flow. Correct the mismatch rather than adding a cast or ignore to hide it. A justified, narrow suppression needs an explanation and evidence that behavior remains correct; blanket disabling of checks is not a repair.

## Build and containers

Read the exact failing step and compare runtime versions, dependency constraints, build context and tracked inputs. For COPY/ADD failures, check both file presence and ignore rules. Preserve reproducibility; do not replace a pinned image with an unverified floating tag. Image execution, privileged mode and credential injection remain separately authorized actions.

## Permission and authentication

Distinguish missing access, expired credentials, fork-event restrictions and incorrect repository identity using metadata-safe diagnostics. Fork restrictions may intentionally withhold secrets. Do not expose protected credentials to untrusted code or switch to a privileged event as a workaround.

Permission blocks, secrets, installation scopes and security controls are not routine CI fixes. Apply the explicit approval boundary before changing them. Use the existing approved auth owner and surface; never print secret values or change identity to evade a denial.

## Timeouts and infrastructure

Find the step that actually timed out and separate user cancellation, job cancellation, resource limits, hung processes and external-service latency. Investigate the cause before raising timeouts or splitting jobs. Concurrency changes may increase spending or contention. Reruns can have deployment or billing side effects and require the workflow-specific scope check.

## Verification

Run the minimal relevant local test, review the exact diff, then follow the authorized commit/push process. Re-read the remote SHA and CI results for that revision using `references/workflow-ci-status.md`. Do not suppress command errors with a fallback that falsely appears successful. No recorded CI, incomplete logs or inaccessible required-check rules leave evidence incomplete.
