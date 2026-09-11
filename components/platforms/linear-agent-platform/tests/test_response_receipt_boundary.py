"""Isolated transport-receipt regression; vendor responses are test fixtures."""
from types import MappingProxyType
from unittest import mock
import test_native_continuation as base


class ResponseReceiptBoundaryTests(base.NativeContinuationTests):
    async def test_receipt_recovery_is_bounded_without_resending(self):
        ledger = self.adapter._ledger
        ledger.enqueue_outbox("response-retry", "linear-session", "activity.create", {
            "activity_id": "response-id", "agent_session_id": "linear-session",
            "activity_type": "response", "body": "result",
            "response_create_acknowledged": True,
        })
        self.adapter._linear.verify_response_receipt = mock.AsyncMock(
            side_effect=base.LinearAPIError("receipt unavailable", retryable=True)
        )
        self.adapter._linear.create_activity = mock.AsyncMock()
        # An acknowledged payload may be recovered even with no decision object.
        ledger.claim_due_outbox()
        ledger.reschedule_outbox("response-retry", "test retry", 0)
        item = ledger.get_outbox_item("response-retry")
        for _ in range(10):
            await base.LinearPlatformAdapter._drain_outbox_once(self.adapter)
            item = ledger.get_outbox_item("response-retry")
            if item["state"] == "dead":
                break
            ledger.reschedule_outbox("response-retry", "test retry", 0)
        self.assertEqual(item["state"], "dead")
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_response_is_not_delivered_until_post_send_receipt_is_verified(self):
        base.FakeGoalManager.existing = True
        base.FakeGoalManager.existing_status = "done"
        checked = await base.FakeLinear(
            description="## Acceptance\n- [x] artifact verified"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=checked)
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        self.adapter._linear.verify_response_receipt = mock.AsyncMock(
            side_effect=base.LinearAPIError("receipt unavailable", retryable=True)
        )
        event = base.turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "artifact result", event._gateway_turn_result
        )
        # Receipt is transport-owned: preparing the result cannot wait for its
        # own future delivery. All work acceptance gates remain in production.
        self.adapter._linear.verify_response_receipt.assert_not_awaited()
        await base.LinearPlatformAdapter._drain_outbox_once(self.adapter)
        self.adapter._linear.create_activity.assert_awaited_once()
        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-success:{event._linear_turn_decision_id}"
        )
        self.assertEqual(item["state"], "pending")
        self.adapter._linear.verify_response_receipt.assert_awaited_once()
        # A restart after vendor acceptance must reconcile the exact receipt,
        # not re-send or reject it because that response completed the session.
        self.adapter._ledger.reschedule_outbox(item["id"], "test retry", 0)
        ledger_path = str(base.Path(self.temp.name) / "ledger.sqlite3")
        self.adapter._ledger.close()
        self.adapter._ledger = base.DeliveryLedger(ledger_path, startup_recovery=False)
        self.adapter._linear.verify_response_receipt.side_effect = None
        self.adapter._linear.verify_response_receipt.return_value = False
        self.adapter._linear.get_agent_turn_context.return_value = {**checked, "status": "complete"}
        await base.LinearPlatformAdapter._drain_outbox_once(self.adapter)
        missing = self.adapter._ledger.get_outbox_item(item["id"])
        self.assertEqual(missing["state"], "pending")
        self.adapter._ledger.reschedule_outbox(item["id"], "test retry", 0)
        self.adapter._linear.verify_response_receipt.return_value = True
        await base.LinearPlatformAdapter._drain_outbox_once(self.adapter)
        self.adapter._linear.create_activity.assert_awaited_once()
        reconciled = self.adapter._ledger.get_outbox_item(item["id"])
        self.assertEqual(reconciled["state"], "delivered")
        self.assertEqual(self.adapter._linear.verify_response_receipt.await_count, 3)
