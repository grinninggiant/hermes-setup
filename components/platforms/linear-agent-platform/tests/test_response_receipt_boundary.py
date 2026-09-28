"""Isolated transport-receipt regression; vendor responses are test fixtures."""
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

