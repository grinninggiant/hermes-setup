"""Real wait -> ingress -> core admission/outbox, with offline vendor I/O only."""
import asyncio
from copy import deepcopy
import unittest
from unittest import mock

import test_native_continuation as base


class DependencyLinear(base.FakeLinear, base.LinearClient):
    def __init__(self):
        base.FakeLinear.__init__(self, status="awaitingInput")
        self.activities = []
        self.created = []
        self.reads = []

    async def get_open_blockers(self, issue_id):
        return []

    async def get_agent_session_delivery_context(self, session_id):
        return {"id": session_id, "app_user_id": self.actor_id}

    async def graphql(self, query, variables):
        # Deliberately newest-first and paginated; no production API/token.
        rows = list(reversed(self.activities))
        after = variables.get("after")
        return {"agentSession": {"id": "linear-session", "activities": {
            "nodes": deepcopy(rows[1:] if after else rows[:1]),
            "pageInfo": {"hasNextPage": not bool(after) and len(rows) > 1,
                         "endCursor": "next"},
        }}}

    def activity(self, activity_id, kind, body="", signal=None):
        row = {"id": activity_id, "createdAt": f"2026-09-22T18:01:{len(self.activities):02d}.000Z",
               "signal": signal, "user": {"id": self.actor_id, "app": True},
               "content": {"__typename": f"AgentActivity{kind.title()}Content", "body": body}}
        self.activities.append(row)
        return row

    async def create_activity(self, session_id, kind, body, *, activity_id, ephemeral=False):
        self.created.append((kind, body))
        self.activity(activity_id, kind, body)
        self.status = "awaitingInput" if kind == "elicitation" else "active"
        return activity_id

    async def get_agent_turn_context(self, session_id):
        self.reads.append(self.status)
        return await super().get_agent_turn_context(session_id)


