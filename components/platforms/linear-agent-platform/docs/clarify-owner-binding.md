# Clarify-only missing requester binding

A normal Linear clarification reply can use the current issue's human assignee
when the active turn has no requester ID. This is not a general authorization
fallback: signed ingress, exact active question/session/issue, open vendor session,
current delegate and app actor, and an assignee with `app is False` matching the
incoming actor must all validate. Missing or mismatched evidence remains denied.

The fallback is request-local. Never assign the resolved owner to SessionSource
or persist it as command authority. Slash commands, Stop, native approvals,
terminal state and credential controls retain their existing paths. A pre-existing
non-empty requester mismatch must not fall back to the owner. Direct activation
can use the fallback only when requester identity is absent.

Validation: the missing-requester regression failed with `clarify_actor_mismatch`
on the base revision, then passed. Negative metadata cases and command denial
after a successful reply are covered in `tests/test_clarify_owner_binding.py`.
The production client's live vendor query was separately exercised and returned
an exact human owner plus matching issue delegate/session app actor.
Independent read-only review found no blockers in the authorization diff.
Unit/review/vendor-read evidence does not replace post-promotion human reply and
model-continuation canary evidence. Deployment scope is general only; no fleet
role, identity, credential or policy rollout is implied.

## Immutable native-owner ingress

Neither registration nor an outbox `pending`/`in_flight` claim proves publication.
Inbound replies require the unsuppressed outbox row and the same immutable Hermes
session/turn owner used by outbound delivery. `verify_clarify_reply` proves that
the exact client-generated question activity precedes the exact human prompt,
with matching session, activity types, actor and body, and no intervening control.
Only a live waiter permits earlier ordinary human prompts between that question
and answer: a rejected requester or invalid choice must not poison the next valid
reply. Commands, signals, newer/ambiguous prompts, other questions, terminal and
unknown activity types still fail closed. `verify_late_clarify_reply` retains the
strict no-intervening-prompt rule for paused-goal rearm.
[Linear's prompted activity](https://linear.app/developers/agent-interaction) is a
user message, not a receipt for the outgoing elicitation; its ID alone cannot prove
which question was visible. No speculative pre-create publication marker is used.

This check runs on retries too: a later create ACK cannot retroactively make an
older unrelated prompt an answer. Missing/malformed evidence or unavailable vendor
reads remain HTTP 503 with no consumed waiter, done delivery or resolution receipt.
The same read admits a genuine published reply before the create transport ACK.
After vendor evidence, look up the current core session again, then recheck the
exact active event, waiter object, owner and closure fence under the preemption
lock. The async lookup is a snapshot, so resolution also checks the live core
routing entry under its in-memory store lock. That lock is acquired nonblocking
and released before ledger I/O; contention returns HTTP 503. No await occurs
between the final check and registry resolution. Reads stay outside the
preemption lock so Stop can cancel promptly.

The inbound delivery row pins the initial question ID, or an empty string for the
normal-prompt lane. The additive nullable `deliveries.clarify_id` column is local
ledger state, not vendor schema. Classified claims survive `release()` as retryable
rows; a replay cannot bind to a successor question, including after ledger reopen.
Unclassified claims retain the existing delete-on-release behavior. Completed and
released retry rows use the existing inbound retention window, except that pending
acceptance-thought obligations retain their receipt until delivery or cancellation.
Both nullable migrations (`clarify_id` and `acceptance_thought_json`) are preserved.

An unpublished stale callback is canceled by its own ID and the human input follows
normal admission. A stale published waiter yields HTTP 503 without marking the
reply done or emitting a resolution receipt. If the captured waiter expires, normal
late-answer admission handles the input rather than resolving the new FIFO head.
Normal fallback disables core's generic interaction interceptor only for that event;
slash and Stop paths remain unchanged. Its delivery is acknowledged only after the
core admission receipt, and accepted-work thought is not emitted for a failed/vetoed
admission. The ordinary webhook path commits that thought obligation with `done`
before scheduling it; there is no extra pre-dispatch or prompt-only thought. A
failed thought INSERT retries only delivery, never core admission or clarify binding.
Actor and authoritative terminal rejections remain non-resolution fences.

`tests/test_inbound_clarify_owner.py` uses signed loopback HTTP, bounded barriers,
real queued core staging/callback registration, core admission and FIFO interception,
and a fixture vendor transport. Both stale-consumption races failed before the fix.
It also covers pending/in-flight pre-create prompts (both reproduced RED), later
ACK plus ledger reopen, fast replies before vendor ACK, transient evidence-read
failure, durable retry binding, migration, rotation, completion, Stop, closure,
actor/delegate/Done and malformed owners. The transport fixture records activity
creation and human prompt chronology; tests exercise the production vendor-evidence
parser/verifier, not a verifier mocked to accept.
`tests/test_inbound_clarify_chronology.py` adds signed wrong-requester→owner,
nested-author and invalid-choice→correction probes (including before create ACK).
All three corrections reproduced HTTP 503 before repair; a real core reset
during vendor evidence reproduced HTTP 200 to the old waiter. The repaired tests
also cover reset after the final async snapshot, store-lock contention and Stop
during evidence reads. The same correction remains denied by strict late rearm.
The paired core is `548f6d6b509d076bc9a0b44eeab12813b659fa3c`. The canonical
runner passed the full component plus ten affected core clarify/store files:
39 files, 1,676 tests, zero failures, isolated HOME and file retries disabled.
These are offline contract results, not deployment or live-vendor acceptance.

## Late normal answer after timeout (0.8.28)

The distinct late-answer gate was explicitly approved on OPS-238. It is not a
general paused-goal resume capability. After a real normal-question timeout, the
staged-completion boundary records that turn's fresh native blocked revision in
the existing question outbox payload. No new table, policy or credential scope.

A fresh signed normal prompt may rearm only that exact paused revision. Require
the same Hermes session, delivered unsuppressed question, bounded remaining
budget, consistent signed nested author, live human issue assignee, app/delegate,
nonterminal issue and open session. Paginate vendor activities to prove the exact
question and answer, rejecting intervening Stop, newer prompts/questions, terminal
activities and unknown activity types. Re-read the goal after vendor I/O; record
the answer ID before native resume, without resetting budget or sending a
synthetic event. Slash/approval/Stop paths and global requester identity do not
change. Missing old-version timeout markers are NOT backfilled from prose.

State/outbox interruption: before resume the same answer may retry against the
same revision; after resume, active-state ingress does not rearm twice. New goal
or pause revision invalidates the old marker. Native judging and existing staged
outbox delivery remain the sole owners of continuation and final response.

Diagnostics distinguish `native_goal_not_rejudged`, `native_goal_paused` and
`late_clarify_unverified`; raw judge prose is neither authorization nor published
evidence. Test the captured timeout→paused→late answer→fresh judge→single response
sequence using `tests/test_late_clarify_goal.py` against the serving core, with a
temporary Hermes home and per-file subprocess runner. This release uses Derya's
same-session self-review only, NOT the independent review noted above for the
earlier requester change. Vendor read-only probes and unit tests do not establish
a live delayed-human-answer or post-final delivery PASS. Rollout is general-only;
preserve the exact deployment rollback coordinates and human-owned issue state.
