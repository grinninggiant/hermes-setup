"""Offline vertical slice: plugin tool execution -> independent acceptance read-back."""
import asyncio
import hashlib
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

TEXT = "9/9 SOUL fresh read-back’te exact delegate-owned acceptance kuralını taşır."
DESCRIPTION = "## Kabul kriterleri\n- [ ] " + TEXT
REVISION = "2026-08-30T20:00:00.000Z"
NAMES = "general assistant researcher coder writer producer marketing health finance".split()
LINES = (b"mark_acceptance canonical test fixture", b"Acceptance checkbox varsa canonical test fixture")


class Ops200SoulVerifierTests(unittest.TestCase):
    extra = test_linear_tools.RegistrationTests.extra

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.root.chmod(0o700)
        self.profiles = self.root / "profiles"
        for name in NAMES:
            home = self.profiles / name
            home.mkdir(parents=True)
            (home / "SOUL.md").write_bytes(b"\n".join(LINES) + b"\n")
        self.home_env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.profiles / "general")})
        self.home_env.start()
        self.line_hashes = mock.patch("linear_tools._OPS200_SOUL_LINE_HASHES", tuple(
            (marker, hashlib.sha256(line).hexdigest()) for marker, line in
            ((b"mark_acceptance", LINES[0]), (b"Acceptance checkbox varsa", LINES[1]))
        ))
        self.line_hashes.start()
        self.ctx = test_linear_tools.FakeContext()
        register_outbound_tools(self.ctx, extra=self.extra(mutations=True))
        self.env = {
            "HERMES_SESSION_PLATFORM": "linear", "HERMES_SESSION_PROFILE": "general",
            "HERMES_SESSION_ID": "hermes-native", "HERMES_SESSION_CHAT_ID": "agent-native",
        }
        self.kwargs = {"session_id": "hermes-native", "turn_id": "turn-native"}
        self.issue = {
            "id": "issue-200", "identifier": "OPS-200", "description": DESCRIPTION,
            "updatedAt": REVISION, "delegate": {"id": "actor-1"},
        }
        self.graphql = mock.MagicMock(actor_id="actor-1", organization_id="org-1")
        self.graphql.connect = mock.AsyncMock()
        self.graphql.close = mock.AsyncMock()
        self.graphql.get_agent_turn_context = mock.AsyncMock(return_value={
            "id": "agent-native", "status": "active", "app_user_id": "actor-1",
            "issue": self.issue,
        })

    def tearDown(self):
        self.line_hashes.stop()
        self.home_env.stop()
        self.tempdir.cleanup()

    def verify(self, *, kwargs=None):
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            return json.loads(asyncio.run(self.ctx.tools["linear_verify_ops200_soul"]["handler"](
                {}, **(self.kwargs if kwargs is None else kwargs))))

    def test_issue_bound_verifier_resolver_replay_and_drift(self):
        self.assertIn("linear_verify_ops200_soul", self.ctx.tools)
        envelope = self.verify()
        criterion = acceptance_criteria(DESCRIPTION)[0]
        self.assertEqual(envelope["criterion_hash"], criterion.criterion_hash)
        self.assertEqual(envelope["result"], "PASS")
        self.assertEqual(envelope["observed_revision"], REVISION)
        self.assertNotIn("mark_acceptance", json.dumps(envelope))
        resolver = self.get_resolver(envelope)
        def authenticate(value=envelope, turn="turn-native"):
            return authenticate_evidence_envelope(
                value, issue_id="issue-200", delegate_id="actor-1", resolver=resolver,
                expected_agent_session_id="agent-native", expected_hermes_turn_id=turn,
            )
        self.assertIsNotNone(authenticate())
        self.assertIsNone(authenticate())  # single-use
        stale = self.verify()
        self.assertIsNone(authenticate(stale, turn="turn-old"))
        self.assertIsNone(authenticate(stale))
        drift = self.verify()
        (self.profiles / "finance" / "SOUL.md").write_bytes(LINES[0] + b"\nchanged\n")
        self.assertIsNone(authenticate(drift))
        self.assertEqual(self.verify()["reason"], "ops200_soul_verification_failed")

    def test_checked_ops200_criterion_issues_fresh_proof_without_vendor_checkbox_write(self):
        checked = DESCRIPTION.replace("[ ]", "[x]")
        self.issue["id"] = _OPS200_ISSUE_ID
        self.issue["description"] = checked
        envelope = self.verify()
        self.assertEqual(envelope.get("result"), "PASS", envelope)
        resolver = self.get_resolver(envelope)
        context = {**test_linear_tools.ExecutionTests.plan_context(
            updated_at=REVISION, description=checked,
        ), "id": _OPS200_ISSUE_ID}
        policy = OutboundPolicy(
            expected_actor_id="actor-1", expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
        )
        ledger = OutboundLedger(str(self.root / "checked-outbound.sqlite3"))
        acceptance_ledger = DeliveryLedger(
            str(self.root / "checked-acceptance.sqlite3"), startup_recovery=False,
        )
        mcp = test_linear_tools.FakeMCP()
        try:
            result = asyncio.run(execute_with_clients(
                profile_id="general", vendor_tool="save_issue",
                arguments={
                    "id": _OPS200_ISSUE_ID, "target_team_id": "ops-1",
                    "operation_key": "ops200-checked-proof-test",
                    "lifecycle_action": "mark_acceptance",
                    "expected_updated_at": REVISION, "description": checked,
                    "acceptance_evidence": [envelope],
                },
                mutation=True, policy=policy, ledger=ledger,
                acceptance_ledger=acceptance_ledger,
                evidence_resolver=resolver,
                expected_agent_session_id="agent-native",
                expected_hermes_turn_id="turn-native",
                graphql_client=test_linear_tools.FakeGraphQL(plan_contexts=[context, context]),  # type: ignore[arg-type]
                mcp_client=mcp,  # type: ignore[arg-type]
            ))
            self.assertEqual(result.get("status"), "already_accepted", result)
            self.assertEqual(
                acceptance_ledger.acceptance_evidence_hashes(
                    _OPS200_ISSUE_ID, "actor-1", accepted_revision=REVISION,
                ),
                {acceptance_criteria(checked)[0].criterion_hash},
            )
            self.assertFalse(any(call[0] == "save_issue" for call in mcp.calls))
            self.assertIsNone(authenticate_evidence_envelope(
                envelope, issue_id=_OPS200_ISSUE_ID, delegate_id="actor-1",
                resolver=resolver, expected_agent_session_id="agent-native",
                expected_hermes_turn_id="turn-native",
            ))  # pointer consumed by the accepted call
        finally:
            acceptance_ledger.close()
            ledger.close()

    def test_verifier_evicts_old_unconsumed_pointers(self):
        envelopes = [self.verify() for _ in range(33)]
        resolver = self.get_resolver(envelopes[0])
        self.assertIsNone(authenticate_evidence_envelope(
            envelopes[0], issue_id="issue-200", delegate_id="actor-1",
            resolver=resolver, expected_agent_session_id="agent-native",
            expected_hermes_turn_id="turn-native",
        ))
        self.assertIsNotNone(authenticate_evidence_envelope(
            envelopes[-1], issue_id="issue-200", delegate_id="actor-1",
            resolver=resolver, expected_agent_session_id="agent-native",
            expected_hermes_turn_id="turn-native",
        ))

    def test_wrong_session_issue_criterion_and_revision_denied_before_vendor_mutation(self):
        self.assertEqual(self.verify(kwargs={})["reason"], "acceptance_provenance_unavailable")
        for change in ({"identifier": "OPS-201"}, {"description": "## Kabul kriterleri\n- [ ] other"},
                       {"updatedAt": ""}, {"delegate": {"id": "other"}}):
            with self.subTest(change=change):
                self.issue.update(change)
                self.assertEqual(self.verify()["reason"], "ops200_soul_verification_failed")
                self.issue = {"id": "issue-200", "identifier": "OPS-200", "description": DESCRIPTION,
                              "updatedAt": REVISION, "delegate": {"id": "actor-1"}}
                self.graphql.get_agent_turn_context.return_value["issue"] = self.issue
        self.graphql.get_agent_turn_context.assert_awaited()

    def test_real_acceptance_gate_rejects_old_turn_then_reads_back_new_pointer(self):
        checked = DESCRIPTION.replace("[ ]", "[x]")
        before = {**test_linear_tools.ExecutionTests.plan_context(
            updated_at=REVISION, description=DESCRIPTION,
        ), "id": "issue-200"}
        after = {**test_linear_tools.ExecutionTests.plan_context(
            updated_at="2026-08-30T20:01:00.000Z", description=checked,
        ), "id": "issue-200"}
        policy = OutboundPolicy(
            expected_actor_id="actor-1", expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
        )
        ledger = OutboundLedger(str(self.root / "outbound.sqlite3"))
        acceptance_ledger = DeliveryLedger(
            str(self.root / "acceptance.sqlite3"), startup_recovery=False,
        )
        try:
            async def run(envelope, turn, graphql, mcp, resolver):
                return await execute_with_clients(
                    profile_id="general", vendor_tool="save_issue",
                    arguments={
                        "id": "issue-200", "target_team_id": "ops-1",
                        "operation_key": "ops200-soul-test", "lifecycle_action": "mark_acceptance",
                        "expected_updated_at": REVISION, "description": checked,
                        "acceptance_evidence": [envelope],
                    },
                    mutation=True, policy=policy, ledger=ledger,
                    acceptance_ledger=acceptance_ledger,
                    evidence_resolver=resolver,
                    expected_agent_session_id="agent-native",
                    expected_hermes_turn_id=turn,
                    graphql_client=graphql, mcp_client=mcp,
                )

            stale = self.verify()
            stale_resolver = self.get_resolver(stale)
            stale_mcp = test_linear_tools.FakeMCP()
            result = asyncio.run(run(
                stale, "turn-old", test_linear_tools.FakeGraphQL(plan_contexts=[before]),
                stale_mcp, stale_resolver,
            ))
            self.assertEqual(result.get("reason"), "acceptance_evidence_invalid")
            self.assertFalse(any(call[0] == "save_issue" for call in stale_mcp.calls))

            fresh = self.verify()
            fresh_resolver = self.get_resolver(fresh)
            live_mcp = test_linear_tools.FakeMCP()
            result = asyncio.run(run(
                fresh, "turn-native",
                test_linear_tools.FakeGraphQL(plan_contexts=[before, before, after]),
                live_mcp, fresh_resolver,
            ))
            self.assertEqual(result.get("status"), "success")
            self.assertEqual(
                [call for call in live_mcp.calls if call[0] == "save_issue"],
                [("save_issue", {"id": "issue-200", "description": checked}, True)],
            )
        finally:
            acceptance_ledger.close()
            ledger.close()

    def get_resolver(self, envelope):
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        execute = mock.AsyncMock(return_value={"error": "linear_policy_denied", "reason": "offline"})
        args = {"id": self.issue["id"], "target_team_id": "ops-1", "operation_key": "one-test",
                "lifecycle_action": "mark_acceptance", "expected_updated_at": REVISION,
                "description": DESCRIPTION.replace("[ ]", "[x]"), "acceptance_evidence": [envelope]}
        with (mock.patch("linear_tools.LinearOAuthStore"),
              mock.patch("linear_tools.LinearClient", return_value=self.graphql),
              mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
              mock.patch("linear_tools.execute_with_clients", new=execute),
              mock.patch("gateway.session_context.get_session_env", side_effect=lambda k, d="": self.env.get(k, d))):
            asyncio.run(self.ctx.tools["linear_save_issue"]["handler"](args, **self.kwargs))
        self.assertIsNotNone(execute.await_args)
        mcp.call_tool.assert_not_called()
        return execute.await_args.kwargs["evidence_resolver"]
