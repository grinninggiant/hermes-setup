"""Delivery-seam regression: staging must stop the generic sender itself."""
from pathlib import Path

from gateway.platforms.base import BasePlatformAdapter
from gateway.run_goals import GatewayGoalsMixin
from gateway.platforms.base import ProcessingOutcome
from test_native_continuation import NativeContinuationTests, turn_event


class StagedDeliveryBoundaryTests(NativeContinuationTests):
    async def test_staged_response_never_reaches_generic_sender_before_judge(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        text = "unverified final candidate"

        prepared = await BasePlatformAdapter._response_after_delivery_decision(
            self.adapter, event, text
        )

        # Every core sender, including queued-initial response delivery, branches
        # on this return value. A second send-side gate is not sufficient.
        self.assertIsNone(prepared)
        self.assertEqual(
            GatewayGoalsMixin._final_text_for_post_turn_hooks({}, event), text
        )
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)

    async def test_completion_is_fenced_to_the_event_that_staged_delivery(self):
        first = turn_event()
        second = turn_event()
        second.message_id = "webhook-event-2"

        await self.adapter.on_processing_start(first)
        await self.adapter.prepare_turn_delivery(first, "first", first._gateway_turn_result)
        await self.adapter.on_processing_start(second)
        await self.adapter.prepare_turn_delivery(second, "second", second._gateway_turn_result)

        await self.adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        self.assertIs(self.adapter._pending_turn_deliveries["linear-session"][0], second)

        await self.adapter.on_processing_complete(second, ProcessingOutcome.SUCCESS)
        self.assertNotIn("linear-session", self.adapter._pending_turn_deliveries)

    async def test_retryable_second_authoritative_read_retains_staged_delivery(self):
        from test_native_continuation import FakeGoalManager, LinearAPIError

        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter._linear.description = "## Acceptance\n- [x] tests pass\n- [x] restart is safe"
        event = turn_event()
        event._gateway_turn_result = {
            **dict(event._gateway_turn_result),
            "completed": True,
            "turn_exit_reason": "completed",
        }
        await self.adapter.on_processing_start(event)
        await BasePlatformAdapter._response_after_delivery_decision(
            self.adapter, event, "durable candidate"
        )

        original = self.adapter._linear.get_agent_turn_context
        calls = 0

        async def fail_second(session_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LinearAPIError("temporary read failure", retryable=True)
            return await original(session_id)

        self.adapter._linear.get_agent_turn_context = fail_second
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)

        self.adapter._linear.get_agent_turn_context = original
        recovery = self.adapter._turn_recovery_task
        self.assertIsNotNone(recovery)
        await recovery
        self.assertNotIn("linear-session", self.adapter._pending_turn_deliveries)
        self.assertEqual(len(self.adapter._ledger.list_turn_decisions("linear-session")), 1)
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 1)

    async def test_stop_between_staged_retries_fences_without_success_or_loop(self):
        from test_native_continuation import FakeGoalManager, LinearAPIError

        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter._linear.description = "## Acceptance\n- [x] tests pass"
        event = turn_event()
        event._gateway_turn_result = {
            **dict(event._gateway_turn_result),
            "completed": True,
            "turn_exit_reason": "completed",
        }
        await self.adapter.on_processing_start(event)
        await BasePlatformAdapter._response_after_delivery_decision(
            self.adapter, event, "must not be delivered"
        )

        original = self.adapter._linear.get_agent_turn_context
        calls = 0

        async def fail_recovery(session_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LinearAPIError("temporary read failure", retryable=True)
            return await original(session_id)

        self.adapter._linear.get_agent_turn_context = fail_recovery
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        decision_id = event._linear_turn_decision_id
        self.adapter._ledger.fence_turn_decisions("linear-session", "operator stop")
        recovery = self.adapter._turn_recovery_task
        self.assertIsNotNone(recovery)
        await recovery

        self.assertNotIn("linear-session", self.adapter._pending_turn_deliveries)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(decision_id)["outcome"], "stopped"
        )
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)
        self.assertTrue(self.adapter._turn_recovery_task.done())

    async def _stage_review_success(self, message_id="review-A"):
        from test_native_continuation import FakeGoalManager
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter._linear.description = "## Acceptance\n- [x] tests pass"
        event = turn_event()
        event.message_id = message_id
        event._gateway_turn_result = {
            **dict(event._gateway_turn_result),
            "completed": True, "turn_exit_reason": "completed",
        }
        await self.adapter.on_processing_start(event)
        await self.adapter.prepare_turn_delivery(event, message_id, event._gateway_turn_result)
        return event

    async def test_review_recovery_does_not_judge_new_staged_turn(self):
        from test_native_continuation import LinearAPIError
        self.adapter._running = False
        first = await self._stage_review_success()
        original = self.adapter._linear.get_agent_turn_context

        async def fail(_session):
            raise LinearAPIError("offline", retryable=True)

        self.adapter._linear.get_agent_turn_context = fail
        await self.adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        second = await self._stage_review_success("review-B")
        self.adapter._linear.get_agent_turn_context = original
        await self.adapter._recover_turn_decisions()
        self.assertIs(self.adapter._pending_turn_deliveries["linear-session"][0], second)
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])

    async def test_review_completion_preserves_replacement_across_await(self):
        await self._review_replacement_across_await(recovery=False, fail=False)

    async def test_review_recovery_preserves_replacement_across_await(self):
        await self._review_replacement_across_await(recovery=True, fail=False)

    async def test_review_failed_recovery_does_not_make_replacement_eligible(self):
        await self._review_replacement_across_await(recovery=True, fail=True)

    async def _review_replacement_across_await(self, *, recovery, fail):
        import asyncio
        from test_native_continuation import LinearAPIError
        self.adapter._running = False
        first = await self._stage_review_success()
        original = self.adapter._linear.get_agent_turn_context

        async def offline(_session):
            raise LinearAPIError("offline", retryable=True)

        if recovery:
            self.adapter._linear.get_agent_turn_context = offline
            await self.adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(session):
            entered.set()
            await release.wait()
            if fail:
                raise LinearAPIError("offline", retryable=True)
            return await original(session)

        self.adapter._linear.get_agent_turn_context = paused
        task = asyncio.create_task(
            self.adapter._recover_turn_decisions() if recovery else
            self.adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        )
        await entered.wait()
        try:
            # Reuse the external event ID too: retry counters must own the tuple.
            second = await self._stage_review_success(first.message_id)
            replacement = self.adapter._pending_turn_deliveries["linear-session"]
        finally:
            release.set()
            await task
        self.assertIs(self.adapter._pending_turn_deliveries.get("linear-session"), replacement)
        self.adapter._linear.get_agent_turn_context = original
        await self.adapter._recover_turn_decisions()
        self.assertIs(self.adapter._pending_turn_deliveries.get("linear-session"), replacement)
        self.assertEqual(self.adapter._staged_delivery_attempts, {})

    async def test_review_cancelled_result_survives_api_failure(self):
        from test_native_continuation import LinearAPIError
        for outcome in (ProcessingOutcome.CANCELLED, ProcessingOutcome.FAILURE):
            with self.subTest(outcome=outcome):
                self.adapter._running = False
                event = await self._stage_review_success(str(outcome))
                original = self.adapter._linear.get_agent_turn_context

                async def offline(_session):
                    raise LinearAPIError("offline", retryable=True)

                self.adapter._linear.get_agent_turn_context = offline
                await self.adapter.on_processing_complete(event, outcome)
                retained = self.adapter._pending_turn_deliveries["linear-session"][2]
                self.assertFalse(retained["completed"])
                self.assertEqual(retained["interrupted"], outcome == ProcessingOutcome.CANCELLED)
                self.adapter._linear.get_agent_turn_context = original
                await self.adapter._recover_turn_decisions()
                for row in self.adapter._ledger.list_turn_decisions("linear-session"):
                    self.assertNotEqual(row["outcome"], "success")
                    item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
                    self.assertEqual(item["payload"]["activity_type"], "error")

    async def test_review_exhausted_success_has_blocked_metadata(self):
        from test_native_continuation import LinearAPIError, adapter_mod
        self.adapter._running = False
        event = await self._stage_review_success()
        original = self.adapter._linear.get_agent_turn_context
        calls = 0

        async def fail_after_reservation(session):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise LinearAPIError("offline", retryable=True)
            return await original(session)

        self.adapter._linear.get_agent_turn_context = fail_after_reservation
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        for _ in range(adapter_mod._STAGED_DELIVERY_MAX_ATTEMPTS):
            await self.adapter._recover_turn_decisions()
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "fenced")
        self.assertEqual(row["outcome"], "blocked")
        item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertNotIn("linear-session", self.adapter._pending_turn_deliveries)
        self.assertEqual(self.adapter._staged_delivery_attempts, {})

