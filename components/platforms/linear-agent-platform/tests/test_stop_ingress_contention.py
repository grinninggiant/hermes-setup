"""Signed loopback ingress must interrupt running core work before slow locks."""
from __future__ import annotations

import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_native_platform as fixtures
import test_native_continuation as native


class StopIngressContentionTests(unittest.IsolatedAsyncioTestCase):
    make_payload = fixtures.AdapterWebhookTests.make_payload
    request_for = fixtures.AdapterWebhookTests.request_for

    async def asyncSetUp(self):
        await fixtures.AdapterWebhookTests.asyncSetUp(self)
        del self.adapter.handle_message
        self.adapter._native_goal_continuation_enabled = True
        self.adapter.gateway_runner = native.fake_gateway_runner()
        turn_client = native.FakeLinear()
        turn_client.actor_id = self.adapter._linear.actor_id
        self.adapter._linear.get_agent_turn_context = turn_client.get_agent_turn_context
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.payload = self.make_payload(agentSession={
            "id": "linear-session", "issue": {"id": "issue-164"}
        })
        app = web.Application()
        app.router.add_post("/linear/webhook", self.adapter._handle_webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for key in list(self.adapter._session_tasks):
            await self.adapter.cancel_session_processing(key)
        await fixtures.AdapterWebhookTests.asyncTearDown(self)

    async def test_stop_interrupts_core_before_locks_but_keeps_durable_fence(self):
        locks = (
            self.adapter._issue_lock("issue-164"),
            self.adapter._session_lock("linear-session"),
            self.adapter._outbox_drain_lock,
        )
        for ordinal, lock in enumerate(locks):
            with self.subTest(lock=ordinal):
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def handler(_event):
                    started.set()
                    try:
                        await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
                    finally:
                        cancelled.set()

                self.adapter.set_message_handler(handler)
                event = self.adapter._message_event(self.payload, f"work-{ordinal}", "fixture")
                await self.adapter.handle_message(event)
                self.assertIs(getattr(event, "_gateway_accepted", None), True)
                await asyncio.wait_for(started.wait(), timeout=1)
                row = self.adapter._ledger.reserve_turn_decision(
                    "linear-session", "issue-164", "hermes-session", 1, ordinal, "continue"
                )
                self.adapter._ledger.transition_turn_decision(
                    row["decision_id"], "pending", "enqueued"
                )
                request = self.request_for({**self.payload, "action": "prompted", "agentActivity": {
                    "id": f"stop-{ordinal}", "body": "stop", "signal": "stop"
                }})
                await lock.acquire()
                post = asyncio.create_task(self.client.post(
                    "/linear/webhook", data=request._body, headers=request.headers
                ))
                try:
                    # A blocked lock must not keep actual core execution running.
                    await asyncio.wait_for(cancelled.wait(), timeout=1)
                    self.assertFalse(post.done())  # No early success ACK before the fence.
                    # Admission already in flight can finish while Stop waits;
                    # the original locked cancellation must still catch it.
                    if lock is not self.adapter._outbox_drain_lock:
                        started.clear()
                        cancelled.clear()
                        late = self.adapter._message_event(self.payload, f"late-{ordinal}", "fixture")
                        await self.adapter.handle_message(late)
                        await asyncio.wait_for(started.wait(), timeout=1)
                finally:
                    lock.release()
                    response = await asyncio.wait_for(post, timeout=2)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(response.status, 200)
                self.assertEqual(self.adapter._ledger.get_turn_decision(
                    row["decision_id"]
                )["dispatch_state"], "fenced")
                self.assertEqual(self.adapter._ledger.get_turn_decision(
                    row["decision_id"]
                )["outcome"], "stopped")


if __name__ == "__main__":
    unittest.main()
