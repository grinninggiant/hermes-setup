"""Owner channels (local Desktop/TUI, Telegram DM) may mark acceptance with comment evidence."""
import unittest
from unittest import mock

import test_ops200_soul_verifier as base
from acceptance import acceptance_criteria, authenticate_evidence_envelope

TEXT = base.TEXT
COMMENT = "comment-1"


class OwnerChannelAcceptanceTests(unittest.TestCase):
    setUp = base.Ops200SoulVerifierTests.setUp
    tearDown = base.Ops200SoulVerifierTests.tearDown
    check = base.Ops200SoulVerifierTests.check
    extra = base.Ops200SoulVerifierTests.extra

    def local(self, **env):
        self.env = {
            "HERMES_SESSION_SOURCE": "desktop", "HERMES_SESSION_PROFILE": "general",
            "HERMES_SESSION_ID": "hermes-native", **env,
        }
        issue = {**self.issue, "state": {"name": "In Progress"}}
        self.graphql.get_issue_turn_context = mock.AsyncMock(return_value=issue)
        self.graphql.get_agent_turn_context = mock.AsyncMock(side_effect=AssertionError("no AgentSession"))
        self.graphql.get_comment_evidence = mock.AsyncMock(return_value={
            "id": COMMENT, "user_id": "actor-1", "issue_id": "issue-200",
            "body": f"Kanıt:\n- {TEXT} → PASS (state.db)\n",
        })

    def args(self, **extra):
        crit = acceptance_criteria(base.DESCRIPTION)[0].criterion_hash
        return {"criterion_hash": crit, "check": "evidence_comment", "equals": COMMENT,
                "issue_id": "issue-200", **extra}

    def test_local_owner_comment_evidence_binds_to_owner_session(self):
        self.local()
        envelope = self.check(self.args())
        self.assertEqual(envelope.get("result"), "PASS", envelope)
        self.graphql.get_issue_turn_context.assert_awaited_with("issue-200")
        evidence = {k: envelope[k] for k in (
            "criterion_hash", "test_class", "evidence_digest", "evidence_pointer",
            "observed_revision", "result", "timestamp")}
        resolver = base.Ops200SoulVerifierTests.get_resolver(self, evidence)
        self.assertIsNotNone(authenticate_evidence_envelope(
            evidence, issue_id="issue-200", delegate_id="actor-1", resolver=resolver,
            expected_agent_session_id="owner-session:hermes-native",
            expected_hermes_turn_id="turn-native"))

    def test_comment_must_be_derya_on_same_issue_with_pass_line(self):
        self.local()
        for patch in ({"user_id": "mutlu"}, {"issue_id": "issue-other"},
                      {"body": f"- {TEXT} → FAIL"}, {"body": "- başka kriter → PASS"}):
            with self.subTest(patch=patch):
                self.graphql.get_comment_evidence.return_value = {
                    "id": COMMENT, "user_id": "actor-1", "issue_id": "issue-200",
                    "body": f"- {TEXT} → PASS", **patch}
                self.assertEqual(self.check(self.args())["result"], "FAIL")

    def test_non_owner_channels_and_other_delegates_are_denied(self):
        denied = "acceptance_provenance_unavailable"
        self.local(HERMES_SESSION_SOURCE="api_server")
        self.assertEqual(self.check(self.args())["reason"], denied)
        self.local(HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_TYPE="group",
                   HERMES_SESSION_USER_ID="u", HERMES_SESSION_MESSAGE_ID="m")
        self.assertEqual(self.check(self.args())["reason"], denied)
        self.local()
        with mock.patch("linear_tools._delegated_child_context", return_value=True):
            self.assertEqual(self.check(self.args())["reason"], denied)
        self.local()
        self.graphql.get_issue_turn_context.return_value = {**self.issue, "delegate": {"id": "naz"}}
        self.assertEqual(self.check(self.args())["reason"], "criterion_verification_failed")
        self.assertEqual(self.check(self.args(issue_id=""))["reason"], "criterion_verification_failed")

    def test_telegram_dm_is_an_owner_channel(self):
        self.local(HERMES_SESSION_SOURCE="", HERMES_SESSION_PLATFORM="telegram",
                   HERMES_SESSION_CHAT_TYPE="dm", HERMES_SESSION_USER_ID="u",
                   HERMES_SESSION_MESSAGE_ID="m")
        self.assertEqual(self.check(self.args()).get("result"), "PASS")


if __name__ == "__main__":
    unittest.main()