class DependencyResumeTests(unittest.IsolatedAsyncioTestCase):
    adapter: base.LinearPlatformAdapter
    drain_patch: mock._patch
    asyncSetUp = base.NativeContinuationTests.asyncSetUp
    asyncTearDown = base.NativeContinuationTests.asyncTearDown

    def fixture(self):
        self.drain_patch.stop()
        del self.adapter.handle_message  # Exercise the real ingress, not the older suite's mock.
        self.adapter._linear = DependencyLinear()
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.payload = {"action": "created", "actor": {"id": "human-1", "name": "Human"},
                        "agentSession": {"id": "linear-session", "issue": {
                            "id": "issue-164", "identifier": "OPS-164", "title": "Resume"}}}
        self.adapter._ledger.put_wait("linear-session", "issue-164", "dependency-delivery",
                                      self.payload, [{"id": "old-blocker", "identifier": "OPS-209"}])
        # The old local outbox receipt may have been pruned. Native history survives.
        self.question_id = self.adapter._activity_uuid("waiting:dependency-delivery")
        self.adapter._linear.activity(self.question_id, "elicitation", "Waiting for blocking issue(s): OPS-209.")
        self.reached_handler = []

        async def handler(event):
            self.reached_handler.append(event)
            return None

        self.adapter.set_message_handler(handler)

    async def settle(self):
        await self.adapter._wait_for_thought("linear-session")
        await asyncio.gather(*list(self.adapter._session_tasks.values()))

    async def old_false_resume(self):
        self.fixture()
        self.adapter.gateway_runner.async_session_store.load_transcript = mock.AsyncMock(return_value=[])
        ledger = self.adapter._ledger
        ledger.claim_wait("linear-session")
        ledger.mark_wait_resumed("linear-session")
        # The old progress was delivered BEFORE the erroneous ingress veto.
        self.adapter._enqueue_activity("linear-session", "thought", "Preparing admission",
                                       item_key="thought:dependency-delivery")
        await self.adapter._post_thought("linear-session")
        event = self.adapter._message_event(self.payload, "dependency-delivery", "dependency-resume",
                                            dependency_resume=True)
        await self.adapter._visible_ingress_veto(event, "awaiting_input")
        await self.adapter._post_thought("linear-session")
        self.assertEqual(self.adapter._linear.status, "awaitingInput")

    async def test_old_false_resume_is_recovered_through_real_admission_once(self):
        await self.old_false_resume()
        self.assertTrue(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        self.assertTrue(await self.adapter._reconcile_wait("linear-session"))
        await self.settle()
        self.assertEqual(len(self.reached_handler), 1)
        self.assertEqual(self.adapter._ledger.get_wait("linear-session")["state"], "resumed")
        self.assertFalse(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))

    async def test_old_false_resume_never_replays_work_or_real_controls(self):
        for case in ("history", "read_error", "goal", "stop", "question", "response",
                     "foreign_veto", "changed_veto", "missing_veto", "closed", "stale_revision"):
            with self.subTest(case=case):
                await self.old_false_resume()
                linear = self.adapter._linear
                store = self.adapter.gateway_runner.async_session_store
                if case == "history":
                    store.load_transcript.return_value = [{"role": "assistant", "content": "Started"}]
                elif case == "read_error":
                    store.load_transcript.side_effect = RuntimeError("unavailable")
                elif case == "goal":
                    base.FakeGoalManager.existing = True
                elif case in {"stop", "question", "response"}:
                    linear.activity("new", "prompt" if case == "stop" else "elicitation" if case == "question" else "response",
                                    "Real control", "stop" if case == "stop" else None)
                elif case == "foreign_veto":
                    linear.activities[-1]["user"]["id"] = "other-app"
                elif case == "changed_veto":
                    linear.activities[-1]["content"]["body"] = "A real question?"
                elif case == "missing_veto":
                    linear.activities.pop()
                elif case == "closed":
                    self.adapter._ledger.has_session_closure = lambda _sid: True
                elif case == "stale_revision":
                    original = linear.verify_dependency_wait
                    async def race(*args, **kwargs):
                        result = await original(*args, **kwargs)
                        ledger = self.adapter._ledger
                        ledger.requeue_unadmitted_wait("linear-session", ledger.get_wait("linear-session")["revision"])
                        return result
                    linear.verify_dependency_wait = race
                self.assertFalse(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
                await self.settle()
                self.assertEqual(self.reached_handler, [])
            await self.asyncTearDown()
            await self.asyncSetUp()

    async def test_dependency_wait_is_progressed_before_ingress_and_reaches_core_once(self):
        self.fixture()
        resumed = await self.adapter._reconcile_wait("linear-session")
        await self.settle()
        wait = self.adapter._ledger.get_wait("linear-session")
        self.assertEqual(len(self.reached_handler), 1,
                         f"resumed={resumed}, ledger={wait['state']}, native={self.adapter._linear.status}")
        self.assertTrue(resumed)
        self.assertEqual(wait["state"], "resumed")
        self.assertTrue(self.reached_handler[0]._gateway_accepted)
        self.assertIn("active", self.adapter._linear.reads)
        self.assertFalse(any(kind == "elicitation" for kind, _ in self.adapter._linear.created))
        self.assertFalse(await self.adapter._reconcile_wait("linear-session"))

    async def startup_queue(self, *, recovered=False):
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.run_startup import GatewayStartupMixin
        from types import MethodType

        if recovered:
            await self.old_false_resume()
            self.assertTrue(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        else:
            self.fixture()
        runner = self.adapter.gateway_runner
        runner.config = base.SimpleNamespace(multiplex_profiles=False)
        runner._startup_restore_in_progress = True
        runner._startup_restore_queue = []
        runner._queue_startup_restore_event = MethodType(GatewayStartupMixin._queue_startup_restore_event, runner)
        runner._adapter_for_source = lambda _source: self.adapter
        self.goal = None
        ensure = runner.ensure_goal_for_source

        async def persistent_ensure(*args, **kwargs):
            if self.goal is None:
                self.goal = await ensure(*args, **kwargs)
                self.goal.created_at = base.adapter_mod.time.time()
                self.goal.last_turn_at = 0
            return self.goal

        async def goal_read(*args, **kwargs):
            return self.goal

        async def handler(event):
            if not getattr(event, "_hermes_startup_restore_replay", False):
                self.assertIsNone(await GatewayInboundMixin._hm_admit_event(runner, event))
            else:
                self.reached_handler.append(event)

        runner.ensure_goal_for_source = persistent_ensure
        runner.goal_state_for_source = goal_read
        self.adapter.set_message_handler(handler)
        self.assertTrue(await self.adapter._reconcile_wait("linear-session"))
        await self.settle()
        self.assertEqual(len(runner._startup_restore_queue), 1)
        self.assertEqual(self.reached_handler, [])
        return runner, MethodType(GatewayStartupMixin._drain_startup_restore_queue, runner)

    async def test_startup_restore_replays_same_admission_not_a_new_goal(self):
        runner, drain = await self.startup_queue()
        event = runner._startup_restore_queue[0]
        goal = self.goal
        self.assertEqual(await drain(), 1)
        await self.settle()
        self.assertEqual(self.reached_handler, [event])
        self.assertIs(self.goal, goal)
        self.assertEqual(len(self.adapter._linear.created), 1)
        self.assertFalse(await self.adapter._reconcile_wait("linear-session"))
        self.assertEqual(await drain(), 0)

    async def test_startup_replay_preserves_controls_and_goal_ownership(self):
        from tools import approval

        for case in ("stop", "question", "paused", "replaced", "executed", "approval", "revision"):
            with self.subTest(case=case):
                runner, drain = await self.startup_queue()
                event = runner._startup_restore_queue[0]
                assert self.goal is not None
                if case == "stop":
                    self.adapter._linear.activity("native-stop", "prompt", "", "stop")
                    self.adapter._ledger.cancel_wait("linear-session")
                elif case == "question":
                    self.adapter._linear.activity("new-question", "elicitation", "Approval required")
                elif case == "paused":
                    self.goal.status = "paused"
                elif case == "replaced":
                    self.goal.created_at += 1
                elif case == "executed":
                    self.goal.turns_used = 1
                elif case == "revision":
                    self.adapter._ledger.requeue_unadmitted_wait("linear-session", event._linear_dependency_revision)
                with mock.patch.object(approval, "get_pending_gateway_approval",
                                       return_value={} if case == "approval" else None):
                    await drain()
                await self.settle()
                self.assertEqual(self.reached_handler, [])
            await self.asyncTearDown()
            await self.asyncSetUp()

    async def stranded_goal(self):
        runner, _drain = await self.startup_queue(recovered=True)
        runner._startup_restore_queue.clear()  # Lost process-local queue, as on restart.
        runner._startup_restore_in_progress = False
        runner._is_session_running = lambda _key: False
        self.adapter._active_turn_events.clear()
        self.adapter._linear.status = "stale"

        async def handler(event):
            self.reached_handler.append(event)

        self.adapter.set_message_handler(handler)
        return runner

    async def test_zero_execution_recovery_uses_same_goal_and_durable_once_claim(self):
        await self.stranded_goal()
        goal = self.goal
        self.assertTrue(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        await self.settle()
        self.assertEqual(len(self.reached_handler), 1)
        event = self.reached_handler[0]
        self.assertTrue(event.internal)
        self.assertTrue(event.metadata["linear_dependency_resume"])
        self.assertIs(self.goal, goal)
        decision = self.adapter._ledger.get_turn_decision(event.metadata["linear_continuation_decision_id"])
        self.assertEqual(decision["dispatch_state"], "completed")
        self.adapter._active_turn_events.clear()
        self.assertFalse(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        await self.adapter._recover_turn_decisions()
        await self.settle()
        self.assertEqual(len(self.reached_handler), 1)

    async def test_zero_execution_recovery_never_claims_started_or_unverified_work(self):
        for case in ("history", "last_turn", "paused", "owner", "startup", "progress_missing",
                     "stop", "question", "binding", "revision_race", "goal_race"):
            with self.subTest(case=case):
                runner = await self.stranded_goal()
                assert self.goal is not None
                linear = self.adapter._linear
                if case == "history":
                    runner.async_session_store.load_transcript.return_value = [{"role": "user", "content": "Started"}]
                elif case == "last_turn":
                    self.goal.last_turn_at = 1
                elif case == "paused":
                    self.goal.status = "paused"
                elif case == "owner":
                    runner._is_session_running = lambda _key: True
                elif case == "startup":
                    runner._startup_restore_in_progress = True
                elif case == "progress_missing":
                    original = self.adapter._ledger.get_outbox_item
                    self.adapter._ledger.get_outbox_item = lambda key: None if key.startswith("activity:dependency-resume:") else original(key)
                elif case in {"stop", "question"}:
                    linear.activity("new-control", "prompt" if case == "stop" else "elicitation", "Real input", "stop" if case == "stop" else None)
                elif case == "binding":
                    runner.async_session_store.entry.session_key = "wrong"
                elif case in {"revision_race", "goal_race"}:
                    original = linear.verify_dependency_wait
                    async def race(*args, **kwargs):
                        result = await original(*args, **kwargs)
                        if case == "goal_race":
                            self.goal.created_at += 10
                        else:
                            wait = self.adapter._ledger.get_wait("linear-session")
                            self.adapter._ledger.requeue_unadmitted_wait("linear-session", wait["revision"])
                        return result
                    linear.verify_dependency_wait = race
                self.assertFalse(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
                await self.settle()
                self.assertEqual(self.reached_handler, [])
            await self.asyncTearDown()
            await self.asyncSetUp()

    async def test_zero_execution_retry_cannot_escape_into_generic_goal_recovery(self):
        await self.stranded_goal()
        admit = self.adapter._admit_turn_event
        self.adapter._admit_turn_event = mock.AsyncMock(side_effect=RuntimeError("transport unavailable"))
        self.assertFalse(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        await self.adapter._recover_turn_decisions()
        self.assertEqual(self.adapter._admit_turn_event.await_count, 1)
        self.adapter._admit_turn_event = admit
        self.assertTrue(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        await self.settle()
        self.assertEqual(len(self.reached_handler), 1)

    async def test_processing_hook_read_failure_cannot_skip_durable_running_claim(self):
        await self.stranded_goal()
        start = self.adapter.on_processing_start
        starts = []

        async def failing_start(event):
            starts.append(event)
            with mock.patch.object(self.adapter._linear, "verify_dependency_wait",
                                   side_effect=RuntimeError("history read unavailable")):
                return await start(event)

        self.adapter.on_processing_start = failing_start
        self.assertTrue(await self.adapter._recover_unadmitted_dependency_wait("linear-session"))
        await self.settle()
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.reached_handler, [])
        decision = self.adapter._ledger.get_turn_decision(starts[0].metadata["linear_continuation_decision_id"])
        self.assertEqual(decision["dispatch_state"], "enqueued")

    async def test_real_gates_and_missing_admission_never_mark_resumed(self):
        from tools import approval, clarify_gateway

        for case in ("question", "approval_question", "stop", "response", "unknown",
                     "foreign_question", "missing_question", "completed", "canceled",
                     "delegate", "actor", "binding", "paused_goal", "approval_waiter",
                     "clarify_waiter", "no_handler", "stale_readback"):
            with self.subTest(case=case):
                self.fixture()
                linear = self.adapter._linear
                context = await linear.get_agent_turn_context("linear-session")
                if case in {"question", "approval_question", "stop", "response", "unknown"}:
                    kind = {"question": "elicitation", "approval_question": "elicitation",
                            "stop": "prompt", "response": "response", "unknown": "unknown"}[case]
                    linear.activity("new-activity", kind, "A real question?", "stop" if case == "stop" else None)
                elif case == "foreign_question":
                    linear.activities[0]["user"]["id"] = "another-app"
                elif case == "missing_question":
                    linear.activities.clear()
                elif case in {"completed", "canceled"}:
                    context["issue"]["state"]["type"] = case
                elif case == "delegate":
                    context["issue"]["delegate"]["id"] = "another-app"
                elif case == "actor":
                    context["app_user_id"] = "another-app"
                elif case == "binding":
                    self.adapter.gateway_runner.async_session_store.entry.session_key = "wrong-key"
                elif case == "paused_goal":
                    base.FakeGoalManager.existing = True
                    base.FakeGoalManager.existing_status = "paused"
                elif case == "no_handler":
                    self.adapter._message_handler = None
                if case in {"completed", "canceled", "delegate", "actor", "stale_readback"}:
                    linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
                with mock.patch.object(approval, "get_pending_gateway_approval",
                                       return_value={} if case == "approval_waiter" else None), \
                     mock.patch.object(clarify_gateway, "get_pending_for_session",
                                       return_value=object() if case == "clarify_waiter" else None):
                    self.assertFalse(await self.adapter._reconcile_wait("linear-session"))
                await self.settle()
                self.assertEqual(self.reached_handler, [])
                self.assertNotEqual(self.adapter._ledger.get_wait("linear-session")["state"], "resumed")
                if case not in {"no_handler", "stale_readback"}:
                    self.assertFalse(any(kind == "thought" for kind, _ in linear.created))
            await self.asyncTearDown()
            await self.asyncSetUp()

    async def test_stop_at_last_goal_await_or_processing_start_never_reaches_handler(self):
        for point in ("goal_read", "goal_create", "processing_start"):
            with self.subTest(point=point):
                self.fixture()
                original_read = self.adapter._goal_state_for_source
                original_ensure = self.adapter._ensure_goal_for_source
                original_start = self.adapter.on_processing_start
                reads, creates = [], []

                async def read(*args):
                    result = await original_read(*args)
                    reads.append(True)
                    if point == "goal_read" and len(reads) == 2:
                        self.adapter._ledger.cancel_wait("linear-session")
                    return result

                async def ensure(*args):
                    creates.append(True)
                    result = await original_ensure(*args)
                    if point == "goal_create":
                        self.adapter._ledger.cancel_wait("linear-session")
                    return result

                async def start(event):
                    if point == "processing_start":
                        self.adapter._linear.activity("last-moment-stop", "prompt", "", "stop")
                    return await original_start(event)

                self.adapter._goal_state_for_source = read
                self.adapter._ensure_goal_for_source = ensure
                self.adapter.on_processing_start = start
                await self.adapter._reconcile_wait("linear-session")
                await self.adapter._wait_for_thought("linear-session")
                await asyncio.gather(*list(self.adapter._session_tasks.values()), return_exceptions=True)
                self.assertEqual(self.reached_handler, [], point)
                if point == "goal_read":
                    self.assertEqual(creates, [], "Stop during goal read must prevent goal creation")
            await self.asyncTearDown()
            await self.asyncSetUp()

    async def test_new_question_during_progress_read_cannot_be_overwritten(self):
        self.fixture()
        linear = self.adapter._linear
        original = linear.get_agent_session_delivery_context

        async def question_arrives(session_id):
            result = await original(session_id)
            if not linear.created:
                linear.activity("new-question", "elicitation", "Approval required")
            return result

        linear.get_agent_session_delivery_context = question_arrives
        self.assertFalse(await self.adapter._reconcile_wait("linear-session"))
        await self.settle()
        self.assertEqual(self.reached_handler, [])
        self.assertFalse(any(kind == "thought" for kind, _ in linear.created))


if __name__ == "__main__":
    unittest.main()
