"""Partial hardening: readiness is not a ten-second delivery guarantee."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import test_created_admission_deadline as admission


class FirstActivityDeliverabilityTests(unittest.IsolatedAsyncioTestCase):
    make_payload = admission.CreatedAdmissionDeadlineTests.make_payload
    request_for = admission.CreatedAdmissionDeadlineTests.request_for
    asyncSetUp = admission.CreatedAdmissionDeadlineTests.asyncSetUp
    asyncTearDown = admission.CreatedAdmissionDeadlineTests.asyncTearDown
    slow_read = admission.CreatedAdmissionDeadlineTests.slow_read
    post = admission.CreatedAdmissionDeadlineTests.post
    configure_activation = admission.CreatedAdmissionDeadlineTests.configure_activation

    def delivery_key(self):
        return admission.probe.fixtures.adapter_mod._delivery_key(
            self.payload, self.request_for(self.payload)._body,
        )

    async def test_initial_owner_stall_retries_without_admission(self):
        await self.assert_owner_stall_before_admission()

    async def test_manager_owner_stall_precedes_dispatch_claim(self):
        await self.assert_owner_stall_before_admission("manager")

    async def test_direct_owner_stall_restores_unattempted_claim(self):
        await self.assert_owner_stall_before_admission("direct")

    async def assert_owner_stall_before_admission(self, kind=None):
        activation = self.configure_activation(kind) if kind else None
        original = self.adapter._linear.get_agent_session_delivery_context
        self.adapter._linear.get_agent_session_delivery_context = self.slow_read
        ledger = self.adapter._ledger
        key = self.delivery_key()
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            started = time.monotonic()
            post = self.post(self.payload)
            await asyncio.wait_for(self.entered.wait(), timeout=1)
            response = await asyncio.wait_for(asyncio.shield(post), timeout=4.9)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["status"], "unavailable")
            ingress.assert_not_awaited()
            self.assertFalse(self.handler_entered.is_set())
            self.assertFalse(ledger.delivery_is_done(key))
            self.assertFalse(ledger.pending_acceptance_thoughts(key))
            self.assertIsNone(ledger.get_outbox_item(f"activity:thought:{key}"))
            self.assertEqual(self.adapter._linear.calls, [])
            if activation:
                self.assertEqual(activation("issue-164")["state"],
                                 "delegated" if kind == "manager" else "granted")
            self.adapter._linear.get_agent_session_delivery_context = original
            accepted = await asyncio.wait_for(self.post(self.payload), timeout=1)
            self.assertEqual(accepted.status, 200)
            await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
            await asyncio.wait_for(self.adapter._wait_for_thought("linear-session"), timeout=1)
            replay = await asyncio.wait_for(self.post(self.payload), timeout=1)
            self.assertEqual((await replay.json())["status"], "duplicate")
            await asyncio.wait_for(self.adapter._wait_for_thought("linear-session"), timeout=1)
            self.assertEqual(ingress.await_count, 1)
            self.assertTrue(ledger.delivery_is_done(key))
            self.assertEqual(len([c for c in self.adapter._linear.calls if c[1] == "thought"]), 1)

    async def test_send_time_owner_stall_is_bounded_retryable_not_dead_lettered(self):
        original = self.adapter._linear.get_agent_session_delivery_context
        ledger = self.adapter._ledger
        key = self.delivery_key()
        item_id = f"activity:thought:{key}"

        async def owner(session_id):
            # Gate succeeds; only the fresh post-admission read is unavailable.
            if ledger.delivery_is_done(key):
                return await self.slow_read(session_id)
            return await original(session_id)

        self.adapter._linear.get_agent_session_delivery_context = owner
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            response = await asyncio.wait_for(self.post(self.payload), timeout=1)
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
            await asyncio.wait_for(self.entered.wait(), timeout=1)
            started = time.monotonic()
            await asyncio.wait_for(self.adapter._wait_for_thought("linear-session"), timeout=4.9)
            self.assertLess(time.monotonic() - started, 5)
            item = ledger.get_outbox_item(item_id)
            self.assertEqual(item["state"], "pending")
            self.assertIn("timed out", item["last_error"])
            self.assertEqual(ledger.outbox_counts()["dead"], 0)
            self.assertEqual(self.adapter._linear.calls, [])
            self.assertTrue(ledger.delivery_is_done(key))
            self.adapter._linear.get_agent_session_delivery_context = original
            ledger.reschedule_outbox(item_id, "fixture recovered", 0)
            self.assertTrue(await self.adapter._drain_outbox_once())
            self.assertEqual(ledger.get_outbox_item(item_id)["state"], "delivered")
            self.assertEqual((await (await self.post(self.payload)).json())["status"], "duplicate")
            self.assertEqual(ingress.await_count, 1)
            self.assertEqual(len([c for c in self.adapter._linear.calls if c[1] == "thought"]), 1)


if __name__ == "__main__":
    unittest.main()
