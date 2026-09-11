# OPS-221 — cached-continuation progress context

Status: tested candidate, NOT deployed; no Linear acceptance claim.

## Exact scope

Restore the resolved Hermes session ID in gateway task-local context so existing
Linear tool/interim progress hooks can match their exact session owner on cached
continuation turns. No new progress grant, fallback to process-global identity,
Stop/approval bypass, goal resume or final-response authorization.

This is separate from the paused-answer candidate e79188c; that candidate remains
unmerged and is not included here. Native paused-answer disposition and incomplete
OPS-221 acceptance remain unresolved. Fleet rollout is outside current authorization.

## Coordinates

- Serving/core base: `8488dc3717b5b22ed1923b108dd6bd54c47adc30`.
- Core candidate: `c8bbae9715ef2715c196cac78842684a5888cb7c` on
  `grinninggiant/hermes-agent`, `fix/ops-221-continuation-session-context`.
- Plugin test base: `08fe0cc` (`hermes-setup` main / serving plugin 0.8.32).
- Core production diff: one keyword argument, `session_id=context.session_id`,
  in `GatewayRunner._set_session_env`. Vendor semantics remain outside core.

## Evidence

Native read-back during OPS-221 execution: AgentSession
`62335ba5-d84d-4539-be0b-a165f65168a7` remained active; ordinal 1 continued with
Hermes session `20260911_171943_a169ca1e`. The previous model summary was a thought,
not a terminal response. Subsequent tool/interim progress was absent while
heartbeat activities continued. This observation alone is not acceptance.

1. RED core executor regression on unmodified core: session ID was empty instead
   of `resolved-hermes-session`; 1 failed / 8 passed.
2. Paired RED using the real gateway context/executor and existing plugin hooks
   (no mock of get_session_env): on base runtime the expected four progress
   scheduling calls across two turns were zero (`0 != 4`). Vendor transport is
   mocked; no vendor events or live canary results were fabricated.
3. Same paired test on candidate core: 1 passed / 0 failed. Two turns each schedule
   one tool-stage and one semantic update to the exact fixture AgentSession.
4. Full candidate gateway suite using canonical clean-environment runner:
   814 files, **8027 passed, 0 failed, 45 skipped** (117.9 s). Platform-specific
   skips are not PASS.
5. Full shipping-plugin suite plus new paired regression against candidate core:
   18 files, **1282 passed, 0 failed** (7.5 s).
6. Core negatives cover concurrent owner A / owner B / explicitly empty ID,
   stale os.environ masking, no process-global mutation, and repeated binding
   without constructing a new agent. Existing plugin mismatch/fence tests remain.
7. Core push read-back via git ls-remote matches the full candidate SHA above.

## Reproduction

Run from the relevant core checkout with its canonical test runner:

```
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/gateway/
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh <plugin-tests>/test_native_platform.py -k cached_continuation_binds_progress_via_real_gateway_context
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh <plugin-tests>/
```

`<plugin-tests>` is this directory. A stock/base core is expected to fail the new
paired regression until the core session-context fix is available. Pyright in a
standalone plugin worktree may report unresolved gateway imports; runtime imports
are exercised against the exact paired core, not substituted with stubs.

## Remaining gates

No independent review is claimed. No merge, immutable activation, general restart
or live post-continuation visibility verification is claimed. Review authorization
must be resolved for this new core candidate without silently treating the earlier
specific drain-review exception as blanket permission. Same-session live
continuation and exact nonterminal vendor progress receipt remain required before
claiming production repair. Do not include the rejected paused-answer candidate
in a release.
