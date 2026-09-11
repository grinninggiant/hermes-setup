"""Captured timeout -> paused -> human answer -> fresh judge -> one delivery.

Vendor I/O is an isolated fixture, never a fabricated production webhook.
"""
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from unittest import mock
import time
import unittest
import test_native_continuation as base


class LateReplyVendorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_paginated_question_reply_and_stop_boundary(self):
        def activity(id, sec, kind, user, body="", signal=None):
            return {"id": id, "createdAt": f"2026-09-11T10:32:{sec:02d}.000Z",
                "signal": signal, "user": {"id": user, "app": user == "app-user"},
                "content": {"__typename": kind, "body": body}}
        q = activity("q", 1, "AgentActivityElicitationContent", "app-user", "Choice?")
        a = activity("a", 3, "AgentActivityPromptContent", "human-1", "1")
        client = base.LinearClient(oauth_file="unused")
        client.actor_id = "app-user"
        async def check(rows):
            async def graphql(query, variables):
                page = [rows[0]] if not variables["after"] else rows[1:]
                return {"agentSession": {"id": "s", "activities": {"nodes": page,
                    "pageInfo": {"hasNextPage": not bool(variables["after"]), "endCursor": "next"}}}}
            client.graphql = graphql
            return await client.verify_late_clarify_reply("s", "q", "a", "human-1", "1")
        self.assertTrue(await check([a, q]))
        stop = activity("stop", 2, "AgentActivityPromptContent", "human-1", "", "stop")
        self.assertFalse(await check([a, stop, q]))
        self.assertFalse(await check([a, activity("new-q", 2, "AgentActivityElicitationContent", "app-user"), q]))
        self.assertFalse(await check([a, activity("other", 4, "AgentActivityPromptContent", "human-1", "2"), q]))
        wrong = deepcopy(a)
        wrong["user"]["id"] = "other-human"
        self.assertFalse(await check([wrong, q]))
        self.assertFalse(await check([a, a]))


