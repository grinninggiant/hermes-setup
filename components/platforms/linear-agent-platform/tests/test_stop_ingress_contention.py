"""Signed loopback ingress must interrupt running core work before slow locks."""
from __future__ import annotations

import asyncio
import json
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



    async def test_refused_stop_retry_does_not_cancel_successor(self):
        from unittest import mock

        started, finish, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(event):
            started.set()
            if event.message_id == "old":
                await finish.wait()
            else:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        self.adapter.set_message_handler(handler)
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "old", "fixture"))
        await asyncio.wait_for(started.wait(), 2)
        old_task, = self.adapter._session_tasks.values()
        stop = {**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-refused", "body": "stop", "signal": "stop",
        }}
        with mock.patch.object(self.adapter._ledger, "claim", side_effect=OSError("ledger unavailable")):
            response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 503)
        finish.set()
        await asyncio.wait_for(old_task, 2)
        started.clear()
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "successor", "fixture"))
        await asyncio.wait_for(started.wait(), 2)
        response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 200)
        self.assertFalse(cancelled.is_set(), "503 retry rebound Stop to a successor")

    async def test_refused_stop_retry_can_cancel_same_owner_after_event_initializes(self):
        from unittest import mock

        entering, release, started, cancelled = (asyncio.Event() for _ in range(4))
        on_start = self.adapter.on_processing_start

        async def delayed_start(event):
            entering.set()
            await release.wait()
            return await on_start(event)

        async def handler(_event):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.adapter.on_processing_start = delayed_start
        self.adapter.set_message_handler(handler)
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "old", "fixture"))
        await asyncio.wait_for(entering.wait(), 2)
        old_task, = self.adapter._session_tasks.values()
        stop = {**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-before-event-bind", "body": "stop", "signal": "stop",
        }}
        with mock.patch.object(self.adapter._ledger, "claim", side_effect=OSError("ledger unavailable")):
            self.assertEqual((await self.adapter._handle_webhook(self.request_for(stop))).status, 503)
        release.set()
        await asyncio.wait_for(started.wait(), 2)
        self.assertIs(self.adapter._session_tasks[next(iter(self.adapter._session_tasks))], old_task)
        response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 200)
        self.assertNotEqual(json.loads(response.text)["status"], "stale_stop")
        await asyncio.wait_for(cancelled.wait(), 2)



class StopRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise native worker gates and targeted interruption in the paired core."""

if __name__ == "__main__":
    unittest.main()
