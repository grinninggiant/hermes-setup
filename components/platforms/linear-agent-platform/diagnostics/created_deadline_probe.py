"""Offline contract probes, deliberately outside default test discovery.

Run explicitly with the Hermes core on PYTHONPATH; see README. No vendor requests.
Failures are unresolved clock/Stop boundaries, not acceptance evidence.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_native_platform as fixtures
import test_native_continuation as native


class CreatedDeadlineProbe(unittest.IsolatedAsyncioTestCase):
    make_payload = fixtures.AdapterWebhookTests.make_payload
    request_for = fixtures.AdapterWebhookTests.request_for

    async def asyncSetUp(self):
        await fixtures.AdapterWebhookTests.asyncSetUp(self)
        del self.adapter.handle_message  # Keep both adapter and core ingress real.
        self.adapter._native_goal_continuation_enabled = True
        self.adapter.gateway_runner = native.fake_gateway_runner()
        turn_client = native.FakeLinear()
        turn_client.actor_id = self.adapter._linear.actor_id
        self.adapter._linear.get_agent_turn_context = turn_client.get_agent_turn_context
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.handler_entered = asyncio.Event()

        async def handler(_event):
            self.handler_entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=15)
            return None

        self.adapter.set_message_handler(handler)
        self.payload = self.make_payload(agentSession={
            "id": "linear-session", "issue": {"id": "issue-164"}
        })
        app = web.Application()
        app.router.add_post("/linear/webhook", self.adapter._handle_webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.posts = []

    async def asyncTearDown(self):
        self.release.set()
        if self.posts:
            await asyncio.wait_for(asyncio.gather(*self.posts, return_exceptions=True), timeout=2)
        await self.client.close()
        tasks = list(self.adapter._session_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        await asyncio.wait_for(fixtures.AdapterWebhookTests.asyncTearDown(self), timeout=2)

    async def slow_read(self, *_args):
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=12)
        raise fixtures.LinearAPIError("fixture read unavailable", retryable=True)

    def post(self, payload):
        request = self.request_for(payload)
        task = asyncio.create_task(self.client.post(
            "/linear/webhook", data=request._body, headers=request.headers
        ))
        self.posts.append(task)
        return task

    async def assert_http_deadline(self, read_method):
        setattr(self.adapter._linear, read_method, self.slow_read)
        started = time.monotonic()
        post = self.post(self.payload)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        done, _ = await asyncio.wait({post}, timeout=max(0, 5 - (time.monotonic() - started)))
        elapsed = time.monotonic() - started
        self.assertFalse(self.handler_entered.is_set())
        self.assertTrue(done, f"No HTTP response at {elapsed:.3f}s; admission read still pending")
        self.assertEqual((await post).status, 503)  # No durable admission: no early success ACK.

    async def test_policy_read_http_deadline(self):
        self.adapter._planned_activation_enabled = True
        await self.assert_http_deadline("get_issue_closure_context")

    async def test_native_ingress_http_deadline(self):
        await self.assert_http_deadline("get_agent_turn_context")

    async def test_outbound_read_first_activity_deadline(self):
        self.adapter._linear.get_agent_session_delivery_context = self.slow_read
        started = time.monotonic()
        response = await asyncio.wait_for(self.post(self.payload), timeout=5)
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        await asyncio.sleep(max(0, 10 - (time.monotonic() - started)))
        self.assertTrue(self.adapter._linear.calls,
                        "HTTP ACK/execution admitted, but no activity attempt within 10 seconds")

    async def test_stop_does_not_wait_behind_slow_policy_read(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._linear.get_issue_closure_context = self.slow_read
        self.post(self.payload)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        stop = self.post({**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-1", "body": "stop", "signal": "stop"
        }})
        await asyncio.wait({stop}, timeout=0.25)
        self.assertTrue(self.adapter.gateway_runner.interrupt_session_processing.await_count,
                        "Stop cancellation is waiting behind the created policy-read lock")


if __name__ == "__main__":
    unittest.main()
