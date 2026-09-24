"""Signed HTTP admission deadlines must not fabricate acceptance or progress."""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "diagnostics"))
import created_deadline_probe as probe


class CreatedAdmissionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    make_payload = probe.CreatedDeadlineProbe.make_payload
    request_for = probe.CreatedDeadlineProbe.request_for
    asyncSetUp = probe.CreatedDeadlineProbe.asyncSetUp
    asyncTearDown = probe.CreatedDeadlineProbe.asyncTearDown
    slow_read = probe.CreatedDeadlineProbe.slow_read
    post = probe.CreatedDeadlineProbe.post

    async def test_timeout_releases_claim_without_dispatch_or_false_thought_then_retry_dedups(self):
        original = self.adapter._linear.get_agent_turn_context
        self.adapter._linear.get_agent_turn_context = self.slow_read
        post = self.post(self.payload)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        duplicate = await asyncio.wait_for(self.post(self.payload), timeout=1)
        self.assertEqual(duplicate.status, 503)
        self.assertEqual((await duplicate.json())["status"], "processing")
        response = await asyncio.wait_for(asyncio.shield(post), timeout=4.9)
        self.assertEqual(response.status, 503)
        self.assertFalse(self.handler_entered.is_set())
        self.assertEqual(self.adapter._linear.calls, [])
        self.assertFalse(self.adapter._ledger.outbox_counts()["pending"])
        self.adapter._linear.get_agent_turn_context = original
        accepted = await asyncio.wait_for(self.post(self.payload), timeout=1)
        self.assertEqual(accepted.status, 200)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        replay = await asyncio.wait_for(self.post(self.payload), timeout=1)
        self.assertEqual(replay.status, 200)
        self.assertEqual((await replay.json())["status"], "duplicate")

    async def test_sqlite_thought_failure_replays_delivery_without_replaying_execution(self):
        from unittest import mock

        ledger = self.adapter._ledger
        request = self.request_for(self.payload)
        key = probe.fixtures.adapter_mod._delivery_key(self.payload, request._body)
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            failed = await asyncio.wait_for(self.post(self.payload), timeout=5)
            self.assertEqual(failed.status, 503)
            await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
            self.assertTrue(ledger.delivery_is_done(key))
            self.assertIsNone(ledger.get_outbox_item(f"activity:thought:{key}"))
            still_failing = await asyncio.wait_for(self.post(self.payload), timeout=5)
            self.assertEqual(still_failing.status, 503)
            self.assertTrue(ledger.delivery_is_done(key))
            ledger._db.execute("DROP TRIGGER fail_thought")
            # Reopen SQLite: no in-memory retry receipt can make this pass.
            ledger.close()
            self.adapter._ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
            self.adapter._ledger.prune(now=int(time.time()) + ledger.retention_seconds + 1)
            self.assertTrue(self.adapter._ledger.delivery_is_done(key))
            self.assertTrue(self.adapter._ledger.pending_acceptance_thoughts(key))
            replay = await asyncio.wait_for(self.post(self.payload), timeout=5)
            self.assertEqual(replay.status, 200)
            self.assertEqual((await replay.json())["status"], "duplicate")
            await self.adapter._wait_for_thought("linear-session")
            self.assertEqual(ingress.await_count, 1)
            thoughts = [call for call in self.adapter._linear.calls if call[1] == "thought"]
            self.assertEqual(len(thoughts), 1)
            again = await asyncio.wait_for(self.post(self.payload), timeout=5)
            self.assertEqual(again.status, 200)
            await self.adapter._wait_for_thought("linear-session")
            self.assertEqual(ingress.await_count, 1)
            self.assertEqual(self.adapter._linear.calls, thoughts)

    async def test_outbox_poll_repairs_persisted_thought_without_webhook_reexecution(self):
        from unittest import mock

        ledger = self.adapter._ledger
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        self.assertEqual((await asyncio.wait_for(self.post(self.payload), timeout=5)).status, 503)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        ledger.close()
        self.adapter._ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
        original = self.adapter._linear.create_activity
        delivered = asyncio.Event()

        async def observe_activity(*args, **kwargs):
            result = await original(*args, **kwargs)
            delivered.set()
            return result

        self.adapter._linear.create_activity = observe_activity
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            self.adapter._running = True
            worker = asyncio.create_task(self.adapter._outbox_loop())
            try:
                await asyncio.wait_for(delivered.wait(), timeout=5)
                self.assertFalse(self.adapter._ledger.pending_acceptance_thoughts())
                ingress.assert_not_awaited()
            finally:
                self.adapter._running = False
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        await self.adapter._wait_for_thought("linear-session")
        self.assertEqual(len(self.adapter._linear.calls), 1)

    async def test_failed_receipt_does_not_starve_unrelated_outbox(self):
        ledger = self.adapter._ledger
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        self.assertEqual((await asyncio.wait_for(self.post(self.payload), timeout=5)).status, 503)
        delivered = asyncio.Event()
        original = self.adapter._linear.create_activity

        async def observe_activity(*args, **kwargs):
            result = await original(*args, **kwargs)
            delivered.set()
            return result

        self.adapter._linear.create_activity = observe_activity
        self.adapter._enqueue_activity("another-session", "thought", "Independent task", item_key="independent")
        self.adapter._running = True
        worker = asyncio.create_task(self.adapter._outbox_loop())
        try:
            await asyncio.wait_for(delivered.wait(), timeout=5)
            self.assertTrue(ledger.pending_acceptance_thoughts())
        finally:
            self.adapter._running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_native_terminal_success_cancels_pending_acceptance_thought(self):
        ledger = self.adapter._ledger
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        self.assertEqual((await asyncio.wait_for(self.post(self.payload), timeout=5)).status, 503)
        decision = ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "success",
        )
        self.adapter._enqueue_turn_success(decision, "Finished", {
            "completed": True, "failed": False, "interrupted": False,
            "turn_exit_reason": "complete", "session_id": "hermes-session",
        }, {"id": "issue-164", "description": "", "updatedAt": "revision"})
        self.assertFalse(ledger.pending_acceptance_thoughts())
        ledger._db.execute("DROP TRIGGER fail_thought")
        self.assertEqual((await self.post(self.payload)).status, 200)
        self.assertFalse([call for call in self.adapter._linear.calls if call[1] == "thought"])

    async def test_stop_after_thought_fault_cancels_repair_without_reexecution(self):
        from unittest import mock

        ledger = self.adapter._ledger
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        failed = await asyncio.wait_for(self.post(self.payload), timeout=5)
        self.assertEqual(failed.status, 503)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        self.assertTrue(ledger.pending_acceptance_thoughts())
        stopped = await asyncio.wait_for(self.post({
            **self.payload, "action": "prompted", "agentActivity": {
                "id": "stop-after-fault", "body": "stop", "signal": "stop",
            },
        }), timeout=5)
        self.assertEqual(stopped.status, 200)
        self.assertFalse(ledger.pending_acceptance_thoughts())
        ledger._db.execute("DROP TRIGGER fail_thought")
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            replay = await asyncio.wait_for(self.post(self.payload), timeout=5)
            self.assertEqual((await replay.json())["status"], "duplicate")
            await self.adapter._wait_for_thought("linear-session")
            ingress.assert_not_awaited()
        self.assertFalse([call for call in self.adapter._linear.calls if call[1] == "thought"])

    async def test_stop_receipt_write_failure_still_interrupts_core_and_retries_truthfully(self):
        await self._assert_stop_receipt_failure(contended=False)

    async def test_stop_receipt_write_failure_still_interrupts_before_session_lock(self):
        await self._assert_stop_receipt_failure(contended=True)

    async def _assert_stop_receipt_failure(self, *, contended):
        ledger = self.adapter._ledger
        self.assertEqual((await self.post(self.payload)).status, 200)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        await self.adapter._wait_for_thought("linear-session")
        core_task, = self.adapter._session_tasks.values()
        decision = ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue",
        )
        ledger.transition_turn_decision(decision["decision_id"], "pending", "enqueued")
        ledger._db.execute(
            "CREATE TRIGGER fail_cancel_receipt BEFORE UPDATE ON deliveries "
            "WHEN OLD.acceptance_thought_json IS NOT NULL "
            "AND NEW.acceptance_thought_json IS NULL "
            "BEGIN SELECT RAISE(FAIL, 'injected cancellation receipt failure'); END"
        )
        stop = {**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-receipt-fault", "body": "stop", "signal": "stop",
        }}
        stop_key = probe.fixtures.adapter_mod._delivery_key(stop, self.request_for(stop)._body)
        lock = self.adapter._session_lock("linear-session")
        if contended:
            await lock.acquire()
        try:
            response = await asyncio.wait_for(self.post(stop), timeout=2)
            self.assertEqual(response.status, 503)  # Never claim durable cancellation succeeded.
            self.assertGreater(self.adapter.gateway_runner.interrupt_session_processing.await_count, 0)
            self.assertTrue(core_task.done())
            self.assertTrue(core_task.cancelled())
            self.assertFalse(ledger.delivery_is_done(stop_key))
            self.assertEqual(ledger.get_turn_decision(decision["decision_id"])["dispatch_state"],
                             "enqueued" if contended else "fenced")
        finally:
            if contended:
                lock.release()
        ledger.close()
        self.adapter._ledger = ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
        self.assertEqual((await self.post(stop)).status, 503)
        self.assertEqual(ledger.get_turn_decision(decision["decision_id"])["dispatch_state"], "fenced")
        self.assertEqual(ledger.get_turn_decision(decision["decision_id"])["outcome"], "stopped")
        self.assertFalse(ledger.delivery_is_done(stop_key))
        ledger._db.execute("DROP TRIGGER fail_cancel_receipt")
        self.assertEqual((await self.post(stop)).status, 200)
        self.assertTrue(ledger.delivery_is_done(stop_key))
        self.assertFalse(ledger.pending_acceptance_thoughts())
        self.assertEqual((await self.post(stop)).status, 200)
        ledger.prune(now=int(time.time()) + ledger.retention_seconds + 1)
        self.assertFalse(ledger.delivery_is_done(stop_key))

    async def test_prune_retains_scheduled_receipt_until_outbox_obligations_finish(self):
        ledger = self.adapter._ledger
        key = probe.fixtures.adapter_mod._delivery_key(self.payload, self.request_for(self.payload)._body)
        thought_id = f"activity:thought:{key}"
        async with self.adapter._outbox_drain_lock:
            self.assertEqual((await self.post(self.payload)).status, 200)
            await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
            ledger.close()
            self.adapter._ledger = ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
            expiry = int(time.time()) + ledger.retention_seconds + 1
            for state in ("pending", "in_flight", "dead"):
                with self.subTest(outbox_state=state):
                    if state == "in_flight":
                        self.assertEqual(ledger.claim_due_outbox().id, thought_id)
                    elif state == "dead":
                        ledger.dead_letter_outbox(thought_id, "fixture delivery failure")
                    self.assertEqual(ledger.prune(now=expiry), 0)
                    self.assertTrue(ledger.delivery_is_done(key))
                    self.assertTrue(ledger.acceptance_thought_is_current(key))
                    self.assertEqual(ledger.get_outbox_item(thought_id)["state"], state)
            ledger.reschedule_outbox(thought_id, "fixture retry", 0)
            self.assertEqual(self.adapter._linear.calls, [])
        await asyncio.wait_for(self.adapter._wait_for_thought("linear-session"), timeout=2)
        self.assertEqual(len(self.adapter._linear.calls), 1)
        self.assertEqual(self.adapter._linear.calls[0][1], "thought")
        self.assertEqual(ledger.get_outbox_item(thought_id)["state"], "delivered")
        ledger.prune(now=int(time.time()) + ledger.retention_seconds + 1)
        self.assertFalse(ledger.delivery_is_done(key))
        self.assertIsNone(ledger.get_outbox_item(thought_id))

    async def test_prune_expires_retry_receipts_without_dropping_acceptance_obligations(self):
        ledger = self.adapter._ledger
        now = int(time.time())
        expired = now - ledger.retention_seconds - 1
        for key in ("retry-expired", "retry-fresh", "processing-expired", "done-owed"):
            self.assertTrue(ledger.claim(key, now=now if key == "retry-fresh" else expired))
        # The separate ingress candidate persists this state on release.
        ledger._db.execute("UPDATE deliveries SET state = 'retry' WHERE webhook_id LIKE 'retry-%'")
        ledger._db.commit()
        ledger.mark_done("done-owed", now=expired, acceptance_thought={
            "agent_session_id": "linear-session", "issue_id": "issue-164", "include_queued": True,
        })
        ledger.close()
        self.adapter._ledger = ledger = probe.fixtures.DeliveryLedger(str(ledger.path))
        # Reopen runs startup pruning; a second prune must remain idempotent.
        ledger.prune(now=now)
        remaining = {row[0] for row in ledger._db.execute("SELECT webhook_id FROM deliveries")}
        self.assertEqual(remaining, {"retry-fresh", "processing-expired", "done-owed"})
        self.assertTrue(ledger.pending_acceptance_thoughts("done-owed"))
        ledger.cancel_acceptance_thoughts("linear-session")
        self.assertEqual(ledger.prune(now=now), 1)
        self.assertFalse(ledger.delivery_is_done("done-owed"))

    async def test_stop_suppresses_thought_after_pending_owner_read(self):
        original = self.adapter._linear.get_agent_session_delivery_context
        owner_release = asyncio.Event()

        async def delayed_owner(session_id):
            self.entered.set()
            await asyncio.wait_for(owner_release.wait(), timeout=5)
            return await original(session_id)

        cancelled = asyncio.Event()

        async def handler(_event):
            self.handler_entered.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=15)
            finally:
                cancelled.set()

        self.adapter.set_message_handler(handler)
        self.adapter._linear.get_agent_session_delivery_context = delayed_owner
        response = await asyncio.wait_for(self.post(self.payload), timeout=5)
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        await asyncio.wait_for(self.handler_entered.wait(), timeout=1)
        stop = self.post({**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-before-owner", "body": "stop", "signal": "stop",
        }})
        try:
            # The model handler must unwind; core cleanup may still need the
            # held outbox lock before the whole processing task can finish.
            await asyncio.wait_for(cancelled.wait(), timeout=5)
        finally:
            owner_release.set()
        self.assertEqual((await asyncio.wait_for(stop, timeout=5)).status, 200)
        await self.adapter._wait_for_thought("linear-session")
        self.assertFalse([call for call in self.adapter._linear.calls if call[1] == "thought"])

    async def test_stop_http_completes_within_five_seconds_behind_policy_read(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._linear.get_issue_closure_context = self.slow_read
        created = self.post(self.payload)
        await asyncio.wait_for(self.entered.wait(), timeout=1)
        started = time.monotonic()
        stopped = await asyncio.wait_for(self.post({
            **self.payload, "action": "prompted", "agentActivity": {
                "id": "stop-policy", "body": "stop", "signal": "stop",
            },
        }), timeout=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(stopped.status, 200)
        self.assertEqual((await created).status, 503)
        self.assertFalse(self.handler_entered.is_set())
        self.assertFalse([call for call in self.adapter._linear.calls if call[1] == "thought"])

    async def test_policy_and_native_reads_share_one_absolute_budget(self):
        self.adapter._planned_activation_enabled = True

        async def policy_read(_issue_id):
            await asyncio.sleep(2)
            return {"state": {"type": "started"}}

        self.adapter._linear.get_issue_closure_context = policy_read
        self.adapter._linear.get_agent_turn_context = self.slow_read
        started = time.monotonic()
        response = await asyncio.wait_for(asyncio.shield(self.post(self.payload)), timeout=4.9)
        self.assertEqual(response.status, 503)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.handler_entered.is_set())
        self.assertEqual(self.adapter._linear.calls, [])


if __name__ == "__main__":
    unittest.main()
