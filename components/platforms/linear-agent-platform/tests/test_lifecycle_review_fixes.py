"""Regression checks for the 0.8.55 lifecycle review fixes."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from ledger import DeliveryLedger  # noqa: E402
from linear_tools import _plan_sections_detail  # noqa: E402
import test_native_platform as native  # noqa: E402

build_agent_prompt = native.adapter_mod.build_agent_prompt


def _grant(ledger: DeliveryLedger, key: str, now: int) -> None:
    assert ledger.reserve_direct_activation_grant(
        operation_key=key, source_platform="telegram", source_user_id="u",
        source_message_id="m", source_session_id="s", source_profile="general",
        actor_id="a", team_id="t", issue_fingerprint="f", now=now,
    )


class LifecycleReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = DeliveryLedger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.ledger.close() if hasattr(self.ledger, "close") else None
        self.tmp.cleanup()

    def _state(self, key: str) -> tuple[str, str]:
        return self.ledger._db.execute(
            "SELECT state, last_error FROM direct_activation_grants WHERE operation_key=?", (key,)
        ).fetchone()

    def test_policy_denial_cancels_grant_not_fails(self):
        _grant(self.ledger, "k1", 1000)
        self.assertTrue(self.ledger.fail_direct_activation_grant(
            "k1", "quota_pre_dispatch_changed", state="canceled", now=1001))
        self.assertEqual(self._state("k1")[0], "canceled")
        self.assertEqual(self.ledger.direct_activation_counts(now=1002)["failed"], 0)

    def test_orphan_granted_grant_expires(self):
        _grant(self.ledger, "k2", 1000)
        self.assertTrue(self.ledger.bind_direct_activation_grant("k2", "issue-2", now=1001))
        self.assertEqual(self.ledger.expire_orphan_direct_grants(now=1002), 0)
        self.assertEqual(self.ledger.expire_orphan_direct_grants(now=1001 + 3600), 1)
        self.assertEqual(self._state("k2"), ("canceled", "granted_without_event_expired"))
        self.assertEqual(self.ledger.direct_activation_counts(now=1001 + 3601)["stuck_active"], 0)

    def test_granted_grant_with_failed_event_expires(self):
        _grant(self.ledger, "k3", 1000)
        self.assertTrue(self.ledger.bind_direct_activation_grant("k3", "issue-3", now=1001))
        self.ledger._db.execute(
            "INSERT INTO direct_activation_events(issue_id, session_id, delivery_key, prompt_json, "
            "state, last_error, created_at, updated_at) "
            "VALUES('issue-3', 's', 'd', '{}', 'failed', 'unbound_event_expired', 1001, 1001)"
        )
        self.assertEqual(self.ledger.expire_orphan_direct_grants(now=1001 + 3600), 1)
        self.assertEqual(self._state("k3")[0], "canceled")

    def test_plan_rejection_names_rule(self):
        self.assertEqual(_plan_sections_detail("short")[1], "plan_too_short")

    def test_direct_activation_prompt_does_not_claim_todo_move(self):
        payload = {"action": "created", "agentSession": {"id": "s", "issue": {"id": "i", "title": "T"}}}
        text = build_agent_prompt(payload, activation_resume=True, direct_activation=True)
        self.assertIn("Direct activation", text)
        self.assertNotIn("moved", text.split("Direct activation")[1].split("\n\n")[0])
        self.assertIn("lifecycle_action=start", build_agent_prompt(payload))


class PlanLimitTests(unittest.TestCase):
    def _applies(self, response):
        import asyncio
        from linear_client import workspace_issue_limit_applies

        class Client:
            async def graphql(self, _query, _variables=None):
                if isinstance(response, Exception):
                    raise response
                return response

        return asyncio.run(workspace_issue_limit_applies(Client()))

    def test_paid_plan_lifts_cap_unknown_keeps_it(self):
        self.assertFalse(self._applies({"organization": {"subscription": {"type": "basic_monthly_12"}}}))
        self.assertTrue(self._applies({"organization": {"subscription": {"type": "free"}}}))
        self.assertTrue(self._applies({"organization": {"subscription": None}}))
        self.assertTrue(self._applies(RuntimeError("down")))


if __name__ == "__main__":
    unittest.main()
