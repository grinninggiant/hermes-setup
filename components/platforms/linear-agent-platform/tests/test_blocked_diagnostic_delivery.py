"""Isolated producer/consumer regression; no live vendor events or judge calls."""
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
    admitted: list[Any]
    temp: tempfile.TemporaryDirectory
    asyncSetUp = base.NativeContinuationTests.asyncSetUp
    asyncTearDown = base.NativeContinuationTests.asyncTearDown

    async def test_fresh_blocked_judge_preserves_explanation_in_one_error(self):
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
            "reason": "PRIVATE_JUDGE_REASON_NOT_FOR_DELIVERY", "message": "",
        }
        explanation = "Teşhis kısmi: tarihsel ilk ret nedeni bilinmiyor; izole test sınırı doğrulandı."
        event = base.turn_event()
        event._gateway_turn_result = MappingProxyType({
            **dict(event._gateway_turn_result), "completed": True,
            "turn_exit_reason": "completed",
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
        self.assertEqual((row["outcome"], row["dispatch_state"]), ("blocked", "completed"))
        item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertIn(explanation, item["payload"]["body"])
        self.assertNotIn("PRIVATE_JUDGE_REASON_NOT_FOR_DELIVERY", item["payload"]["body"])
        self.assertEqual(base.FakeGoalManager.resume_calls, 0)
        self.assertEqual(self.admitted, [])
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="fixture-activity")
        await base.LinearPlatformAdapter._drain_outbox_once(self.adapter)
        self.adapter._linear.create_activity.assert_awaited_once()
        self.assertEqual(self.adapter._linear.create_activity.await_args.args,
                         ("linear-session", "error", item["payload"]["body"]))
        await self.adapter.on_processing_complete(event, base.ProcessingOutcome.SUCCESS)
        self.assertFalse(await base.LinearPlatformAdapter._drain_outbox_once(self.adapter))
        self.assertEqual(manager.state.status, "paused")

    async def test_diagnostic_explanation_never_overrides_negative_gates(self):
        cases = ("stop", "cancel", "done", "approval", "input", "delegate",
                 "owner", "blocker", "terminal", "failed", "budget", "stale", "rotation")
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
                if case == "budget": result["completed"] = False; result["turn_exit_reason"] = "max_iterations_reached(90)"
                if case == "stale": state.last_turn_at = 1.0
                if case == "rotation": self.adapter.gateway_runner.async_session_store.entry.session_id = "rotated"
                self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(return_value=state)
                self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
                await self.adapter._prepare_native_owned_turn_delivery(event, "DO_NOT_EXPOSE", result)
                row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
                item = self.adapter._ledger.get_outbox_item(f"activity:turn-decision:{row['decision_id']}")
                self.assertNotIn("DO_NOT_EXPOSE", item["payload"]["body"])
                self.assertNotEqual(item["payload"]["activity_type"], "response")
                self.assertEqual(self.admitted, [])
                self.assertEqual(base.FakeGoalManager.resume_calls, 0)

    async def test_error_explanation_survives_ledger_reopen_without_duplicate(self):
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
        call = self.adapter._linear.create_activity.await_args
        self.assertEqual(call.args[1], "error")
        self.assertIn("RECOVERABLE_EXPLANATION", call.args[2])
        self.assertEqual(self.admitted, [])
