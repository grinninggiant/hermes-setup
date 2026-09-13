---
name: test-driven-development
description: "Use when implementing a behavior change with test-first development."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, tdd, development, quality, red-green-refactor]
    related_skills: [systematic-debugging]
---

# Test-driven development

Use a small RED → GREEN → REFACTOR cycle for each behavior slice. Follow the repository's test conventions and the actual task scope; this skill does not authorize deleting existing work, changing security controls, or starting workers.

## RED: specify and reproduce

Write a minimal test of the required observable behavior. Run it against the old implementation and confirm it fails for the intended missing behavior, not a syntax error, unavailable dependency, or broken fixture. For a bug, reproduce the reported symptom rather than a nearby failure.

A test that passes immediately may document existing behavior or provide useful characterization coverage; it does not demonstrate a regression was caught. Keep that distinction in the evidence. Preserve pre-existing or uncommitted work; isolate a baseline when testing an already-written change instead of deleting the implementation to perform the ritual.

Example:

```python
def test_retries_until_third_attempt():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")
        return "success"

    assert retry_operation(operation) == "success"
    assert attempts == 3
```

Also cover exhaustion and non-retryable errors when those are part of the contract. This example uses a local function; retrying real external side effects requires the operation's idempotency and approval rules.

## GREEN: implement one slice

Make the smallest correct implementation that satisfies the behavior. Do not add unrelated functionality. Prefer exercising real code; use controlled test doubles at external boundaries rather than asserting only that a mock returned its configured value.

Run the focused test again. Fix implementation defects without weakening valid assertions. If the test encoded the wrong requirement, correct it explicitly and re-establish what it proves.

## REFACTOR: preserve behavior

Remove duplication and clarify structure while keeping tests green. Work in vertical slices—one behavior and its implementation—rather than inventing a large suite against an interface that has not been exercised.

After the focused cycle, run affected regression/integration tests and the repository-required checks. Use the full suite for a broad change or an explicit repository requirement, not automatically after every small edit. Failed checks require diagnosis; do not hide unrelated failures or report a clean run when none occurred.

## Choosing evidence

- A executable behavior change benefits from a regression test reaching the changed path.
- A pure refactor can use existing characterization coverage; verify that it exercises the touched behavior.
- A documentation or configuration-only change needs its relevant parser, contract, loader, or runtime check—not an invented unit test just to satisfy TDD terminology.
- A throwaway experiment is not production acceptance. Retain the useful result and test the implementation that will actually ship.

Do not introduce a new approval checkpoint for safe local test iterations. Respect real production, credential, spending, destructive-operation and Stop boundaries.

## Completion

Deliver the requested implementation, the actual test commands/results, and any remaining limitations. Distinguish RED/GREEN evidence, regression coverage, and live integration acceptance. A string assertion or isolated unit test does not establish external-system behavior. Continue through fixing and verification within the authorized scope; do not stop merely because a first implementation exists.
