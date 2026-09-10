from __future__ import annotations

import hashlib
import hmac
import importlib.util
import asyncio
import json
import sys
import tempfile
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

    async def create_activity(self, session_id, activity_type, body, *, activity_id, ephemeral=False):
        if self.fail:
            raise client_mod.LinearAPIError("vendor rejected activity", retryable=self.retryable)
        self.activities.append((session_id, activity_type, body))
        return activity_id


class Linear:
    organization_id = "org-221"
    actor_id = "app-221"
    actor_name = "Native app"

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
                "gateway_session_key": self.key, "linear_delivery_key": "turn-221"})}

    def tearDown(self):
        clarify_gateway.clear_session(self.key)
        self.adapter._ledger.close()
        self.tmp.cleanup()

    def payload(self, actor="user-221", body="2", webhook="webhook-221"):
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

    async def test_signed_actor_replay_and_wrong_actor(self):
        clarify_gateway.register("normal", self.key, "Target?", ["staging", "prod"])
        first = await self.webhook(self.payload())
        self.assertEqual(json.loads(first.text)["status"], "clarify_resolved")
        self.assertEqual(clarify_gateway.wait_for_response("normal", .01), "prod")
        replay = await self.webhook(self.payload())
        self.assertEqual(json.loads(replay.text)["status"], "duplicate")
        clarify_gateway.register("wrong", self.key, "Target?", ["yes"])
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
        clarify_gateway.register("slash", self.key, "Target?", ["yes"])
        for command in ("/approve", "/deny", "/stop", "/anything"):
            self.assertIsNone(await self.adapter._resolve_clarify_input(
                "linear-session-221", "issue-221", {"agentActivity": {"body": command}}))

    async def test_no_pending_reply_does_not_become_clarify_actor_mismatch(self):
        result = await self.adapter._resolve_clarify_input(
            "linear-session-221", "issue-221", self.payload(actor="other-user")
        )
        self.assertIsNone(result)

    async def test_delivered_question_remains_fenced_after_registry_timeout(self):
        clarify_gateway.register("timeout", self.key, "Target?", ["yes"])
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
        clarify_gateway.register("closure", self.key, "Target?", ["yes"])
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

    async def test_timeout_fence_does_not_swallow_failure_in_real_completion_callback(self):
        clarify_gateway.register("timeout-failure", self.key, "Target?", ["yes"])
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
        clarify_gateway.register("q1", self.key, "Q1", ["yes"])
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
        clarify_gateway.register("q2", self.key, "Q2", ["no"])
        release.set()
        result = await task
        self.assertFalse(result.success)
        self.assertIn("replaced", result.error or "")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:q1"))

    async def test_pending_question_is_suppressed_after_waiter_and_turn_restart_loss(self):
        self.transport.fail = True
        self.transport.retryable = True
        clarify_gateway.register("orphan", self.key, "Q orphan", None)
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
        clarify_gateway.register("late", self.key, "Q late", ["yes"])
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
            await original(session_id)

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
        clarify_gateway.register("resolved", self.key, "Target?", ["yes"])
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
            clarify_gateway.register(state, self.key, "Target?", ["yes"])
            result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], state, self.key)
            self.assertFalse(result.success)
            clarify_gateway.clear_session("clarify-test-session")

    async def test_custom_choice_marks_awaiting_text_after_real_delivery(self):
        entry = clarify_gateway.register("custom", self.key, "Target?", ["yes", "no"])
        result = await self.adapter.send_clarify("linear-session-221", "Target?", ["yes", "no"], "custom", self.key)
        self.assertTrue(result.success)
        self.assertTrue(entry.awaiting_text)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:custom")["state"], "delivered")

    async def test_question_fence_does_not_swallow_real_error(self):
        entry = clarify_gateway.register("fence", self.key, "Target?", ["yes"])
        await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "fence", self.key)
        await self.adapter.on_processing_complete(self.adapter._active_turn_events["linear-session-221"], ProcessingOutcome.FAILURE)
        self.assertTrue(any(kind == "error" for _, kind, _ in self.transport.activities))
        self.assertFalse(entry.event.is_set())

    async def test_delivered_question_fences_generic_success_completion(self):
        clarify_gateway.register("done", self.key, "Target?", ["yes"])
        await self.adapter.send_clarify("linear-session-221", "Target?", ["yes"], "done", self.key)
        event = self.adapter._active_turn_events["linear-session-221"]
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        self.assertEqual(self.transport.activities, [("linear-session-221", "elicitation", self.transport.activities[0][2])])
        self.assertIs(self.adapter._active_turn_events["linear-session-221"], event)

    async def test_dead_delivery_is_failure_not_delivered(self):
        self.transport.fail = True
        clarify_gateway.register("dead", self.key, "Target?", None)
        result = await self.adapter.send_clarify("linear-session-221", "Target?", None, "dead", self.key)
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:dead")["state"], "dead")

    async def test_pending_delivery_is_ambiguous_not_success(self):
        self.adapter.transport = Transport(fail=True, retryable=True)
        self.adapter._linear.transport = self.adapter.transport
        clarify_gateway.register("pending", self.key, "Target?", None)
        result = await self.adapter.send_clarify("linear-session-221", "Target?", None, "pending", self.key)
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(self.adapter._ledger.get_outbox_item("activity:clarify:pending")["state"], "pending")


if __name__ == "__main__":
    unittest.main()
