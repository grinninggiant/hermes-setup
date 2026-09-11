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
