# OPS-221 — paused-answer delivery diagnostic

Status: partial candidate, NOT approved for merge/promotion. No live acceptance claim.

## Reproduced boundary

Serving plugin 0.8.32 matches source adapter at 08fe0cc. The serving core executable resolves to release 8488dc3717b5b22ed1923b108dd6bd54c47adc30.

The historical native turn completed a user-facing explanation. Its persisted decision was blocked/completed/native_goal_paused. In `_prepare_native_owned_turn_delivery`, a freshly paused native goal maps to blocked; `_enqueue_turn_terminal_activity` replaces the intended response with a fixed diagnostic notice. The response text is not passed to that function. This is separate from budget exhaustion and an authoritative Stop event.

Regression `test_paused_judge_keeps_completed_human_answer_without_authorizing_success` was run RED on the unmodified adapter: the expected answer was absent from the actual SQLite outbox body. The candidate preserves a bounded, redacted main-turn answer in the error receipt; it never forwards private judge reason, resumes a goal, or authorizes a response.

## Executed verification

Using the paired core's `scripts/run_tests.sh`, clean environment and isolated per-file processes:

- Regression baseline: 1 failed for the expected missing-answer assertion.
- First candidate focused package: 102 passed.
- Candidate including safety negatives: 18 files, 1293 passed, 0 failed.
- Safety negatives: Stop, failed/interrupted/incomplete execution, internal wake, complete vendor session, awaitingInput; no answer forwarding or response authorization.
- Exact retry retains one decision and one deterministic outbox key.
- `git diff --check`: clean.

These are executable fixture/integration results, not live vendor acceptance. No synthetic webhook or human answer was sent.

## Self-review: why this is not the final fix

The candidate conserves explanatory text but still emits an error and closes the execution. It therefore does NOT implement the requested nonterminal explanatory answer plus continued independent technical work. Do not merge/promote it as the OPS-221 repair.

The existing goal contract says broadly to stop when blocked or awaiting human input. The downstream adapter sees only native paused state plus live lifecycle gates; it cannot safely infer whether a remaining human-event canary blocks all authorized work. Parsing private judge prose or blindly resuming paused state is not an acceptable fix.

The next implementation must separate user-facing explanatory delivery from task completion and preserve explicit human-wait/technical-blocker distinctions, including independent-work continuation. It must exercise real post-turn judging, classification, outbox drain and vendor visibility. Preserve Stop/approval/ownership, bounded budgets, human Done, checked criteria, and general-only scope. No additional human Stop/test question is needed to reproduce the current defect.

## Production state

No config, runtime/plugin promotion, restart, fleet operation, acceptance mutation, or issue-state change was performed for this candidate. General health remained 0.8.32 with pending/in_flight/dead outbox counts all zero; degraded health separately reports an existing stuck direct activation. That health condition is not attributed to this defect.
