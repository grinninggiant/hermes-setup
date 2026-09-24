from __future__ import annotations

import hashlib
import hmac
import importlib.util
import asyncio
import contextvars
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource, build_session_key
from tools import clarify_gateway

ROOT = Path(__file__).resolve().parents[1]
NAME = "linear_native_clarify_test_plugin"
spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
sys.modules[NAME] = plugin
spec.loader.exec_module(plugin)
adapter_mod = __import__(f"{NAME}.adapter", fromlist=["*"])
ledger_mod = __import__(f"{NAME}.ledger", fromlist=["*"])
client_mod = __import__(f"{NAME}.linear_client", fromlist=["*"])


class Transport:
    def __init__(self, fail=False, retryable=False):
        self.fail = fail
        self.retryable = retryable
        self.activities = []
        self.evidence = {}
        self.sequence = 0

    def record_activity(self, session_id, activity_id, kind, body, user_id):
        self.sequence += 1
        self.evidence[activity_id] = {
            "id": activity_id, "agentSessionId": session_id,
            "createdAt": (datetime(2026, 1, 1, tzinfo=timezone.utc)
                          + timedelta(seconds=self.sequence)).isoformat(),
            "signal": None, "user": {"id": user_id, "app": user_id == "app-221"},
            "content": {"__typename": "AgentActivity" + kind.title() + "Content", "body": body},
        }

    async def create_activity(self, session_id, activity_type, body, *, activity_id, ephemeral=False):
        if self.fail:
            raise client_mod.LinearAPIError("vendor rejected activity", retryable=self.retryable)
        self.activities.append((session_id, activity_type, body))
        self.record_activity(session_id, activity_id, activity_type, body, "app-221")
        return activity_id


