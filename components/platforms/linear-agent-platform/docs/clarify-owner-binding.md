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