class LateClarifyGoalTests(base.NativeContinuationTests):
    async def timeout_fixture(self):
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.state = SimpleNamespace(status="active", created_at=123.0,
            turns_used=1, max_turns=20, last_turn_at=1.0,
            last_verdict="continue", paused_reason=None)
        async def read_state(*args, **kwargs):
            return deepcopy(self.state)
        async def resume(*args, reset_budget, **kwargs):
            self.assertFalse(reset_budget)
            self.state.status = "active"
            self.state.paused_reason = None
            return SimpleNamespace(state=deepcopy(self.state), continuation_prompt="continue")
        self.adapter.gateway_runner.goal_state_for_source = read_state
        self.adapter.gateway_runner.resume_goal_for_source = mock.AsyncMock(side_effect=resume)
        self.adapter._linear.description = "## Acceptance\n- [x] verified plan"
        original_context = self.adapter._linear.get_agent_turn_context
        async def context(sid):
            value = await original_context(sid)
            value["issue"]["assignee"] = {"id": "human-1", "app": False}
            return value
        self.adapter._linear.get_agent_turn_context = context
        self.adapter._linear.verify_late_clarify_reply = mock.AsyncMock(return_value=True)
        question = base.turn_event()
        question._gateway_turn_result = MappingProxyType({**dict(question._gateway_turn_result),
            "completed": True, "turn_exit_reason": "completed"})
        question.metadata["linear_clarify_id"] = "timeout-q"
        await self.adapter.on_processing_start(question)
        self.adapter._ledger.enqueue_outbox("activity:clarify:timeout-q", "linear-session",
            "activity.create", {"activity_id": "question-id", "activity_type": "elicitation",
                "agent_session_id": "linear-session", "clarify_id": "timeout-q"})
        self.adapter._ledger.mark_outbox_delivered("activity:clarify:timeout-q")
        # This state change represents the timeout turn's native judge, not an
        # authorization inferred from prose. Marker creation must bind this turn.
        self.state.status = "paused"
        self.state.turns_used = 2
        self.state.last_turn_at = time.time()
        self.state.last_verdict = "blocked"
        self.state.paused_reason = "judged unachievable: awaiting human answer"
        await self.adapter.prepare_turn_delivery(question, "No answer yet", question._gateway_turn_result)
        await self.adapter.on_processing_complete(question, base.ProcessingOutcome.SUCCESS)
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])
        reply = base.turn_event()
        reply.message_id = "late-answer-event"
        reply.metadata["linear_action"] = "prompted"
        reply._linear_verified_normal_prompt = True
        reply.raw_message = {"agentActivity": {"id": "answer-id", "userId": "human-1",
            "user": {"id": "human-1"}, "agentSessionId": "linear-session",
            "content": {"type": "prompt", "body": "1"}}}
        reply._gateway_turn_result = MappingProxyType({**dict(reply._gateway_turn_result),
            "completed": True, "turn_exit_reason": "completed"})
        return reply

    async def test_late_answer_must_not_inherit_timeout_goal_verdict(self):
        reply = await self.timeout_fixture()
        # A restart may discard all Python objects. The timeout provenance lives
        # in the existing SQLite outbox, not an in-memory registry guess.
        ledger_path = str(base.Path(self.temp.name) / "ledger.sqlite3")
        self.adapter._ledger.close()
        self.adapter._ledger = base.DeliveryLedger(ledger_path, startup_recovery=False)
        self.assertFalse(await self.adapter._prepare_bound_linear_ingress(reply))
        self.assertFalse(await self.adapter._prepare_bound_linear_ingress(reply))
        self.adapter.gateway_runner.resume_goal_for_source.assert_awaited_once()
        self.assertEqual(self.state.status, "active")
        self.assertEqual(self.state.turns_used, 2)
        await self.adapter.on_processing_start(reply)
        await self.adapter.prepare_turn_delivery(reply, "answer received", reply._gateway_turn_result)
        # Real core post-turn method, vendor/model transport substituted only.
        from gateway.run_goals import GatewayGoalsMixin
        manager = mock.Mock()
        manager.is_active.return_value = self.state.status == "active"
        def judge(*args, **kwargs):
            self.state.status = "done"
            self.state.turns_used = 3
            self.state.last_turn_at = time.time()
            self.state.last_verdict = "done"
            return {"status": "done", "verdict": "done", "message": "", "should_continue": False}
        manager.evaluate_after_turn.side_effect = judge
        async def execute(fn):
            return fn()
        runner = SimpleNamespace(_post_turn_manager=mock.AsyncMock(return_value=manager),
            _session_key_for_source=lambda source: None, _run_in_executor_with_context=execute)
        await GatewayGoalsMixin._post_turn_goal_continuation(runner,
            session_entry=SimpleNamespace(session_id="hermes-session"), source=reply.source,
            final_response="answer received")
        manager.evaluate_after_turn.assert_called_once()
        await self.adapter.on_processing_complete(reply, base.ProcessingOutcome.SUCCESS)
        await self.adapter.on_processing_complete(reply, base.ProcessingOutcome.SUCCESS)
        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual([(r["ordinal"], r["outcome"]) for r in rows], [(3, "success")])
        count = self.adapter._ledger._db.execute("SELECT count(*) FROM outbox WHERE json_extract(payload_json,'$.activity_type')='response'").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_schema_shaped_signed_late_answer_reaches_rearm_once(self):
        reply = await self.timeout_fixture()
        reply.raw_message["agentActivity"]["content"]["body"] = "![image](https://example.invalid/image.png)"
        self.adapter._signing_secrets = ("s" * 32,)
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()
        self.adapter._schedule_thought = mock.Mock()
        received = []
        async def ingress(event):
            self.assertFalse(await self.adapter._prepare_bound_linear_ingress(event))
            received.append(event)
            event._gateway_accepted = True
        self.adapter.handle_message = ingress
        payload = {"type": "AgentSessionEvent", "action": "prompted", "organizationId": "org",
            "webhookId": "subscription", "webhookTimestamp": int(time.time() * 1000),
            "agentSession": {"id": "linear-session", "status": "active", "issue": {"id": "issue-164"}},
            "agentActivity": reply.raw_message["agentActivity"]}
        response = await self.adapter._handle_webhook(base.FakeRequest(payload))
        self.assertEqual(base.json.loads(response.text)["status"], "accepted")
        replay = await self.adapter._handle_webhook(base.FakeRequest(payload))
        self.assertEqual(base.json.loads(replay.text)["status"], "duplicate")
        self.assertEqual(len(received), 1)
        self.assertFalse(received[0].source.user_id)
        self.adapter.gateway_runner.resume_goal_for_source.assert_awaited_once()

    async def test_rearm_preserves_control_and_revision_boundaries(self):
        reply = await self.timeout_fixture()
        baseline = deepcopy(self.state)
        raw = deepcopy(reply.raw_message)
        marker = self.adapter._ledger.latest_clarify_timeout("linear-session")["payload"]
        cases = ["internal", "unsigned", "stop", "slash", "wrong_author", "wrong_session",
                 "budget", "pause_revision", "goal_revision", "missing_marker", "consumed", "vendor_race"]
        for case in cases:
            with self.subTest(case=case):
                self.state = deepcopy(baseline)
                reply.raw_message = deepcopy(raw)
                reply.internal = False
                reply._linear_verified_normal_prompt = True
                self.adapter._ledger.update_outbox_payload_metadata("activity:clarify:timeout-q",
                    {**marker, "late_reply_id": None})
                self.adapter._linear.verify_late_clarify_reply.side_effect = None
                a = reply.raw_message["agentActivity"]
                if case == "internal": reply.internal = True
                elif case == "unsigned": reply._linear_verified_normal_prompt = False
                elif case == "stop": a["signal"] = "stop"
                elif case == "slash": a["content"]["body"] = "/approve"
                elif case == "wrong_author": a["userId"] = "other"
                elif case == "wrong_session": a["agentSessionId"] = "other-session"
                elif case == "budget": self.state.max_turns = self.state.turns_used
                elif case == "pause_revision": self.state.paused_reason = "user-paused"
                elif case == "goal_revision": self.state.created_at += 1
                elif case == "missing_marker":
                    self.adapter._ledger.update_outbox_payload_metadata("activity:clarify:timeout-q", {"clarify_timeout_goal": {}})
                elif case == "consumed":
                    self.adapter._ledger.update_outbox_payload_metadata("activity:clarify:timeout-q", {"late_reply_id": "different-reply"})
                elif case == "vendor_race":
                    async def change_during_read(*args):
                        self.state.paused_reason = "user-paused"
                        return True
                    self.adapter._linear.verify_late_clarify_reply.side_effect = change_during_read
                expected = {
                    "internal": "prompt_provenance_unverified", "unsigned": "prompt_provenance_unverified",
                    "stop": "control_signal", "slash": "command_body", "wrong_author": "author_envelope_mismatch",
                    "wrong_session": "session_envelope_mismatch", "budget": "goal_budget_exhausted",
                    "pause_revision": "timeout_goal_revision_mismatch", "goal_revision": "timeout_goal_revision_mismatch",
                    "missing_marker": "timeout_goal_revision_mismatch", "consumed": "different_reply_consumed",
                    "vendor_race": "goal_changed_during_vendor_read",
                }
                with self.assertLogs(base.adapter_mod.logger.name, level="INFO") as logs:
                    self.assertFalse(await self.adapter._resume_late_clarify_goal(reply, "hermes-session", deepcopy(self.state)))
                self.assertIn("reason=" + expected[case], "\n".join(logs.output))
                self.adapter.gateway_runner.resume_goal_for_source.assert_not_awaited()

    async def test_old_paused_goal_reports_stale_judge_instead_of_unverified(self):
        reply = await self.timeout_fixture()
        await self.adapter.on_processing_start(reply)
        await self.adapter.prepare_turn_delivery(reply, "answer received", reply._gateway_turn_result)
        await self.adapter.on_processing_complete(reply, base.ProcessingOutcome.SUCCESS)
        row = self.adapter._ledger.list_turn_decisions("linear-session")[0]
        self.assertEqual(row["error"], "native_goal_not_rejudged")
        notice = self.adapter._continuation_blocker_notice(row["error"], step="test")
        self.assertIn("native_goal_not_rejudged", notice)
        self.assertNotIn("awaiting human answer", notice)

    async def test_late_reply_rejection_reports_exact_safe_boundary(self):
        reply = await self.timeout_fixture()
        reply.raw_message["agentActivity"]["userId"] = None
        reply.raw_message["agentActivity"]["content"]["body"] = "![image](https://example.invalid/private?signature=DO_NOT_LOG)"
        with self.assertLogs(base.adapter_mod.logger.name, level="INFO") as logs:
            self.assertTrue(await self.adapter._prepare_bound_linear_ingress(reply))
        output = "\n".join(logs.output)
        self.assertIn("reason=author_missing", output)
        self.assertNotIn("DO_NOT_LOG", output)
        self.assertNotIn("example.invalid", output)
        self.adapter._linear.verify_late_clarify_reply.assert_not_awaited()
        self.adapter.gateway_runner.resume_goal_for_source.assert_not_awaited()

    async def test_timeout_revision_and_vendor_rejections_are_distinguishable(self):
        reply = await self.timeout_fixture()
        self.state.created_at += 1
        with self.assertLogs(base.adapter_mod.logger.name, level="INFO") as logs:
            self.assertFalse(await self.adapter._resume_late_clarify_goal(reply, "hermes-session", deepcopy(self.state)))
        self.assertIn("reason=timeout_goal_revision_mismatch", "\n".join(logs.output))
        self.state.created_at -= 1
        self.adapter._linear.verify_late_clarify_reply.return_value = False
        with self.assertLogs(base.adapter_mod.logger.name, level="INFO") as logs:
            self.assertFalse(await self.adapter._resume_late_clarify_goal(reply, "hermes-session", deepcopy(self.state)))
        self.assertIn("reason=vendor_evidence_unverified", "\n".join(logs.output))
        self.adapter.gateway_runner.resume_goal_for_source.assert_not_awaited()

    async def test_owner_delegate_and_approval_rejections_remain_closed(self):
        reply = await self.timeout_fixture()
        original = self.adapter._linear.get_agent_turn_context
        for case in ("owner", "delegate", "approval"):
            with self.subTest(case=case):
                async def context(sid):
                    value = await original(sid)
                    if case == "owner":
                        value["issue"]["assignee"]["id"] = "different-human"
                    elif case == "delegate":
                        value["issue"]["delegate"]["id"] = "different-agent"
                    else:
                        value["status"] = "awaitingInput"
                    return value
                self.adapter._linear.get_agent_turn_context = context
                with self.assertLogs(base.adapter_mod.logger.name, level="INFO") as logs:
                    self.assertFalse(await self.adapter._resume_late_clarify_goal(reply, "hermes-session", deepcopy(self.state)))
                expected = "owner_mismatch" if case == "owner" else "live_lifecycle_denied"
                self.assertIn("reason=" + expected, "\n".join(logs.output))
                self.adapter.gateway_runner.resume_goal_for_source.assert_not_awaited()

    async def test_unverified_late_reply_never_rearms_pause(self):
        reply = await self.timeout_fixture()
        self.adapter._linear.verify_late_clarify_reply.return_value = False
        self.assertTrue(await self.adapter._prepare_bound_linear_ingress(reply))
        self.adapter.gateway_runner.resume_goal_for_source.assert_not_awaited()
        self.assertEqual(self.state.status, "paused")