class Linear:
    organization_id = "org-221"
    actor_id = "app-221"
    actor_name = "Native app"
    verify_late_clarify_reply = client_mod.LinearClient.verify_late_clarify_reply
    verify_clarify_reply = client_mod.LinearClient.verify_clarify_reply
    _agent_activity_evidence = client_mod.LinearClient._agent_activity_evidence

    async def graphql(self, query, variables):
        assert "LinearAgentActivityEvidence" in query
        assert variables["after"] is None
        return {"agentSession": {"id": variables["id"], "activities": {
            "nodes": [row for row in self.transport.evidence.values()
                      if row["agentSessionId"] == variables["id"]],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}

    def __init__(self, transport, state="started", status="active", delegate_id=None):
        self.transport, self.state = transport, state
        self.status = status
        self.delegate_id = delegate_id or self.actor_id

    async def create_activity(self, *args, **kwargs):
        return await self.transport.create_activity(*args, **kwargs)

    async def get_agent_session_delivery_context(self, session_id):
        return {"id": session_id, "app_user_id": self.actor_id}

    async def get_agent_turn_context(self, session_id):
        return {"id": session_id, "app_user_id": self.actor_id, "status": self.status,
                "issue": {"id": "issue-221", "state": {"type": self.state},
                           "delegate": {"id": self.delegate_id}}}


def source(user_id="user-221"):
    return SessionSource(platform=Platform.WEBHOOK, chat_id="linear-session-221", user_id=user_id,
                         user_name="Human", chat_name="OPS-221", chat_type="dm")


class NativeClarifyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clarify_gateway.clear_session("clarify-test-session")
        self.tmp = tempfile.TemporaryDirectory()
        self.key = build_session_key(source(), group_sessions_per_user=True,
                                     thread_sessions_per_user=False, profile=None)
        clarify_gateway.clear_session(self.key)
        cfg = PlatformConfig(enabled=True, extra={"native_goal_continuation_enabled": True,
                                                  "database_path": str(Path(self.tmp.name) / "ledger.sqlite3"),
                                                  "oauth_file": "unused"})
        self.adapter = adapter_mod.LinearPlatformAdapter(cfg, Platform.WEBHOOK)
        self.adapter._ledger = ledger_mod.DeliveryLedger(cfg.extra["database_path"], startup_recovery=False)
        self.transport = Transport()
        self.adapter._linear = Linear(self.transport)
        self.adapter._completed_turn_results = {}
        self.adapter._active_turn_events = {"linear-session-221": MessageEvent(
            text="turn", message_type=MessageType.TEXT, source=source(), metadata={
                "linear_agent_session_id": "linear-session-221", "linear_issue_id": "issue-221",
                "gateway_session_key": self.key, "gateway_session_id": "hermes-session-221",
                "linear_delivery_key": "turn-221"})}
        self.adapter.open_progress_turn("linear-session-221", "turn-221")
        self.adapter.gateway_runner = SimpleNamespace(async_session_store=SimpleNamespace(
            lookup_by_session_key=mock.AsyncMock(return_value=SimpleNamespace(
                session_key=self.key, session_id="hermes-session-221"))))
        store = self.adapter.gateway_runner.async_session_store
        store._lock = threading.Lock()
        store._entries = {self.key: store.lookup_by_session_key.return_value}

    def tearDown(self):
        clarify_gateway.clear_session(self.key)
        self.adapter._ledger.close()
        self.tmp.cleanup()

    def payload(self, actor="user-221", body="2", webhook="webhook-221"):
        self.transport.record_activity("linear-session-221", f"activity-{webhook}", "prompt", body, actor)
        return {"type": "AgentSessionEvent", "action": "prompted", "organizationId": "org-221",
                "webhookId": webhook, "webhookTimestamp": time.time(), "actor": {"id": actor},
                "agentSession": {"id": "linear-session-221", "status": "active", "issue": {"id": "issue-221"}},
                "agentActivity": {"id": f"activity-{webhook}", "body": body}}

    async def webhook(self, payload):
        raw = json.dumps(payload).encode()
        sig = hmac.new(b"secret-221", raw, hashlib.sha256).hexdigest()

        class Request:
            headers = {"Linear-Signature": sig}

            async def read(_self):
                return raw

        self.adapter._signing_secrets = ("secret-221",)
        return await self.adapter._handle_webhook(Request())

    async def publish_question(self, clarify_id, choices):
        await self.adapter._drain_outbox_once()
        entry = clarify_gateway.register(clarify_id, self.key, "Target?", choices,
            turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", choices, clarify_id, self.key)
        self.assertTrue(result.success, result.error)
        return entry

    def _foreground_context(self, turn_id):
        from agent.tool_executor import _ToolCallRef, _pre_tool_block, _resolve_sequential_dispatch
        from gateway.run_turn_runner import TurnRunner
        from gateway.session_context import set_session_vars
        from hermes_cli import plugins

        context = contextvars.copy_context()
        context.run(set_session_vars, platform="linear", chat_id="linear-session-221",
                    session_key=self.key, session_id="hermes-session-221")
        runner = object.__new__(TurnRunner)
        # This fixture bypasses __init__, so preserve the actual per-runner
        # registration fence introduced by the paired core candidate.
        runner._clarify_lock = threading.Lock()
        runner._clarify_ids = set()
        runner._clarify_closed = False
        runner._ctx = SimpleNamespace(
            _status_adapter=self.adapter, session_key=self.key,
            _status_chat_id="linear-session-221", _status_thread_metadata={})
        runner._close_native_stream_boundary = lambda *a, **kw: True
        runner._stream_consumer = lambda: None
        loop = asyncio.get_running_loop()
        runner._schedule = lambda coro, _message: asyncio.run_coroutine_threadsafe(coro, loop)
        agent = SimpleNamespace(session_id="hermes-session-221", _current_turn_id=turn_id,
                                clarify_callback=runner._clarify_callback_sync)
        ref = _ToolCallRef("clarify", {"question": "Target?", "choices": ["yes"]},
                           "task", turn_id, [])
        dispatch = _resolve_sequential_dispatch(agent, ref, [])
        manager = plugins.PluginManager(scope_key=self.tmp.name)
        manager._hooks["pre_tool_call"] = [plugin._pre_tool_progress]
        # Preserve the real bounded/copied-context dispatcher, not a direct hook call.
        with mock.patch.object(plugin, "_progress_adapter", return_value=self.adapter), \
                mock.patch.object(self.adapter, "schedule_tool_progress"), \
                mock.patch.object(plugins, "invoke_hook", manager.invoke_hook), \
                mock.patch.object(plugins, "_resolve_hook_callback_timeout", return_value=5):
            blocked, args = context.run(_pre_tool_block, agent, ref)
        self.assertIsNone(blocked)
        self.assertEqual(self.adapter._current_progress_turn_key("linear-session-221"), turn_id)
        return SimpleNamespace(context=context, execute=lambda: dispatch.execute(args), turn_id=turn_id)

    def _complete_foreground(self, context, turn_id):
        with mock.patch.object(plugin, "_progress_adapters", [self.adapter]):
            context.context.copy().run(plugin._on_session_end, platform="linear",
                        session_id="hermes-session-221", turn_id=turn_id,
                        completed=True, failed=False, interrupted=False,
                        turn_exit_reason="completed")

    async def _stage_queued_foreground(self):
        from gateway.run_turn import GatewayTurnMixin

        event = self.adapter._active_turn_events["linear-session-221"]
        entry = self.adapter.gateway_runner.async_session_store.lookup_by_session_key.return_value
        old = self._foreground_context("foreground-old")
        self._complete_foreground(old, "foreground-old")
        runner = SimpleNamespace(
            _immutable_gateway_turn_result=GatewayTurnMixin._immutable_gateway_turn_result,
            _run_agent_stream_confirmed_final_delivery=lambda *a, **kw: False,
            _is_intentional_silence=lambda *a: False,
            _pop_post_delivery_callback=lambda *a: None,
        )
        turn = SimpleNamespace(session_key=self.key, session_id=entry.session_id,
                               stream_consumer_holder=[None], gateway_event=event,
                               _run_still_current=lambda: True, run_generation=1)
        result = dict(self.adapter._completed_turn_results[event.source.chat_id],
                      final_response="Prior foreground result")
        # Exact core queued-first-response -> real adapter staging, not send().
        await GatewayTurnMixin._run_agent_deliver_first_response(
            runner, turn, self.adapter, result, result, None)
        self.assertIs(self.adapter._pending_turn_deliveries[event.source.chat_id][0], event)
        self.assertEqual(self.transport.activities, [])
        # Recursive queued execution need not call on_processing_start again.
        return old, self._foreground_context("foreground-new")

    async def _send_in_context(self, context, clarify_id, *, stop=False):
        # Clear FIFO predecessors; the real callback registers this probe's owner.
        clarify_gateway.clear_session(self.key)
        sends, owners = [], []
        send = self.adapter.send_clarify

        async def send_then_answer(**kwargs):
            pending = clarify_gateway.get_pending_for_session(self.key, include_choice_prompts=True)
            owners.append(getattr(pending, "turn_owner", None))
            if stop:
                clarify_gateway.clear_session(self.key)
            result = await send(**kwargs)
            sends.append(result)
            if result.success:
                response = await self.webhook(self.payload(body="1", webhook=clarify_id))
                self.assertEqual(json.loads(response.text)["status"], "clarify_resolved")
            return result

        with mock.patch.object(self.adapter, "send_clarify", send_then_answer), \
                mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex=clarify_id)):
            result = await asyncio.wait_for(
                asyncio.to_thread(context.context.run, context.execute), 10)
        self.assertEqual(owners, [("hermes-session-221", context.turn_id)])
        self.assertEqual(len(sends), 1)
        self.assertIsNone(clarify_gateway.get_pending_for_session(self.key, include_choice_prompts=True))
        if sends[0].success:
            self.assertEqual(json.loads(result)["user_response"], "yes")
        return sends[0]

    async def test_queued_followup_clarifies_after_real_core_staging(self):
        _, current = await self._stage_queued_foreground()
        result = await self._send_in_context(current, "queued-new")
        self.assertTrue(result.success, result.error)
        row = self.adapter._ledger.get_outbox_item("activity:clarify:queued-new")
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["payload"]["activity_type"], "elicitation")
        self.assertEqual(len(self.transport.activities), 1)
        self.assertEqual(row["payload"]["clarify_hermes_session_id"], "hermes-session-221")
        self.assertEqual(row["payload"]["clarify_hermes_turn_id"], "foreground-new")

    async def test_queued_late_old_callback_and_current_completion_stay_fenced(self):
        old, current = await self._stage_queued_foreground()
        result = await self._send_in_context(old, "late-old")
        self.assertFalse(result.success)
        self.assertIn("turn owner is no longer live", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:late-old"))
        self._complete_foreground(current, "foreground-new")
        # An out-of-order old end hook must not erase the newer completed fence.
        self._complete_foreground(old, "foreground-old")
        result = await self._send_in_context(current, "completed")
        self.assertFalse(result.success)
        self.assertIn("turn owner is no longer live", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:completed"))
        self.assertEqual(self.transport.activities, [])

    async def test_queued_clarify_preserves_stop_rotation_and_owner_guards(self):
        _, current = await self._stage_queued_foreground()
        linear = self.adapter._linear
        entry = self.adapter.gateway_runner.async_session_store.lookup_by_session_key.return_value
        for target, attr, value, error in (
            (linear, "status", "complete", "live issue ownership"),
            (linear, "state", "completed", "live issue ownership"),
            (linear, "delegate_id", "different-owner", "live issue ownership"),
            (entry, "session_id", "rotated-hermes-session", "session rotated"),
        ):
            with self.subTest(gate=attr), mock.patch.object(target, attr, value):
                result = await self._send_in_context(current, attr)
                self.assertFalse(result.success)
                self.assertIn(error, result.error or "")
                self.assertIsNone(self.adapter._ledger.get_outbox_item(f"activity:clarify:{attr[:10]}"))
        # Native Stop cancels the registry waiter; it cannot be reopened by cache expiry.
        result = await self._send_in_context(current, "stopped", stop=True)
        self.assertFalse(result.success)
        self.assertIn("registration is missing", result.error or "")
        self.adapter._ledger.enqueue_closure_activity(
            "closure-queued", "issue-221", "linear-session-221", "activity-closure-queued", "Closed.", {})
        result = await self._send_in_context(current, "closed")
        self.assertFalse(result.success)
        self.assertIn("terminal turn state", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:closed"))
        self.assertEqual(self.transport.activities, [])

    async def test_stopped_owner_cannot_register_a_late_callback(self):
        _, current = await self._stage_queued_foreground()
        self.adapter.record_completed_turn(
            chat_id="linear-session-221", hermes_session_id="hermes-session-221",
            turn_id="foreground-new", completed=False, failed=False, interrupted=True,
            turn_exit_reason="stopped")
        result = await self._send_in_context(current, "late-stop")
        self.assertFalse(result.success)
        self.assertIn("turn owner is no longer live", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:late-stop"))
        self.assertEqual(self.transport.activities, [])

    async def test_native_owner_is_rechecked_after_awaited_delivery_validation(self):
        _, current = await self._stage_queued_foreground()
        validate = self.adapter._validate_activity_target
        calls = 0

        async def complete_before_send(session_id):
            nonlocal calls
            calls += 1
            if calls == 2:  # Outbox validation, after durable enqueue.
                self._complete_foreground(current, "foreground-new")
            return await validate(session_id)

        with mock.patch.object(self.adapter, "_validate_activity_target", complete_before_send):
            result = await self._send_in_context(current, "late-drain")
        self.assertFalse(result.success)
        self.assertIn("suppressed", result.error or "")
        row = self.adapter._ledger.get_outbox_item("activity:clarify:late-drain")
        self.assertTrue(row["payload"]["clarify_suppressed"])
        self.assertEqual(row["payload"]["clarify_suppression_reason"], "waiter_unavailable")
        self.assertEqual(self.transport.activities, [])

    async def test_native_owner_is_required_and_must_be_an_immutable_pair(self):
        for owner in (None, ["hermes-session-221", "turn-221"], ("hermes-session-221",),
                      ("hermes-session-221", ""), ("hermes-session-221", 1)):
            with self.subTest(owner=owner):
                clarify_gateway.clear_session(self.key)
                clarify_gateway.register("invalid", self.key, "Target?", ["yes"], turn_owner=owner)
                result = await self.adapter.send_clarify(
                    "linear-session-221", "Target?", ["yes"], "invalid", self.key)
                self.assertFalse(result.success)
                self.assertIn("native turn owner is missing or malformed", result.error or "")
                self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:invalid"))
        self.assertEqual(self.transport.activities, [])

    async def test_preflight_timeout_is_not_an_ambiguous_delivery(self):
        clarify_gateway.register("preflight-timeout", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=client_mod.LinearAPIError("read timed out", retryable=True)
        )
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "preflight-timeout", self.key
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertFalse((result.raw_response or {}).get("ambiguous", False))
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:preflight-timeout"))
        self.assertEqual(self.transport.activities, [])

    async def test_preflight_recovers_with_one_question_and_rejects_auth_without_retry(self):
        clarify_gateway.register("retry-read", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        context = await self.adapter._linear.get_agent_turn_context("linear-session-221")
        reader = mock.AsyncMock(side_effect=[
            client_mod.LinearAPIError("read timed out", retryable=True), context
        ])
        self.adapter._linear.get_agent_turn_context = reader
        result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "retry-read", self.key)
        self.assertTrue(result.success)
        self.assertEqual(reader.await_count, 2)
        self.assertEqual(len(self.transport.activities), 1)
        reader.side_effect = client_mod.LinearAPIError("unauthorized", retryable=False)
        reader.reset_mock()
        result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "retry-read", self.key)
        self.assertFalse(result.success)
        self.assertEqual(reader.await_count, 1)
        self.assertEqual(len(self.transport.activities), 1)

    async def test_existing_queued_question_retains_ambiguity_on_read_timeout(self):
        clarify_gateway.register("prior-question", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        self.transport.fail = self.transport.retryable = True
        await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "prior-question", self.key)
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=client_mod.LinearAPIError("read timed out", retryable=True)
        )
        result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "prior-question", self.key)
        self.assertTrue(result.raw_response["ambiguous"])
        self.assertIsNotNone(self.adapter._ledger.get_outbox_item("activity:clarify:prior-question"))

    async def test_native_question_pauses_but_does_not_terminally_seal_progress(self):
        sid, turn = "linear-session-221", "turn-221"
        self.adapter.open_progress_turn(sid, turn)
        clarify_gateway.register("progress", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        self.assertTrue((await self.adapter.send_clarify(sid, "Target?", ["yes"], "progress", self.key)).success)
        self.assertFalse(self.adapter._progress_is_allowed(sid, turn))
        self.assertFalse(self.adapter._progress_chat_is_allowed(sid))
        progress = {"transient_progress": True, "transient_progress_key": turn, "transient_progress_kind": "semantic"}
        await self.adapter.send(sid, "hidden-while-waiting", metadata=progress)
        await self.adapter._drain_outbox_once()
        self.assertNotIn("hidden-while-waiting", [a[2] for a in self.transport.activities])
        self.assertEqual(await self.adapter._resolve_clarify_input(sid, "issue-221", self.payload(body="1")), "clarify_resolved")
        self.assertTrue(self.adapter._progress_is_allowed(sid, turn))
        self.assertTrue(self.adapter._progress_chat_is_allowed(sid))
        await self.adapter.send(sid, "review-running", metadata=progress)
        await self.adapter._drain_outbox_once()
        self.assertIn("review-running", [a[2] for a in self.transport.activities])
        self.adapter._enqueue_activity(sid, "error", "real failure", item_key="real-error")
        self.assertFalse(self.adapter._progress_is_allowed(sid, turn))
        self.assertFalse(self.adapter._progress_chat_is_allowed(sid))

    async def test_pending_question_suppresses_progress_without_becoming_terminal(self):
        sid, turn = "linear-session-221", "turn-221"
        self.adapter.open_progress_turn(sid, turn)
        self.transport.fail = self.transport.retryable = True
        clarify_gateway.register("pending", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        self.assertFalse((await self.adapter.send_clarify(sid, "Target?", ["yes"], "pending", self.key)).success)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:pending")["state"], "pending")
        self.assertFalse(self.adapter._progress_is_allowed(sid, turn))
        self.assertFalse(self.adapter._progress_chat_is_allowed(sid))
        self.assertTrue(self.adapter._ledger.progress_is_allowed(sid, turn))
        self.adapter._enqueue_activity(sid, "elicitation", "Non-native human decision", item_key="non-native")
        self.assertFalse(self.adapter._ledger.progress_is_allowed(sid, turn))

    async def test_verified_answer_enqueues_one_ephemeral_receipt(self):
        clarify_gateway.register("receipt", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        sent = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "receipt", self.key
        )
        self.assertTrue(sent.success)
        payload = self.payload(body="1")
        resolved = await self.adapter._resolve_clarify_input(
            "linear-session-221", "issue-221", payload
        )
        self.assertEqual(resolved, "clarify_resolved")
        row = self.adapter._ledger.get_outbox_item("activity:clarify-resolved:receipt")
        self.assertIsNotNone(row)
        self.assertEqual(row["operation"], "activity.transient.create")
        self.assertEqual(row["payload"]["activity_type"], "thought")
        self.assertTrue(row["payload"]["ephemeral"])
        self.assertIn("Yanıt alındı", row["payload"]["body"])
        self.assertNotIn("Target?", row["payload"]["body"])
        await self.adapter._resolve_clarify_input("linear-session-221", "issue-221", payload)
        await self.adapter._drain_outbox_once()
        receipts = [a for a in self.transport.activities if a[1] == "thought"]
        self.assertEqual(len(receipts), 1)

    async def test_rejected_or_closed_reply_never_enqueues_receipt(self):
        entry = await self.publish_question("rejected", ["yes"])
        # Exercise retained native-choice selection validation, before text mode.
        entry.awaiting_text = False
        for payload, expected in (
            (self.payload(actor="other"), "clarify_actor_mismatch"),
            (self.payload(body="7"), "clarify_rejected"),
        ):
            self.assertEqual(
                await self.adapter._resolve_clarify_input("linear-session-221", "issue-221", payload),
                expected,
            )
            self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:rejected"))
        self.adapter._linear.status = "complete"
        self.assertEqual(
            await self.adapter._resolve_clarify_input("linear-session-221", "issue-221", self.payload(body="1")),
            "clarify_fenced",
        )
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:rejected"))

    async def test_signed_actor_replay_and_wrong_actor(self):
        await self.publish_question("normal", ["staging", "prod"])
        first = await self.webhook(self.payload())
        self.assertEqual(json.loads(first.text)["status"], "clarify_resolved")
        self.assertEqual(clarify_gateway.wait_for_response("normal", .01), "prod")
        replay = await self.webhook(self.payload())
        self.assertEqual(json.loads(replay.text)["status"], "duplicate")
        await self.publish_question("wrong", ["yes"])
        wrong = await self.webhook(self.payload(actor="other-user", webhook="webhook-wrong"))
        self.assertEqual(json.loads(wrong.text)["status"], "clarify_actor_mismatch")

    async def test_webhook_command_requires_exact_active_human_before_core_dispatch(self):
        self.adapter.handle_message = mock.AsyncMock()
        response = await self.webhook(self.payload(actor="other-user", body="/approve"))
        self.assertEqual(
            json.loads(response.text)["status"],
            "native_command_requester_unavailable",
        )
        self.adapter.handle_message.assert_not_awaited()

        response = await self.webhook(self.payload(body="/approve", webhook="webhook-ok"))
        self.assertEqual(json.loads(response.text)["status"], "accepted")
        self.adapter.handle_message.assert_awaited_once()

    async def test_approval_command_reaches_core_command_lane_while_native_waits(self):
        """The adapter gate must not turn a core approval command into model input."""
        self.adapter._ledger.bind_issue_session("issue-221", "linear-session-221")
        self.adapter.gateway_runner = type(
            "Runner",
            (),
            {
                "async_session_store": type(
                    "Store", (),
                    {"get_or_create_session": lambda _self, _source, **_kwargs: None},
                )(),
            },
        )()

        async def get_or_create(_source, **_kwargs):
            return type(
                "Entry", (),
                {"session_id": "hermes-session", "session_key": self.key},
            )()

        self.adapter.gateway_runner.async_session_store.get_or_create_session = get_or_create
        async def goal_state_for_source(_source, *, session_id=None):
            return type("State", (), {"status": "active"})()

        self.adapter.gateway_runner.goal_state_for_source = goal_state_for_source
        dispatched = []
        model_turns = []

        async def core_command_dispatch(event):
            if event.text in {"/approve", "/deny"}:
                dispatched.append(event.text)
            else:
                model_turns.append(event.text)
            return None

        # The webhook owns admission; BasePlatformAdapter owns the active-session
        # inline command lane.  The test supplies only the core handler seam.
        self.adapter.set_message_handler(core_command_dispatch)
        active_guard = asyncio.Event()
        self.adapter._active_sessions[self.key] = active_guard
        self.adapter._session_tasks[self.key] = asyncio.current_task()
        self.adapter._linear.status = "awaitingInput"

        for command, webhook_id in (("/approve", "webhook-approve"), ("/deny", "webhook-deny")):
            response = await self.webhook(self.payload(body=command, webhook=webhook_id))
            self.assertEqual(json.loads(response.text)["status"], "accepted")

        self.assertEqual(dispatched, ["/approve", "/deny"])
        self.assertEqual(dispatched.count("/approve"), 1)
        self.assertEqual(dispatched.count("/deny"), 1)
        self.assertEqual(model_turns, [])
        self.assertIs(self.adapter._active_sessions[self.key], active_guard)
        self.assertEqual(
            self.adapter._active_turn_events["linear-session-221"].source.user_id,
            "user-221",
        )

        for actor, webhook_id in (("other-user", "webhook-wrong"), ("", "webhook-missing")):
            response = await self.webhook(
                self.payload(actor=actor, body="/approve", webhook=webhook_id)
            )
            self.assertEqual(
                json.loads(response.text)["status"],
                "native_command_requester_unavailable",
            )
        self.assertEqual(dispatched, ["/approve", "/deny"])

        self.adapter._linear.state = "completed"
        response = await self.webhook(self.payload(body="/approve", webhook="webhook-terminal"))
        self.assertEqual(json.loads(response.text)["status"], "accepted")
        self.adapter._linear.state = "started"
        self.adapter._linear.delegate_id = "different-delegate"
        response = await self.webhook(self.payload(body="/deny", webhook="webhook-delegate"))
        self.assertEqual(json.loads(response.text)["status"], "accepted")
        self.assertEqual(dispatched, ["/approve", "/deny"])

    async def test_approval_command_does_not_bypass_terminal_or_delegate_fences(self):
        self.adapter._ledger.bind_issue_session("issue-221", "linear-session-221")
        event = self.adapter._message_event(
            self.payload(body="/approve"), "delivery-approval", "webhook-approval"
        )
        self.assertTrue(self.adapter._trusted_native_command_requester(event))
        async def get_or_create(_self, _source, **_kwargs):
            return type("Entry", (), {"session_id": "hermes-session", "session_key": self.key})()
        async def goal_state(_self, _source, *, session_id=None):
            return type("State", (), {"status": "active"})()
        self.adapter.gateway_runner = type(
            "Runner", (),
            {"async_session_store": type("Store", (), {"get_or_create_session": get_or_create})(),
             "goal_state_for_source": goal_state},
        )()

        self.adapter._linear.state = "completed"
        self.assertTrue(await self.adapter._prepare_bound_linear_ingress(event))

        self.adapter._linear.state = "started"
        self.adapter._linear.actor_id = "different-delegate"
        self.assertTrue(await self.adapter._prepare_bound_linear_ingress(event))

    async def test_native_command_binding_rejects_unavailable_and_direct_identities(self):
        self.assertEqual(adapter_mod._actor({})[0], "")

        incoming = self.adapter._message_event(
            self.payload(body="/approve"), "delivery-missing", "webhook-missing"
        )
        incoming.source.user_id = ""
        self.assertFalse(self.adapter._trusted_native_command_requester(incoming))

        direct_active = self.adapter._active_turn_events["linear-session-221"]
        direct_active.metadata["linear_direct_activation"] = True
        incoming.source.user_id = "user-221"
        self.assertFalse(self.adapter._trusted_native_command_requester(incoming))

        incoming.metadata["linear_direct_activation"] = True
        direct_active.metadata.pop("linear_direct_activation")
        self.assertFalse(self.adapter._trusted_native_command_requester(incoming))

    async def test_slash_commands_pass_through(self):
        clarify_gateway.register("slash", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        for command in ("/approve", "/deny", "/stop", "/anything"):
            self.assertIsNone(await self.adapter._resolve_clarify_input(
                "linear-session-221", "issue-221", {"agentActivity": {"body": command}}))

    async def test_no_pending_reply_does_not_become_clarify_actor_mismatch(self):
        result = await self.adapter._resolve_clarify_input(
            "linear-session-221", "issue-221", self.payload(actor="other-user")
        )
        self.assertIsNone(result)

    async def test_delivered_question_remains_fenced_after_registry_timeout(self):
        clarify_gateway.register("timeout", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "timeout", self.key
        )
        self.assertTrue(result.success)
        self.assertIsNone(clarify_gateway.wait_for_response("timeout", 0.001))

        event = self.adapter._active_turn_events["linear-session-221"]
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        self.assertIs(self.adapter._active_turn_events["linear-session-221"], event)

    async def test_closure_suppression_is_not_published_clarify_delivery(self):
        self.transport.fail = True
        self.transport.retryable = True
        clarify_gateway.register("closure", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "closure", self.key
        )
        self.assertFalse(result.success)
        self.adapter._ledger.enqueue_closure_activity(
            "closure-221", "issue-221", "linear-session-221", "activity-closure-221",
            "Closed.", {}, now=time.time()
        )
        item = self.adapter._ledger.get_outbox_item("activity:clarify:closure")
        self.assertEqual(item["state"], "delivered")
        self.assertTrue(item["payload"]["clarify_suppressed"])
        self.assertFalse(self.adapter._has_delivered_native_clarify(
            self.adapter._active_turn_events["linear-session-221"]
        ))

    async def test_terminal_session_before_drain_suppresses_question_without_success(self):
        clarify_gateway.register("terminal", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        reads = 0

        async def delivery_context(session_id):
            nonlocal reads
            reads += 1
            return {"id": session_id, "app_user_id": self.adapter._linear.actor_id,
                    "status": "active" if reads == 1 else "complete"}

        self.adapter._linear.get_agent_session_delivery_context = delivery_context
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "terminal", self.key
        )
        item = self.adapter._ledger.get_outbox_item("activity:clarify:terminal")
        self.assertFalse(result.success)
        self.assertTrue(item["payload"]["clarify_suppressed"])
        self.assertEqual(self.transport.activities, [])
        self.assertFalse(self.adapter._has_delivered_native_clarify(
            self.adapter._active_turn_events["linear-session-221"]
        ))

    async def test_timeout_fence_does_not_swallow_failure_in_real_completion_callback(self):
        clarify_gateway.register("timeout-failure", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "timeout-failure", self.key
        )
        event = self.adapter._active_turn_events["linear-session-221"]
        clarify_gateway.clear_session(self.key)
        self.adapter._pending_turn_deliveries["linear-session-221"] = (
            event, "partial response", {"session_id": "hermes-session", "completed": True}
        )
        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
        self.assertNotIn("linear-session-221", self.adapter._pending_turn_deliveries)
        self.assertTrue(self.transport.activities)
        self.assertNotEqual(self.transport.activities[-1][1], "elicitation")

    async def test_clarify_rechecks_turn_and_waiter_after_context_await(self):
        clarify_gateway.register("q1", self.key, "Q1", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.adapter._linear.get_agent_turn_context

        async def delayed_context(session_id):
            entered.set()
            await release.wait()
            return await original(session_id)

        self.adapter._linear.get_agent_turn_context = delayed_context
        task = asyncio.create_task(
            self.adapter.send_clarify("linear-session-221", "Q1", ["yes"], "q1", self.key)
        )
        await entered.wait()
        replacement = MessageEvent(
            text="turn-2", message_type=MessageType.TEXT, source=source(), metadata={
                "linear_agent_session_id": "linear-session-221",
                "linear_issue_id": "issue-221", "gateway_session_key": self.key,
                "linear_delivery_key": "turn-2",
            }
        )
        self.adapter._active_turn_events["linear-session-221"] = replacement
        clarify_gateway.clear_session(self.key)
        clarify_gateway.register("q2", self.key, "Q2", ["no"], turn_owner=("hermes-session-221", "turn-221"))
        release.set()
        result = await task
        self.assertFalse(result.success)
        self.assertIn("replaced", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:q1"))

    async def test_restart_resumed_turn_without_delivery_key_can_clarify(self):
        event = self.adapter._active_turn_events["linear-session-221"]
        event.metadata.pop("linear_delivery_key")
        event.message_id = None
        await self.adapter.on_processing_start(event)
        clarify_gateway.register("restart-live", self.key, "Restart question", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Restart question", ["yes"], "restart-live", self.key
        )
        self.assertTrue(result.success, result.error)
        item = self.adapter._ledger.get_outbox_item("activity:clarify:restart-live")
        self.assertTrue(item["payload"]["clarify_turn_key"])
        self.assertNotIn("linear_delivery_key", event.metadata)
        self.assertEqual(len(self.transport.activities), 1)
        response = await self.webhook(self.payload(body="1"))
        self.assertEqual(json.loads(response.text)["status"], "clarify_resolved")
        self.assertEqual(clarify_gateway.wait_for_response("restart-live", .01), "yes")

    async def test_restart_replacement_cannot_deliver_previous_turn_question(self):
        event = self.adapter._active_turn_events["linear-session-221"]
        event.metadata.pop("linear_delivery_key")
        event.message_id = None
        await self.adapter.on_processing_start(event)
        self.transport.fail = True
        self.transport.retryable = True
        clarify_gateway.register("restart-old", self.key, "Old question", None, turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Old question", None, "restart-old", self.key
        )
        self.assertFalse(result.success)
        old_key = event.metadata["linear_clarify_turn_key"]
        replacement = MessageEvent(
            text="resume", message_type=MessageType.TEXT, source=source(),
            metadata=dict(event.metadata),
        )
        await self.adapter.on_processing_start(replacement)
        self.assertNotEqual(replacement.metadata["linear_clarify_turn_key"], old_key)
        self.transport.fail = False
        self.adapter._ledger.reschedule_outbox(
            "activity:clarify:restart-old", "test", 0, now=time.time() - 1
        )
        await self.adapter._drain_outbox_once()
        item = self.adapter._ledger.get_outbox_item("activity:clarify:restart-old")
        self.assertTrue(item["payload"]["clarify_suppressed"])
        self.assertEqual(self.transport.activities, [])

    async def test_pending_question_is_suppressed_after_waiter_and_turn_restart_loss(self):
        self.transport.fail = True
        self.transport.retryable = True
        clarify_gateway.register("orphan", self.key, "Q orphan", None, turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Q orphan", None, "orphan", self.key
        )
        self.assertFalse(result.success)
        self.assertEqual(
            self.adapter._ledger.get_outbox_item("activity:clarify:orphan")["state"],
            "pending",
        )
        clarify_gateway.clear_session(self.key)
        self.adapter._active_turn_events.clear()
        self.transport.fail = False
        self.adapter._ledger.reschedule_outbox(
            "activity:clarify:orphan", "test", 0, now=time.time() - 1
        )
        await self.adapter._drain_outbox_once()
        item = self.adapter._ledger.get_outbox_item("activity:clarify:orphan")
        self.assertEqual(item["state"], "delivered")
        self.assertTrue(item["payload"]["clarify_suppressed"])
        self.assertEqual(item["payload"]["clarify_suppression_reason"], "waiter_unavailable")
        self.assertEqual(self.transport.activities, [])

    async def test_clarify_rechecks_liveness_after_awaited_delivery_validation(self):
        clarify_gateway.register("late", self.key, "Q late", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        original = self.adapter._validate_activity_target

        async def delayed_validation(session_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await release.wait()
            return await original(session_id)

        self.adapter._validate_activity_target = delayed_validation
        task = asyncio.create_task(
            self.adapter.send_clarify("linear-session-221", "Q late", ["yes"], "late", self.key)
        )
        await entered.wait()
        replacement = MessageEvent(
            text="turn-2", message_type=MessageType.TEXT, source=source(), metadata={
                "linear_agent_session_id": "linear-session-221",
                "linear_issue_id": "issue-221", "gateway_session_key": self.key,
                "linear_delivery_key": "turn-2",
            }
        )
        self.adapter._active_turn_events["linear-session-221"] = replacement
        clarify_gateway.clear_session(self.key)
        release.set()
        result = await task

        self.assertFalse(result.success)
        self.assertIn("suppressed", result.error or "")
        item = self.adapter._ledger.get_outbox_item("activity:clarify:late")
        self.assertTrue(item["payload"]["clarify_suppressed"])
        self.assertEqual(self.transport.activities, [])

    async def test_resolved_question_does_not_remain_fenced(self):
        clarify_gateway.register("resolved", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "resolved", self.key
        )
        self.assertTrue(result.success)
        self.assertEqual(
            await self.adapter._resolve_clarify_input(
                "linear-session-221", "issue-221", self.payload(body="1")
            ),
            "clarify_resolved",
        )
        event = self.adapter._active_turn_events["linear-session-221"]
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        self.assertNotIn("linear-session-221", self.adapter._active_turn_events)

    async def test_closed_issue_is_rejected(self):
        for state in ("completed", "canceled"):
            self.adapter._linear.state = state
            clarify_gateway.register(state, self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
            result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], state, self.key)
            self.assertFalse(result.success)
            clarify_gateway.clear_session("clarify-test-session")

    async def test_custom_choice_marks_awaiting_text_after_real_delivery(self):
        entry = clarify_gateway.register("custom", self.key, "Target?", ["yes", "no"], turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes", "no"], "custom", self.key)
        self.assertTrue(result.success)
        self.assertTrue(entry.awaiting_text)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:custom")["state"], "delivered")

    async def test_question_fence_does_not_swallow_real_error(self):
        entry = clarify_gateway.register("fence", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "fence", self.key)
        await self.adapter.on_processing_complete(self.adapter._active_turn_events["linear-session-221"], ProcessingOutcome.FAILURE)
        self.assertTrue(any(kind == "error" for _, kind, _ in self.transport.activities))
        self.assertFalse(entry.event.is_set())

    async def test_delivered_question_fences_generic_success_completion(self):
        clarify_gateway.register("done", self.key, "Target?", ["yes"], turn_owner=("hermes-session-221", "turn-221"))
        await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "done", self.key)
        event = self.adapter._active_turn_events["linear-session-221"]
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        self.assertEqual(self.transport.activities, [("linear-session-221", "elicitation", self.transport.activities[0][2])])
        self.assertIs(self.adapter._active_turn_events["linear-session-221"], event)

    async def test_dead_delivery_is_failure_not_delivered(self):
        self.transport.fail = True
        clarify_gateway.register("dead", self.key, "Target?", None, turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify("linear-session-221", "Target?", None, "dead", self.key)
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:dead")["state"], "dead")

    async def test_pending_delivery_is_ambiguous_not_success(self):
        self.adapter.transport = Transport(fail=True, retryable=True)
        self.adapter._linear.transport = self.adapter.transport
        clarify_gateway.register("pending", self.key, "Target?", None, turn_owner=("hermes-session-221", "turn-221"))
        result = await self.adapter.send_clarify("linear-session-221", "Target?", None, "pending", self.key)
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:pending")["state"], "pending")


if __name__ == "__main__":
    unittest.main()
