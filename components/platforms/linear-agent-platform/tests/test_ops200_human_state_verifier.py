"""Native-session evidence for the human-owned OPS-200 state guard."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_linear_tools
from acceptance import acceptance_criteria, authenticate_evidence_envelope
from ledger import DeliveryLedger
from linear_tools import _OPS200_ISSUE_ID, execute_with_clients, register_outbound_tools
from outbound_ledger import OutboundLedger
from outbound_policy import OutboundPolicy

TEXT = "Human-owned issue state’i korunur; agent Done girişimleri reddedilir."
DESCRIPTION = "## Kabul kriterleri\n- [ ] " + TEXT
REVISION = "2026-09-24T05:00:00.000Z"


class HumanStateVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.home = Path(self.tempdir.name) / "general"
        self.home.mkdir()
        self.env_patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.ctx = test_linear_tools.FakeContext()
        self.root = Path(self.tempdir.name)
        register_outbound_tools(self.ctx, extra=test_linear_tools.RegistrationTests.extra(self, mutations=True))
        self.issue = {
            "id": _OPS200_ISSUE_ID, "identifier": "OPS-200", "description": DESCRIPTION,
            "updatedAt": REVISION, "delegate": {"id": "actor-1"},
        }
        self.plan = {
            "id": _OPS200_ISSUE_ID, "title": "OPS-200", "description": DESCRIPTION,
            "updatedAt": REVISION, "team": {"id": "ops-1"},
            "state": {"id": "started-1", "type": "started"},
            "assignee": {"id": "human-1", "app": False}, "delegate": {"id": "actor-1"},
        }
        self.terminal = {
            "id": _OPS200_ISSUE_ID, "updatedAt": REVISION, "description": DESCRIPTION,
            "team": {"id": "ops-1"}, "state": {"id": "started-1", "type": "started"},
            "creator": {"id": "actor-1"}, "delegate": {"id": "actor-1"},
            "parent": None,
        }
        self.graphql = mock.MagicMock(actor_id="actor-1", organization_id="org-1")
        self.graphql.connect = mock.AsyncMock()
        self.graphql.close = mock.AsyncMock()
        self.graphql.get_agent_turn_context = mock.AsyncMock(return_value={
            "id": "session-1", "status": "active", "app_user_id": "actor-1", "issue": self.issue,
        })
        self.graphql.get_issue_plan_context = mock.AsyncMock(return_value=self.plan)
        self.graphql.get_issue_child_terminal_context = mock.AsyncMock(return_value=self.terminal)
        self.env = {
            "HERMES_SESSION_PLATFORM": "linear", "HERMES_SESSION_PROFILE": "general",
            "HERMES_SESSION_ID": "session-1", "HERMES_SESSION_CHAT_ID": "session-1",
        }
        self.kwargs = {"session_id": "session-1", "turn_id": "turn-1"}

    def verify(self):
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            return json.loads(asyncio.run(self.ctx.tools["linear_verify_ops200_human_state"]["handler"](
                {}, **self.kwargs)))

    def test_only_real_human_owned_top_level_guard_yields_one_use_proof(self):
        envelope = self.verify()
        self.assertEqual(envelope["result"], "PASS", envelope)
        self.assertEqual(envelope["criterion_hash"], acceptance_criteria(DESCRIPTION)[0].criterion_hash)
        self.assertEqual(envelope["observed_revision"], REVISION)
        self.graphql.get_issue_plan_context.assert_awaited()
        self.graphql.get_issue_child_terminal_context.assert_awaited()
        self.assertNotIn("human-1", json.dumps(envelope))
        self.assertNotIn("actor-1", json.dumps(envelope))
        # Resolver is passed to the mark_acceptance handler, never model-controlled.
        resolver = self.capture_resolver(envelope)
        def authenticate():
            return authenticate_evidence_envelope(
                envelope, issue_id=_OPS200_ISSUE_ID, delegate_id="actor-1", resolver=resolver,
                expected_agent_session_id="session-1", expected_hermes_turn_id="turn-1",
            )
        self.assertIsNotNone(authenticate())
        self.assertIsNone(authenticate())

    def capture_resolver(self, envelope):
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        execution = mock.AsyncMock(return_value={"error": "offline"})
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
              mock.patch("linear_tools.execute_with_clients", new=execution),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            asyncio.run(self.ctx.tools["linear_save_issue"]["handler"]({
                "id": _OPS200_ISSUE_ID, "target_team_id": "ops-1", "operation_key": "state-probe",
                "lifecycle_action": "mark_acceptance", "expected_updated_at": REVISION,
                "description": DESCRIPTION.replace("[ ]", "[x]"), "acceptance_evidence": [envelope],
            }, **self.kwargs))
            assert execution.await_args is not None
            return execution.await_args.kwargs["evidence_resolver"]

    def test_same_turn_mark_reads_back_exact_checkbox_and_consumes_proof(self):
        envelope = self.verify()
        resolver = self.capture_resolver(envelope)
        checked = DESCRIPTION.replace("[ ]", "[x]")
        before = {**test_linear_tools.ExecutionTests.plan_context(
            updated_at=REVISION, description=DESCRIPTION,
        ), "id": _OPS200_ISSUE_ID}
        after = {**test_linear_tools.ExecutionTests.plan_context(
            updated_at="2026-09-24T05:01:00.000Z", description=checked,
        ), "id": _OPS200_ISSUE_ID}
        ledger = OutboundLedger(str(self.root / "outbound.sqlite3"))
        acceptance_ledger = DeliveryLedger(str(self.root / "acceptance.sqlite3"), startup_recovery=False)
        mcp = test_linear_tools.FakeMCP()
        try:
            result = asyncio.run(execute_with_clients(
                profile_id="general", vendor_tool="save_issue",
                arguments={
                    "id": _OPS200_ISSUE_ID, "target_team_id": "ops-1", "operation_key": "state-test",
                    "lifecycle_action": "mark_acceptance", "expected_updated_at": REVISION,
                    "description": checked, "acceptance_evidence": [envelope],
                },
                mutation=True, policy=OutboundPolicy(
                    expected_actor_id="actor-1", expected_organization_id="org-1",
                    allowed_team_ids={"ops-1"},
                ),
                ledger=ledger, acceptance_ledger=acceptance_ledger,
                evidence_resolver=resolver, expected_agent_session_id="session-1",
                expected_hermes_turn_id="turn-1",
                graphql_client=test_linear_tools.FakeGraphQL(plan_contexts=[before, before, after]),  # type: ignore[arg-type]
                mcp_client=mcp,  # type: ignore[arg-type]
            ))
            self.assertEqual(result.get("status"), "success", result)
            self.assertEqual(
                [call for call in mcp.calls if call[0] == "save_issue"],
                [("save_issue", {"id": _OPS200_ISSUE_ID, "description": checked}, True)],
            )
            self.assertEqual(acceptance_ledger.acceptance_evidence_hashes(
                _OPS200_ISSUE_ID, "actor-1", accepted_revision=after["updatedAt"],
            ), {acceptance_criteria(checked)[0].criterion_hash})
        finally:
            acceptance_ledger.close()
            ledger.close()

    def test_drift_or_wrong_owner_denies_proof(self):
        self.plan["assignee"]["app"] = True
        self.assertEqual(self.verify()["reason"], "ops200_human_state_verification_failed")
        self.plan["assignee"]["app"] = False
        self.terminal["parent"] = {"id": "parent-1"}
        self.assertEqual(self.verify()["reason"], "ops200_human_state_verification_failed")

    def test_ownership_drift_between_proof_and_mark_denies_before_vendor(self):
        envelope = self.verify()
        self.plan["assignee"]["app"] = True
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            result = json.loads(asyncio.run(self.ctx.tools["linear_save_issue"]["handler"]({
                "id": _OPS200_ISSUE_ID, "target_team_id": "ops-1", "operation_key": "state-drift-test",
                "lifecycle_action": "mark_acceptance", "expected_updated_at": REVISION,
                "description": DESCRIPTION.replace("[ ]", "[x]"), "acceptance_evidence": [envelope],
            }, **self.kwargs)))
        self.assertEqual(result.get("reason"), "acceptance_evidence_invalid", result)
        mcp.connect.assert_not_awaited()

    def test_registered_handler_marks_only_proven_checkbox_with_remote_readback(self):
        envelope = self.verify()
        after = {**self.plan, "description": DESCRIPTION.replace("[ ]", "[x]"),
                 "updatedAt": "2026-09-24T05:01:00.000Z"}
        self.graphql.get_issue_plan_context.side_effect = [self.plan, self.plan, self.plan, after]
        self.graphql.get_issue_team_id = mock.AsyncMock(return_value="ops-1")
        fake_mcp = test_linear_tools.FakeMCP()
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        mcp.call_tool = mock.AsyncMock(side_effect=fake_mcp.call_tool)
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            result = json.loads(asyncio.run(self.ctx.tools["linear_save_issue"]["handler"]({
                "id": _OPS200_ISSUE_ID, "target_team_id": "ops-1", "operation_key": "state-handler-e2e",
                "lifecycle_action": "mark_acceptance", "expected_updated_at": REVISION,
                "description": after["description"], "acceptance_evidence": [envelope],
            }, **self.kwargs)))
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual([call for call in fake_mcp.calls if call[0] == "save_issue"], [
            ("save_issue", {"id": _OPS200_ISSUE_ID, "description": after["description"]}, True),
        ])
        ledger = DeliveryLedger(str(self.root / "database"), startup_recovery=False)
        try:
            self.assertEqual(ledger.acceptance_evidence_hashes(
                _OPS200_ISSUE_ID, "actor-1", accepted_revision=after["updatedAt"],
            ), {acceptance_criteria(after["description"])[0].criterion_hash})
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
