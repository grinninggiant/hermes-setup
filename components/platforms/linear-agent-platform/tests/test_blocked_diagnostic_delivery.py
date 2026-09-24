"""Isolated producer/consumer regression; no live vendor events or judge calls."""
import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest import mock
from typing import Any
import tempfile
import time
import unittest
from copy import deepcopy
import test_native_continuation as base


class BlockedDiagnosticDeliveryTests(unittest.IsolatedAsyncioTestCase):
    adapter: Any
    drain_patch: Any
    admitted: list[Any]
    temp: tempfile.TemporaryDirectory
    asyncSetUp = base.NativeContinuationTests.asyncSetUp
    asyncTearDown = base.NativeContinuationTests.asyncTearDown

    async def test_fresh_blocked_judge_reports_verified_error(self):
        await self._assert_fresh_blocked_fence(completed=True)

    async def test_budget_exit_reports_verified_error_without_resuming(self):
        await self._assert_fresh_blocked_fence(completed=False)

    async def _assert_fresh_blocked_fence(self, *, completed):
        from gateway.run import GatewayRunner

        store = base.FakeSessionStore()
        runner = object.__new__(GatewayRunner)
        runner.session_store = store._store
        runner._async_session_store = store
        runner._goal_max_turns_from_config = lambda: 20
        runner._warm_goals_session_db = mock.AsyncMock(return_value=None)
        runner._run_in_executor_with_context = mock.AsyncMock(side_effect=lambda call: call())
        self.adapter.gateway_runner = runner
        manager = base.FakeGoalManager("hermes-session")
        manager.set("isolated diagnostic task", contract=None)
        async def read_state(*args, **kwargs):
            return manager.state
        runner.goal_state_for_source = read_state
        base.FakeGoalManager.decision = {
            "status": "paused", "should_continue": False,
            "continuation_prompt": None, "verdict": "blocked",
            "reason": "judged unachievable: PRIVATE_JUDGE_REASON_NOT_FOR_DELIVERY", "message": "",
        }
        explanation = "Teşhis kısmi: tarihsel ilk ret nedeni bilinmiyor; izole test sınırı doğrulandı."
        event: Any = base.turn_event()
        event._gateway_turn_result = MappingProxyType({
            **dict(event._gateway_turn_result), "completed": completed,
            "turn_exit_reason": "completed" if completed else "max_iterations_reached(90)",
        })
        await self.adapter.on_processing_start(event)
        await self.adapter.prepare_turn_delivery(event, explanation, event._gateway_turn_result)
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])
        with mock.patch("hermes_cli.goals.GoalManager", side_effect=lambda **kwargs: manager):
            await runner._post_turn_goal_continuation(
                session_entry=store.entry, source=event.source, final_response=explanation,
            )
        # Fake judge mirrors the native last-turn timestamp without remote LLM I/O.
        manager.state.last_turn_at = time.time()
        manager.state.last_verdict = "blocked"
        await self.adapter.on_processing_complete(event, base.ProcessingOutcome.SUCCESS)
        row = self.adapter._ledger.list_turn_decisions("linear-session")[-1]
        self.assertEqual((row["outcome"], row["dispatch_state"]), ("blocked", "fenced"))
        item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
        self.assertIsNotNone(item)
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertNotIn("PRIVATE_JUDGE_REASON_NOT_FOR_DELIVERY", item["payload"]["body"])
        self.assertNotIn(explanation, item["payload"]["body"])
        self.assertFalse(self.adapter._ledger.progress_is_allowed("linear-session"))
        self.assertEqual(base.FakeGoalManager.resume_calls, 0)
        self.assertEqual(self.admitted, [])
        self.adapter._validate_activity_target = mock.AsyncMock(return_value={"status": "active"})
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="fixture-activity")
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.assertEqual(self.adapter._linear.create_activity.await_count, 1)
        self.assertEqual(self.adapter._linear.create_activity.await_args_list[0].args[1], "error")
        await self.adapter.on_processing_complete(event, base.ProcessingOutcome.SUCCESS)
        self.assertFalse(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.assertEqual(self.adapter._linear.create_activity.await_count, 1)
        self.assertEqual(manager.state.status, "paused")

    async def test_diagnostic_explanation_never_overrides_negative_gates(self):
        cases = ("stop", "cancel", "done", "approval", "input", "delegate",
                 "owner", "blocker", "terminal", "failed", "unknown_exit", "stale", "rotation",
                 "wait_verdict", "human_pause")
        original_context = await self.adapter._linear.get_agent_turn_context("linear-session")
        for ordinal, case in enumerate(cases, 1):
            with self.subTest(case=case):
                event = base.turn_event()
                await self.adapter.on_processing_start(event)
                state = SimpleNamespace(status="paused", created_at=123.0,
                    turns_used=ordinal, max_turns=20, last_turn_at=time.time(),
                    last_verdict="blocked", paused_reason="judged unachievable: fixture")
                context = deepcopy(original_context)
                result = {**dict(event._gateway_turn_result), "completed": True,
                          "turn_exit_reason": "completed"}
                if case == "stop": event.metadata["linear_signal"] = "stop"
                if case == "cancel": result["interrupted"] = True
                if case == "done": context["issue"]["state"]["type"] = "completed"
                if case == "approval": result["turn_exit_reason"] = "approval"
                if case == "input": context["status"] = "awaitingInput"
                if case == "delegate": context["issue"]["delegate"]["id"] = "other"
                if case == "owner": context["app_user_id"] = "other"
                if case == "blocker": context["open_blockers"] = ["fixture"]
                if case == "terminal": context["status"] = "complete"
                if case == "failed": result["failed"] = True
                if case == "unknown_exit": result["completed"] = False; result["turn_exit_reason"] = "unknown_incomplete"
                if case == "stale": state.last_turn_at = 1.0
                if case == "rotation": self.adapter.gateway_runner.async_session_store.entry.session_id = "rotated"
                if case == "wait_verdict": state.last_verdict = "wait"
                if case == "human_pause": state.paused_reason = "paused by user"
                self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(return_value=state)
                self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
                await self.adapter._prepare_native_owned_turn_delivery(event, "DO_NOT_EXPOSE", result)
                row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
                item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
                if item is not None:
                    self.assertNotIn("DO_NOT_EXPOSE", item["payload"]["body"])
                    self.assertNotEqual(item["payload"]["activity_type"], "response")
                if case in {"stop", "cancel", "done", "stale", "rotation", "wait_verdict", "human_pause"}:
                    self.assertIsNone(item)
                self.assertEqual(self.admitted, [])
                self.assertEqual(base.FakeGoalManager.resume_calls, 0)

    async def test_verified_pause_error_survives_ledger_reopen(self):
        event = base.turn_event()
        await self.adapter.on_processing_start(event)
        state = SimpleNamespace(status="paused", created_at=123.0,
            turns_used=2, max_turns=20, last_turn_at=time.time(),
            last_verdict="blocked", paused_reason="judged unachievable: fixture")
        self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(return_value=state)
        result = {**dict(event._gateway_turn_result), "completed": True, "turn_exit_reason": "completed"}
        await self.adapter._prepare_native_owned_turn_delivery(event, "RECOVERABLE_EXPLANATION", result)
        path = str(base.Path(self.temp.name) / "ledger.sqlite3")
        self.adapter._ledger.close()
        self.adapter._ledger = base.DeliveryLedger(path, startup_recovery=False)
        self.adapter._linear.get_agent_session_delivery_context = mock.AsyncMock(return_value={"app_user_id": "app-user"})
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="fixture")
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.assertFalse(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.adapter._linear.create_activity.assert_awaited_once()
        self.assertEqual(self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)["dispatch_state"], "fenced")
        self.assertFalse(self.adapter._ledger.progress_is_allowed("linear-session"))
        self.assertEqual(self.admitted, [])

    async def _queue_verified_blocked_error(self):
        event = base.turn_event()
        await self.adapter.on_processing_start(event)
        state = SimpleNamespace(
            status="paused", created_at=123.0, turns_used=2, max_turns=20,
            last_turn_at=time.time(), last_verdict="blocked",
            paused_reason="judged unachievable: fixture",
        )
        self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(return_value=state)
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "Partial explanation", event._gateway_turn_result,
        )
        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{event._linear_turn_decision_id}"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "pending")
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="fixture")

    async def test_queued_blocked_error_is_suppressed_after_stop(self):
        await self._queue_verified_blocked_error()
        self.assertGreater(self.adapter._ledger.fence_turn_decisions(
            "linear-session", "linear_authoritative_stop"
        ), 0)
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_queued_blocked_error_is_suppressed_after_human_input_wait(self):
        await self._queue_verified_blocked_error()
        context = await self.adapter._linear.get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value={**context, "status": "awaitingInput"}
        )
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_queued_blocked_error_is_suppressed_after_rotation(self):
        await self._queue_verified_blocked_error()
        self.adapter.gateway_runner.async_session_store.entry.session_id = "rotated"
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_queued_blocked_error_is_suppressed_after_done(self):
        await self._queue_verified_blocked_error()
        context = await self.adapter._linear.get_agent_turn_context("linear-session")
        context["issue"]["state"]["type"] = "completed"
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
        self.assertTrue(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.adapter._linear.create_activity.assert_not_awaited()

    async def test_inline_status_dispatch_does_not_deadlock_on_queued_visibility(self):
        await self._queue_verified_blocked_error()
        self.adapter._active_turn_events.pop("linear-session", None)
        self.drain_patch.stop()
        self.adapter._prepare_bound_linear_ingress = mock.AsyncMock(return_value=False)
        self.adapter._message_handler = mock.AsyncMock(return_value="Status text")
        command = base.turn_event()
        command.text = "/status"
        session_key = self.adapter._event_session_key(command)
        self.adapter._active_sessions[session_key] = asyncio.Event()
        async with self.adapter._session_lock("linear-session"):
            self.adapter._schedule_thought(
                "linear-session", "issue-164", "status-deadlock-regression",
                include_queued=False,
            )
            await asyncio.wait_for(
                base.LinearPlatformAdapter.handle_message(self.adapter, command),
                timeout=2,
            )
            await asyncio.wait_for(self.adapter._wait_for_thought("linear-session"), timeout=2)
        self.assertGreaterEqual(self.adapter._linear.create_activity.await_count, 1)
