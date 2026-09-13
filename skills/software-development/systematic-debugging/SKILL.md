---
name: systematic-debugging
description: "Use when diagnosing a reproducible bug, failed test, build, or integration."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development]
---

# Systematic debugging

Diagnose the mechanism behind the reported symptom before changing it. Use the smallest useful feedback loop; do not turn a clear error into a mandatory architecture survey.

## Establish the failing path

Read the actual error, relevant stack frames and recent changes. Trace the affected caller and data flow far enough to identify the boundary that fails. Compare a working case when that helps distinguish causes. Consult only the documentation relevant to that boundary.

Create or identify a bounded, repeatable command that detects the exact symptom: an existing test, a fixture-driven CLI call, a development HTTP request, a browser assertion, or a sanitized trace replay. Run it before changing the implementation. Dependency/setup errors do not count as reproducing the bug.

If reproduction is intermittent, record its frequency and conditions; control time, randomness and concurrency where possible. Choose a bounded sample suited to the failure, not an arbitrary repetition quota. If a live mutation has unknown effects, inspect its exact state before any retry.

## Test a cause, not a guess

State the most plausible falsifiable explanation and the observation that would distinguish it. Maintain alternatives only when evidence warrants them. Probe one variable at a time; a precise error may need just one hypothesis.

For multi-component failures, inspect inputs, outputs, identity/config propagation and durable state at the relevant boundary. Keep diagnostic output metadata-only where credentials or personal data could appear. Add temporary instrumentation only when existing read-only evidence is insufficient, and remove it before shipping.

Minimize a reproduction while preserving the failure. Use a debugger, narrow test, controlled replay or bisection when it reduces uncertainty. Do not gather an entire repository or every service log by default.

## Async and store-race tests

Pause a worker before it acquires the contested store lock. Blocking inside an ownership callback under that lock while the event-loop thread synchronously resets the store deadlocks the test instead of reproducing a stale write. Bound every threading wait, release barriers in `finally`, and cap each test-file subprocess so failures cannot orphan workers.

Exercise the real caller, async facade and locked store together when ownership arguments cross those boundaries. Calling the store alone misses a dropped guard at the caller.

## Fix and verify

Add or reuse a regression test that reaches the confirmed failure. For a test-first behavior change, use `test-driven-development`. Implement the smallest root-cause fix; avoid unrelated cleanup and stacked speculative patches.

Run the exact reproduction after the fix, then affected regression/integration tests and repository-required checks. Run the full suite only when the change surface warrants it. Verify the real runtime or external system when that is part of the request; configuration parity or unit tests alone are not live acceptance.

If the fix fails, return to the evidence instead of layering another workaround. Repeated failures across shared state or multiple callers are a reason to revisit the mechanism and scope, not proof that a larger rewrite is required. Continue safe investigation autonomously; ask only when a real decision, approval boundary, or unavailable human fact blocks progress.

## Completion and boundaries

Report the reproduced symptom, supported cause, exact change, actual test results and unresolved limitations. Separate confirmed causes from hypotheses and verified fixes from mitigations. Do not claim universal performance gains or success rates from this procedure.

No automatic worker launch, production mutation, credential change or restart follows from loading this skill. Preserve current task ownership, Stop and approval boundaries. Continue through the requested fix and verification rather than ending at a diagnosis when safe implementation is authorized.
