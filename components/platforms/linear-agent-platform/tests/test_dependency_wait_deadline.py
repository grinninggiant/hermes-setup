"""Durable dependency waits ACK before recovery I/O; recovery retains its fences."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import test_created_admission_deadline as deadline
from test_dependency_resume_admission import DependencyLinear

probe = deadline.probe


class DependencyWaitDeadlineTests(unittest.IsolatedAsyncioTestCase):
    make_payload = probe.CreatedDeadlineProbe.make_payload
    request_for = probe.CreatedDeadlineProbe.request_for
    post = probe.CreatedDeadlineProbe.post

    async def asyncSetUp(self):
        await probe.CreatedDeadlineProbe.asyncSetUp(self)
        original = self.adapter._linear
        self.adapter._linear = DependencyLinear()
        self.adapter._linear.organization_id = original.organization_id
        self.adapter._linear.actor_id = original.actor_id
        self.adapter._linear.update_issue_state = original.update_issue_state
        self.adapter._status_writeback_enabled = True
        self.first_read = asyncio.Event()
        self.blocker_release = asyncio.Event()
        self.blocker_reads = 0
        self.blockers = [{"id": "blocker-7", "identifier": "OPS-7"}]

        async def blockers(_issue_id):
            self.blocker_reads += 1
            if self.blocker_reads == 1:
                self.first_read.set()
                return self.blockers
            self.entered.set()
            await asyncio.wait_for(self.blocker_release.wait(), timeout=12)
            return []

        async def handler(event):
            self.events.append(event)
            self.handler_entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=15)

        self.adapter._linear.get_open_blockers = blockers
        self.adapter.set_message_handler(handler)
        self.goal_patch = mock.patch.object(probe.native.FakeGoalManager, "existing", False)
        self.goal_patch.start()
        self.key = probe.fixtures.adapter_mod._delivery_key(
            self.payload, self.request_for(self.payload)._body,
        )

    async def asyncTearDown(self):
        self.blocker_release.set()
        self.adapter._running = False
        if self.adapter._dependency_task is not None:
            self.adapter._dependency_task.cancel()
            await asyncio.gather(self.adapter._dependency_task, return_exceptions=True)
            self.adapter._dependency_task = None
        try:
            await probe.CreatedDeadlineProbe.asyncTearDown(self)
        finally:
            self.goal_patch.stop()

    async def park_and_reopen(self):
        started = time.monotonic()
        post = self.post(self.payload)
        await asyncio.wait_for(self.first_read.wait(), timeout=1)
        # On the broken path, only this concurrent retry gets a prompt ACK.
        duplicate = await asyncio.wait_for(self.post(self.payload), timeout=1)
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(await duplicate.json(), {"status": "duplicate"})
        ledger = self.adapter._ledger
        self.assertTrue(ledger.delivery_is_done(self.key))
        done, _ = await asyncio.wait({post}, timeout=max(0, 5 - (time.monotonic() - started)))
        elapsed = time.monotonic() - started
        self.assertTrue(done, f"No created HTTP response at {elapsed:.3f}s after durable wait")
        self.assertLess(elapsed, 5)
        self.assertEqual((await post).status, 200)
        self.assertEqual(await post.result().json(), {"status": "awaiting_input"})
        self.assertEqual(self.blocker_reads, 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._linear.created, [])
        # Real reopen proves the ACK covers wait, elicitation, status and dedup,
        # not a process-local promise or an already-dispatched core event.
        ledger.close()
        self.adapter._ledger = ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
        self.assertTrue(ledger.delivery_is_done(self.key))
        self.assertEqual(ledger.get_wait("linear-session")["state"], "waiting")
        self.assertEqual(ledger.get_wait("linear-session")["blockers"], self.blockers)
        for item_id in (f"activity:waiting:{self.key}", f"status:{self.key}:blocked"):
            self.assertEqual(ledger.get_outbox_item(item_id)["state"], "pending")

    def start_recovery(self):
        self.adapter._running = True
        self.adapter._dependency_task = asyncio.create_task(self.adapter._dependency_loop())

    async def test_created_wait_acks_within_five_seconds_then_poll_recovers_once(self):
        await self.park_and_reopen()
        # The blocker completion webhook may have arrived before put_wait. No
        # further inbound delivery is required: the existing loop recovers it.
        await self.adapter._post_thought("linear-session")
        self.start_recovery()
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        self.assertEqual(self.events, [])
        self.blocker_release.set()
        await asyncio.wait_for(self.handler_entered.wait(), timeout=2)
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0]._gateway_accepted)
        self.assertEqual(self.adapter._ledger.get_wait("linear-session")["state"], "resumed")
        self.assertFalse(await self.adapter._reconcile_wait("linear-session"))
        replay = await asyncio.wait_for(self.post(self.payload), timeout=1)
        self.assertEqual(await replay.json(), {"status": "duplicate"})
        competing = {**self.payload, "agentSession": {
            "id": "competing-session", "issue": {"id": "issue-164"},
        }}
        response = await asyncio.wait_for(self.post(competing), timeout=1)
        self.assertEqual(await response.json(), {"status": "issue_session_active"})
        self.assertEqual(len(self.events), 1)

    async def test_stop_or_closure_during_recovery_read_prevents_dispatch_and_replay(self):
        for fence in ("stop", "closure"):
            with self.subTest(fence=fence):
                await self.park_and_reopen()
                self.start_recovery()
                await asyncio.wait_for(self.entered.wait(), timeout=1)
                ledger = self.adapter._ledger
                if fence == "stop":
                    response = await asyncio.wait_for(self.post({
                        **self.payload, "action": "prompted", "agentActivity": {
                            "id": "stop-wait", "body": "stop", "signal": "stop",
                        },
                    }), timeout=2)
                    self.assertEqual(response.status, 200)
                else:
                    ledger.enqueue_closure_activity(
                        "wait-closure", "issue-164", "linear-session",
                        "closure-activity", "Human completion received.", {},
                    )
                self.assertEqual(ledger.get_wait("linear-session")["state"], "canceled")
                # Let this exact recovery iteration finish, without sleeping
                # through the normal 60-second polling interval.
                self.adapter._running = False
                self.adapter._dependency_poll_seconds = 0
                self.blocker_release.set()
                await asyncio.wait_for(self.adapter._dependency_task, timeout=2)
                self.assertEqual(self.events, [])
                self.assertFalse(self.adapter._session_tasks)
                self.assertFalse(await self.adapter._reconcile_wait("linear-session"))
                replay = await asyncio.wait_for(self.post(self.payload), timeout=1)
                self.assertEqual(await replay.json(), {"status": "duplicate"})
                self.assertEqual(ledger.get_wait("linear-session")["state"], "canceled")
                self.assertEqual(self.events, [])
            if fence == "stop":
                await self.asyncTearDown()
                await self.asyncSetUp()


if __name__ == "__main__":
    unittest.main()