class SilentOrphanStartupTests(NativeContinuationTests):
    def _restart(self):
        from test_native_continuation import DeliveryLedger
        self.adapter._ledger.close()
        self.adapter._ledger = DeliveryLedger(str(Path(self.temp.name) / 'ledger.sqlite3'))
        self.adapter._pending_turn_deliveries.clear()
        self.adapter._staged_delivery_attempts.clear()
        self.adapter._running = False

    def _reserve_success(self, ordinal=1):
        return self.adapter._ledger.reserve_turn_decision(
            'linear-session', 'issue-164', 'hermes-session', 123000000, ordinal,
            'success', source=self.adapter._source_snapshot(turn_event().source),
        )

    async def test_startup_orphan_success_is_one_honest_error_across_two_startups(self):
        row = self._reserve_success()
        self._restart()
        await self.adapter._recover_turn_decisions()
        current = self.adapter._ledger.get_turn_decision(row['decision_id'])
        self.assertEqual((current['outcome'], current['dispatch_state']), ('blocked', 'fenced'))
        item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
        self.assertEqual(item['payload']['activity_type'], 'error')
        self.assertIn('restart', item['payload']['body'])
        self._restart()
        await self.adapter._recover_turn_decisions()
        await self.adapter._recover_turn_decisions()
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 1)
        self.assertEqual(self.admitted, [])

        from unittest import mock
        from test_native_continuation import LinearPlatformAdapter
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        self.adapter._linear.create_activity = mock.AsyncMock()
        await LinearPlatformAdapter._drain_outbox_once(self.adapter)
        await LinearPlatformAdapter._drain_outbox_once(self.adapter)
        self.adapter._linear.create_activity.assert_awaited_once()
        self.assertEqual(self.adapter._linear.create_activity.await_args.args[1], 'error')

    async def test_startup_orphan_terminal_or_unowned_vendor_is_silent(self):
        from unittest import mock
        original = self.adapter._linear.get_agent_turn_context
        for ordinal, change in enumerate((
            {'status': 'complete'}, {'status': 'stale'}, {'status': 'stopped'},
            {'status': 'error'}, {'app_user_id': 'other'}, {'status': 'unknown'},
        ), 1):
            row = self._reserve_success(ordinal)
            self._restart()
            context = await original('linear-session')
            context.update(change)
            self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
            await self.adapter._recover_turn_decisions()
            current = self.adapter._ledger.get_turn_decision(row['decision_id'])
            self.assertEqual((current['outcome'], current['dispatch_state']), ('blocked', 'fenced'))
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 0)
        self.assertEqual(self.admitted, [])

    async def test_startup_orphan_unknown_api_is_silent_fail_closed(self):
        from unittest import mock
        row = self._reserve_success()
        self._restart()
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(side_effect=RuntimeError('unknown API failure'))
        await self.adapter._recover_turn_decisions()
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['outcome'], 'blocked')
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 0)

    async def test_startup_orphan_retry_budget_survives_restart(self):
        from unittest import mock
        from test_native_continuation import LinearAPIError, adapter_mod
        row = self._reserve_success()
        read = mock.AsyncMock(side_effect=LinearAPIError('offline', retryable=True))
        self.adapter._linear.get_agent_turn_context = read
        for attempt in range(adapter_mod._TURN_ADMISSION_MAX_ATTEMPTS):
            self._restart()
            await self.adapter._recover_turn_decisions()
            current = self.adapter._ledger.get_turn_decision(row['decision_id'])
            if attempt + 1 < adapter_mod._TURN_ADMISSION_MAX_ATTEMPTS:
                self.assertEqual((current['outcome'], current['dispatch_state']), ('success', 'pending'))
        self._restart()
        await self.adapter._recover_turn_decisions()
        self.assertEqual(read.await_count, adapter_mod._TURN_ADMISSION_MAX_ATTEMPTS)
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['outcome'], 'blocked')
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 0)
        self.assertEqual(self.admitted, [])

    async def test_startup_orphan_retry_worker_rereads_live_gate(self):
        from unittest import mock
        from test_native_continuation import LinearAPIError
        row = self._reserve_success()
        self._restart()
        context = await self.adapter._linear.get_agent_turn_context('linear-session')
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(side_effect=[
            LinearAPIError('offline', retryable=True), context,
        ])
        self.adapter._running = True
        await self.adapter._recover_turn_decisions()
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['dispatch_state'], 'pending')
        await self.adapter._turn_recovery_task
        self.assertEqual(self.adapter._linear.get_agent_turn_context.await_count, 2)
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['outcome'], 'blocked')
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 1)

    async def test_startup_durable_success_is_untouched_even_if_pending(self):
        from unittest import mock
        for ordinal, state in enumerate(('pending', 'completed'), 1):
            row = self._reserve_success(ordinal)
            key = f"activity:turn-success:{row['decision_id']}"
            self.adapter._ledger.enqueue_outbox(key, 'linear-session', 'activity.create', {
                'agent_session_id': 'linear-session', 'activity_type': 'response',
                'linear_turn_decision_id': row['decision_id'], 'body': 'already durable',
            })
            if state == 'completed':
                self.adapter._ledger.transition_turn_decision(row['decision_id'], 'pending', 'completed')
            before = self.adapter._ledger.get_outbox_item(key)
            self._restart()
            self.adapter._linear.get_agent_turn_context = mock.AsyncMock(side_effect=AssertionError('must not revalidate here'))
            await self.adapter._recover_turn_decisions()
            await self.adapter._recover_turn_decisions()
            current = self.adapter._ledger.get_turn_decision(row['decision_id'])
            self.assertEqual((current['outcome'], current['dispatch_state']), ('success', state))
            self.assertEqual(self.adapter._ledger.get_outbox_item(key), before)
            self.adapter._linear.get_agent_turn_context.assert_not_awaited()
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 2)

    async def test_startup_orphan_error_rechecks_vendor_before_outbox_delivery(self):
        from unittest import mock
        from test_native_continuation import LinearPlatformAdapter
        self._reserve_success()
        self._restart()
        await self.adapter._recover_turn_decisions()
        self.adapter._linear.status = 'complete'
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        self.adapter._linear.create_activity = mock.AsyncMock()
        await LinearPlatformAdapter._drain_outbox_once(self.adapter)
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_startup_orphan_stop_between_retries_is_silent(self):
        from unittest import mock
        from test_native_continuation import LinearAPIError
        row = self._reserve_success()
        self._restart()
        context = await self.adapter._linear.get_agent_turn_context('linear-session')
        context['status'] = 'complete'
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(side_effect=[
            LinearAPIError('offline', retryable=True), context,
        ])
        await self.adapter._recover_turn_decisions()
        self._restart()
        await self.adapter._recover_turn_decisions()
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['outcome'], 'blocked')
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 0)

    async def test_startup_orphan_unrelated_success_does_not_hide_orphan(self):
        other = self._reserve_success(2)
        self.adapter._ledger.complete_turn_success(
            other['decision_id'], f"activity:turn-success:{other['decision_id']}",
            'linear-session', {'activity_type': 'response', 'body': 'other turn'},
        )
        row = self._reserve_success()
        self._restart()
        await self.adapter._recover_turn_decisions()
        self.assertEqual(self.adapter._ledger.get_turn_decision(row['decision_id'])['outcome'], 'blocked')
        self.assertEqual(self.adapter._ledger.get_turn_decision(other['decision_id'])['outcome'], 'success')
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 2)

    async def test_startup_success_outbox_arriving_during_read_is_not_fenced(self):
        for ordinal, status in enumerate(('active', 'complete'), 1):
            row = self._reserve_success(ordinal)
            self._restart()
            original = self.adapter._linear.get_agent_turn_context
            # Keep the real context producer separate from the injected read.
            from test_native_continuation import FakeLinear
            context = await FakeLinear(status=status).get_agent_turn_context('linear-session')
            key = f"activity:turn-success:{row['decision_id']}"

            async def arriving(_session):
                self.adapter._ledger.enqueue_outbox(key, 'linear-session', 'activity.create', {
                    'activity_type': 'response', 'linear_turn_decision_id': row['decision_id'],
                    'body': 'durable concurrent response',
                })
                return context

            self.adapter._linear.get_agent_turn_context = arriving
            await self.adapter._recover_turn_decisions()
            self.adapter._linear.get_agent_turn_context = original
            current = self.adapter._ledger.get_turn_decision(row['decision_id'])
            self.assertEqual((current['outcome'], current['dispatch_state']), ('success', 'pending'))
            self.assertIsNone(self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}"))
        self.assertEqual(sum(self.adapter._ledger.outbox_counts().values()), 2)
