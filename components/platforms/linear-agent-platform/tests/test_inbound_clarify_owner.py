"""Signed concurrent ingress must never answer an unpublished/stale native waiter."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_native_clarify as native
from gateway.run_inbound import GatewayInboundMixin
from tools import clarify_gateway


class InboundClarifyOwnerTests(unittest.IsolatedAsyncioTestCase):
    setUp = native.NativeClarifyTests.setUp
    tearDown = native.NativeClarifyTests.tearDown
    payload = native.NativeClarifyTests.payload
    _foreground_context = native.NativeClarifyTests._foreground_context
    _complete_foreground = native.NativeClarifyTests._complete_foreground
    _stage_queued_foreground = native.NativeClarifyTests._stage_queued_foreground

    async def asyncSetUp(self):
        self.adapter._signing_secrets = ("secret-221",)
        self.adapter._ledger.bind_issue_session("issue-221", "linear-session-221")
        store = self.adapter.gateway_runner.async_session_store
        store.get_or_create_session = mock.AsyncMock(return_value=store.lookup_by_session_key.return_value)
        self.adapter.gateway_runner._profile_name_for_source = lambda *a: None
        self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(
            return_value=SimpleNamespace(status="active"))
        app = web.Application()
        app.router.add_post("/linear/webhook", self.adapter._handle_webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.normal = []
        self.admitted = asyncio.Event()
        self.finish = asyncio.Event()
        # Exercise the actual core FIFO interceptor on fallback, not just a mock
        # returning success: a new waiter can register during normal preemption.
        runner = SimpleNamespace(
            _hm_update_prompt_reply=lambda *a: None,
            _hm_slash_confirm_reply=mock.AsyncMock(return_value=None),
            _pending_event_audio_paths=lambda *a: [],
            _prepare_clarify_reply_text=mock.AsyncMock(side_effect=lambda event: event.text),
            _adapter_for_source=lambda *a: None,
        )
        runner._hm_clarify_reply = lambda *args: GatewayInboundMixin._hm_clarify_reply(runner, *args)

        async def admit(event):
            intercepted = await GatewayInboundMixin._hm_pending_reply_intercepts(
                runner, event, event.source, self.key)
            if intercepted is None:
                self.normal.append(event)
            self.admitted.set()
            await asyncio.wait_for(self.finish.wait(), 5)

        self.adapter.set_message_handler(admit)

    async def asyncTearDown(self):
        await self.client.close()
        for key in list(self.adapter._session_tasks):
            await self.adapter.cancel_session_processing(key)

    async def post(self, payload):
        raw = json.dumps(payload).encode()
        sig = hmac.new(b"secret-221", raw, hashlib.sha256).hexdigest()
        response = await asyncio.wait_for(self.client.post(
            "/linear/webhook", data=raw, headers={"Linear-Signature": sig}), 5)
        return response.status, (await response.json())["status"]

    def done(self, payload):
        raw = json.dumps(payload).encode()
        return self.adapter._ledger.delivery_is_done(native.adapter_mod._delivery_key(payload, raw))

    async def test_unpublished_old_callback_falls_back_without_answering_either_waiter(self):
        old, _ = await self._stage_queued_foreground()
        entered, release = asyncio.Event(), asyncio.Event()
        validate = self.adapter._validate_activity_target
        pending = None

        async def blocked_validation(session_id):
            nonlocal pending
            pending = clarify_gateway.get_pending_for_session(self.key, include_choice_prompts=True)
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await validate(session_id)

        with mock.patch.object(self.adapter, "_validate_activity_target", blocked_validation), \
                mock.patch("uuid.uuid4", return_value=SimpleNamespace(hex="old-http")):
            worker = asyncio.create_task(asyncio.to_thread(old.context.run, old.execute))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(pending.turn_owner, ("hermes-session-221", "foreground-old"))
                successor = clarify_gateway.register("successor", self.key, "New?", ["yes"],
                    turn_owner=("hermes-session-221", "foreground-new"))
                payload = self.payload(body="1", webhook="unpublished-http")
                self.assertEqual(await self.post(payload), (200, "accepted"))
                await asyncio.wait_for(self.admitted.wait(), 5)
                self.assertNotEqual(pending.response, "yes")
                self.assertFalse(successor.event.is_set())
                self.assertEqual([native.adapter_mod._activity_body(event.raw_message) for event in self.normal], ["1"])
                self.assertTrue(self.done(payload))
                self.assertEqual(await self.post(payload), (200, "duplicate"))
            finally:
                release.set()
                result = json.loads(await asyncio.wait_for(worker, 5))
        self.assertEqual(result["user_response"], "[clarify prompt could not be delivered]")
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify:old-http"))
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:old-http"))
        await self.adapter._drain_outbox_once()
        self.assertEqual([a[1] for a in self.transport.activities], ["thought"])
        self.assertIn("accepted the task", self.transport.activities[0][2])
        self.assertFalse(any("Yanıt alındı" in a[2] for a in self.transport.activities))

    async def test_published_reply_revalidates_owner_after_read_and_pins_retries(self):
        entry = clarify_gateway.register("published", self.key, "Target?", ["yes"],
            turn_owner=("hermes-session-221", "turn-221"))
        sent = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "published", self.key)
        self.assertTrue(sent.success, sent.error)
        entered, release = asyncio.Event(), asyncio.Event()
        read = self.adapter._linear.get_agent_turn_context

        async def blocked_read(session_id):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await read(session_id)

        payload = self.payload(body="1", webhook="published-http")
        with mock.patch.object(self.adapter._linear, "get_agent_turn_context", blocked_read):
            post = asyncio.create_task(self.post(payload))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.adapter.open_progress_turn("linear-session-221", "successor-turn")
            finally:
                release.set()
                response = await asyncio.wait_for(post, 5)
        self.assertEqual(response, (503, "clarify_unavailable"))
        self.assertFalse(self.done(payload))
        self.assertFalse(entry.event.is_set())
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:published"))
        self.assertEqual(self.normal, [])
        # Retry the exact signed event after a restart of durable state and FIFO
        # replacement. It may become a normal prompt, never the new question's answer.
        clarify_gateway.cancel("published")
        successor = clarify_gateway.register("successor", self.key, "New?", ["yes"],
            turn_owner=("hermes-session-221", "successor-turn"))
        self.assertTrue((await self.adapter.send_clarify(
            "linear-session-221", "New?", ["yes"], "successor", self.key)).success)
        ledger_path = str(self.adapter._ledger.path)
        self.adapter._ledger.close()
        self.adapter._ledger = native.ledger_mod.DeliveryLedger(ledger_path, startup_recovery=False)
        self.assertEqual(await self.post(payload), (200, "accepted"))
        await asyncio.wait_for(self.admitted.wait(), 5)
        self.assertFalse(successor.event.is_set())
        self.assertEqual([native.adapter_mod._activity_body(event.raw_message) for event in self.normal], ["1"])
        self.assertTrue(self.done(payload))
        self.assertEqual(await self.post(payload), (200, "duplicate"))
        self.assertEqual([a[1] for a in self.transport.activities if a[1] == "elicitation"],
                         ["elicitation", "elicitation"])
        self.assertFalse(any("Yanıt alındı" in a[2] for a in self.transport.activities))

    async def _unpublished_question_does_not_consume_prompt(self, stage):
        cid = "not-published-" + stage
        entry = clarify_gateway.register(cid, self.key, "Not visible yet?", None,
            turn_owner=("hermes-session-221", "turn-221"))
        entered, release = asyncio.Event(), asyncio.Event()
        drain = self.adapter._drain_outbox_once
        validate = self.adapter._validate_activity_target
        calls = 0

        async def paused_drain():
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await drain()

        async def paused_validation(sid):
            nonlocal calls
            calls += 1
            if calls == 2:  # Claimed in_flight, still before vendor create.
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return await validate(sid)

        target, blocker = ("_drain_outbox_once", paused_drain) if stage == "pending" else (
            "_validate_activity_target", paused_validation)
        with mock.patch.object(self.adapter, target, blocker):
            send = asyncio.create_task(self.adapter.send_clarify(
                "linear-session-221", "Not visible yet?", None, cid, self.key))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                item = self.adapter._ledger.get_outbox_item("activity:clarify:" + cid)
                self.assertEqual(item["state"], stage)
                self.assertEqual(self.transport.activities, [])
                payload = self.payload(body="Please summarize the issue instead", webhook=cid)
                self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
                self.assertFalse(self.done(payload))
                self.assertIsNone(entry.response)
                self.assertEqual(self.normal, [])
                self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:" + cid))
            finally:
                release.set()
                outbound = await asyncio.wait_for(send, 5)
        self.assertTrue(outbound.success, outbound.error)
        # A later ACK must not retroactively turn the older human prompt into
        # this question's answer, even after durable state is reopened.
        ledger_path = str(self.adapter._ledger.path)
        self.adapter._ledger.close()
        self.adapter._ledger = native.ledger_mod.DeliveryLedger(ledger_path, startup_recovery=False)
        self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
        self.assertIsNone(entry.response)
        self.assertFalse(self.done(payload))
        clarify_gateway.cancel(cid)
        successor = clarify_gateway.register("successor", self.key, "New?", None,
            turn_owner=("hermes-session-221", "turn-221"))
        self.assertTrue((await self.adapter.send_clarify(
            "linear-session-221", "New?", None, "successor", self.key)).success)
        self.assertEqual(await self.post(payload), (200, "accepted"))
        await asyncio.wait_for(self.admitted.wait(), 5)
        self.assertFalse(successor.event.is_set())
        self.assertEqual([native.adapter_mod._activity_body(event.raw_message) for event in self.normal],
                         ["Please summarize the issue instead"])
        self.assertFalse(self.normal[0].allow_gateway_control)
        self.assertTrue(self.done(payload))
        self.assertEqual(await self.post(payload), (200, "duplicate"))
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:" + cid))
        self.assertFalse(any("Yanıt alındı" in a[2] for a in self.transport.activities))

    async def test_pending_before_first_vendor_call_does_not_consume_prompt(self):
        await self._unpublished_question_does_not_consume_prompt("pending")

    async def test_in_flight_before_first_vendor_call_does_not_consume_prompt(self):
        await self._unpublished_question_does_not_consume_prompt("in_flight")

    async def test_signed_reply_can_win_vendor_ack_without_losing_resolution(self):
        entry = clarify_gateway.register("fast", self.key, "Target?", ["yes"],
            turn_owner=("hermes-session-221", "turn-221"))
        entered, release = asyncio.Event(), asyncio.Event()
        create = self.transport.create_activity

        async def delayed_ack(*args, **kwargs):
            activity_id = await create(*args, **kwargs)
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return activity_id

        with mock.patch.object(self.transport, "create_activity", delayed_ack):
            send = asyncio.create_task(self.adapter.send_clarify(
                "linear-session-221", "Target?", ["yes"], "fast", self.key))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                payload = self.payload(body="1", webhook="fast-reply")
                item = self.adapter._ledger.get_outbox_item("activity:clarify:fast")
                self.assertEqual(item["state"], "in_flight")
                self.assertIn(item["payload"]["activity_id"], self.transport.evidence)
                with mock.patch.object(self.adapter._linear, "graphql", mock.AsyncMock(
                    side_effect=native.client_mod.LinearAPIError("read timeout", retryable=True))):
                    self.assertEqual(await self.post(payload), (503, "unavailable"))
                self.assertFalse(self.done(payload))
                self.assertIsNone(entry.response)
                self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:fast"))
                self.assertEqual(await self.post(payload), (200, "clarify_resolved"))
                self.assertEqual(entry.response, "yes")
                self.assertTrue(self.done(payload))
                self.assertEqual(await self.post(payload), (200, "duplicate"))
            finally:
                release.set()
                result = await asyncio.wait_for(send, 5)
        self.assertTrue(result.success, result.error)
        self.assertTrue(self.adapter._ledger.get_outbox_item("activity:clarify:fast")["payload"]["clarify_resolved"])
        await self.adapter._drain_outbox_once()
        self.assertEqual([a[1] for a in self.transport.activities], ["elicitation", "thought"])
        self.assertEqual(self.normal, [])

    async def test_failed_normal_admission_retries_without_rebinding_or_false_activity(self):
        payload = self.payload(body="continue normally", webhook="normal-retry")
        with mock.patch.object(self.adapter, "handle_message", mock.AsyncMock()):
            self.assertEqual(await self.post(payload), (503, "unavailable"))
        self.assertFalse(self.done(payload))
        self.assertEqual(self.transport.activities, [])
        successor = clarify_gateway.register("successor", self.key, "New?", None,
            turn_owner=("hermes-session-221", "turn-221"))
        self.assertTrue((await self.adapter.send_clarify(
            "linear-session-221", "New?", None, "successor", self.key)).success)
        self.assertEqual(await self.post(payload), (200, "accepted"))
        await asyncio.wait_for(self.admitted.wait(), 5)
        self.assertFalse(successor.event.is_set())
        self.assertTrue(self.done(payload))
        self.assertEqual(len(self.normal), 1)

    async def test_pinned_normal_prompt_repairs_receipt_without_reexecution_or_rebinding(self):
        import time

        payload = self.payload(body="continue normally", webhook="normal-receipt")
        key = native.adapter_mod._delivery_key(payload, json.dumps(payload).encode())
        with mock.patch.object(self.adapter, "handle_message", mock.AsyncMock()):
            self.assertEqual(await self.post(payload), (503, "unavailable"))
        successor = clarify_gateway.register("successor", self.key, "New?", None,
            turn_owner=("hermes-session-221", "turn-221"))
        self.assertTrue((await self.adapter.send_clarify(
            "linear-session-221", "New?", None, "successor", self.key)).success)
        ledger = self.adapter._ledger
        ledger._db.execute(
            "CREATE TEMP TRIGGER fail_thought BEFORE INSERT ON outbox "
            "WHEN NEW.id LIKE 'activity:thought:%' "
            "BEGIN SELECT RAISE(FAIL, 'injected thought failure'); END"
        )
        with mock.patch.object(self.adapter, "handle_message", wraps=self.adapter.handle_message) as ingress:
            self.assertEqual(await self.post(payload), (503, "unavailable"))
            await asyncio.wait_for(self.admitted.wait(), 5)
            self.assertTrue(self.done(payload))
            self.assertEqual(len(self.normal), 1)
            self.assertFalse(self.normal[0].allow_gateway_control)
            self.assertFalse(successor.event.is_set())
            self.assertEqual(ledger.bind_clarify_reply(key, "successor"), "")
            self.assertEqual(ledger.pending_acceptance_thoughts(key), [{
                "agent_session_id": "linear-session-221", "issue_id": "issue-221",
                "include_queued": False, "delivery_key": key,
            }])
            self.assertIsNone(ledger.get_outbox_item(f"activity:thought:{key}"))
            self.assertEqual(await self.post(payload), (503, "unavailable"))
            path = str(ledger.path)
            ledger.close()
            self.adapter._ledger = ledger = native.ledger_mod.DeliveryLedger(path, startup_recovery=False)
            ledger.prune(now=int(time.time()) + ledger.retention_seconds + 1)
            self.assertTrue(self.done(payload))
            self.assertEqual(await self.post(payload), (200, "duplicate"))
            await self.adapter._wait_for_thought("linear-session-221")
            self.assertEqual(ingress.await_count, 1)
            self.assertEqual(await self.post(payload), (200, "duplicate"))
        thought = ledger.get_outbox_item(f"activity:thought:{key}")
        self.assertEqual(thought["state"], "delivered")
        self.assertEqual(thought["payload"]["acceptance_delivery_key"], key)
        self.assertFalse(ledger.pending_acceptance_thoughts(key))
        self.assertEqual([a[1] for a in self.transport.activities], ["elicitation", "thought"])
        self.assertFalse(successor.event.is_set())
        self.assertFalse(any("Yanıt alındı" in a[2] for a in self.transport.activities))

    async def test_real_stop_interrupts_processing_during_signed_reply_chronology_read(self):
        self.assertEqual(await self.post(self.payload(body="start", webhook="start-for-stop")),
                         (200, "accepted"))
        await asyncio.wait_for(self.admitted.wait(), 5)
        await self.adapter._wait_for_thought("linear-session-221")
        task, = self.adapter._session_tasks.values()
        owner = self.adapter._current_progress_turn_key("linear-session-221")
        entry = clarify_gateway.register("stop-read", self.key, "Target?", ["yes"],
            turn_owner=("hermes-session-221", owner))
        self.assertTrue((await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], "stop-read", self.key)).success)
        payload = self.payload(body="1", webhook="stop-read-reply")
        entered, release = asyncio.Event(), asyncio.Event()
        graphql = self.adapter._linear.graphql

        async def blocked_chronology(*args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await graphql(*args, **kwargs)

        with mock.patch.object(self.adapter._linear, "graphql", blocked_chronology):
            reply = asyncio.create_task(self.post(payload))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                stop = self.payload(body="stop", webhook="stop-during-chronology")
                stop["agentActivity"]["signal"] = "stop"
                self.assertEqual(await self.post(stop), (200, "accepted"))
                self.assertTrue(task.cancelled())
                self.assertFalse(self.adapter._ledger.pending_acceptance_thoughts())
            finally:
                release.set()
                status, reason = await asyncio.wait_for(reply, 5)
        self.assertEqual((status, reason), (503, "clarify_unavailable"))
        self.assertNotEqual(entry.response, "yes")
        self.assertFalse(self.done(payload))
        self.assertIsNone(self.adapter._ledger.get_outbox_item("activity:clarify-resolved:stop-read"))
        self.assertEqual(len(self.normal), 1)

    async def test_signed_reply_rechecks_rotation_completion_stop_closure_and_vendor_fences(self):
        for gate in ("rotation", "completed", "stop", "replacement", "vendor-done", "delegate",
                     "actor", "missing-owner", "malformed-owner", "closure"):
            with self.subTest(gate=gate):
                clarify_gateway.clear_session(self.key)
                active = self.adapter._active_turn_events["linear-session-221"]
                turn_id = "turn-" + gate
                self.adapter.open_progress_turn("linear-session-221", turn_id)
                self.adapter._completed_turn_results.clear()
                active._linear_completed_turn_owners = set()
                entry = clarify_gateway.register(gate, self.key, "Target?", ["yes"],
                    turn_owner=("hermes-session-221", turn_id))
                self.assertTrue((await self.adapter.send_clarify(
                    "linear-session-221", "Target?", ["yes"], gate, self.key)).success)
                payload = self.payload(body="1", webhook="gate-" + gate)
                lookup = self.adapter.gateway_runner.async_session_store.lookup_by_session_key
                context = await self.adapter._linear.get_agent_turn_context("linear-session-221")

                async def change_after_read(key):
                    session = await lookup(key)
                    if gate == "rotation":
                        return SimpleNamespace(session_key=key, session_id="rotated")
                    if gate == "completed":
                        self.adapter.record_completed_turn(chat_id="linear-session-221",
                            hermes_session_id="hermes-session-221", turn_id=turn_id,
                            completed=True, failed=False, interrupted=False, turn_exit_reason="completed")
                    elif gate == "stop":
                        # Signed Stop must finish while this incoming reply is in a read.
                        stop = self.payload(body="stop", webhook="native-stop")
                        stop["agentActivity"]["signal"] = "stop"
                        with mock.patch.object(self.adapter, "handle_message", mock.AsyncMock()), \
                                mock.patch.object(self.adapter, "_cancel_linear_session_processing",
                                    mock.AsyncMock(side_effect=lambda *a: clarify_gateway.clear_session(self.key))):
                            self.assertEqual(await self.post(stop), (200, "accepted"))
                    elif gate == "replacement":
                        clarify_gateway.cancel(gate)
                        clarify_gateway.register(gate, self.key, "Target?", ["yes"],
                            turn_owner=("hermes-session-221", turn_id))
                    elif gate == "closure":
                        self.adapter._ledger.enqueue_closure_activity(
                            "closure-http", "issue-221", "linear-session-221", "close-http", "Closed.", {})
                    return session

                if gate == "vendor-done":
                    context["issue"]["state"]["type"] = "completed"
                elif gate == "delegate":
                    context["issue"]["delegate"]["id"] = "different-app"
                elif gate == "actor":
                    payload["actor"]["id"] = "other-human"
                elif gate == "missing-owner":
                    entry.turn_owner = None
                elif gate == "malformed-owner":
                    entry.turn_owner = ["hermes-session-221", turn_id]
                with mock.patch.object(self.adapter._linear, "get_agent_turn_context", mock.AsyncMock(return_value=context)), \
                        mock.patch.object(self.adapter.gateway_runner.async_session_store, "lookup_by_session_key", change_after_read):
                    status, reason = await self.post(payload)
                expected = (200, "clarify_actor_mismatch") if gate == "actor" else (
                    (200, "clarify_fenced") if gate in {"closure", "vendor-done", "delegate"}
                    else (503, "clarify_unavailable"))
                self.assertEqual((status, reason), expected)
                self.assertNotEqual(entry.response, "yes")
                self.assertEqual(self.done(payload), status == 200)
                self.assertIsNone(self.adapter._ledger.get_outbox_item(f"activity:clarify-resolved:{gate}"))
        self.assertEqual(self.normal, [])
        self.assertFalse(any("Yanıt alındı" in a[2] for a in self.transport.activities))

    def test_reply_binding_migrates_and_survives_release_restart_and_expiry(self):
        import sqlite3
        path = self.tmp.name + "/legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE deliveries (webhook_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at INTEGER NOT NULL)")
            db.execute("INSERT INTO deliveries VALUES ('legacy', 'done', 100)")
        ledger = native.ledger_mod.DeliveryLedger(path, retention_seconds=60, startup_recovery=False)
        try:
            self.assertTrue(ledger.delivery_is_done("legacy"))
            self.assertFalse(ledger.acceptance_thought_is_current("legacy"))
            self.assertTrue(ledger.claim("owed", now=100))
            self.assertEqual(ledger.bind_clarify_reply("owed", "original"), "original")
            ledger.mark_done("owed", now=100, acceptance_thought={
                "agent_session_id": "linear-session-221", "issue_id": "issue-221", "include_queued": False,
            })
            ledger.release("owed")
            self.assertTrue(ledger.delivery_is_done("owed"))
            self.assertTrue(ledger.claim("reply", now=100))
            self.assertEqual(ledger.bind_clarify_reply("reply", "original"), "original")
            ledger.release("reply")
            self.assertFalse(ledger.delivery_is_done("reply"))
            self.assertTrue(ledger.claim("reply", now=101))
            self.assertEqual(ledger.bind_clarify_reply("reply", "successor"), "original")
            ledger.release("reply")
            ledger.close()
            ledger = native.ledger_mod.DeliveryLedger(path, retention_seconds=60, startup_recovery=False)
            self.assertTrue(ledger.claim("reply", now=102))
            self.assertEqual(ledger.bind_clarify_reply("reply", ""), "original")
            ledger.release("reply")
            self.assertTrue(ledger.claim("normal", now=102))
            self.assertEqual(ledger.bind_clarify_reply("normal", ""), "")
            ledger.release("normal")
            self.assertTrue(ledger.claim("normal", now=103))
            self.assertEqual(ledger.bind_clarify_reply("normal", "successor"), "")
            ledger.release("normal")
            ledger.prune(now=200)
            self.assertTrue(ledger.delivery_is_done("owed"))
            self.assertEqual(ledger.bind_clarify_reply("owed", "successor"), "original")
            self.assertTrue(ledger.pending_acceptance_thoughts("owed"))
            ledger.cancel_acceptance_thoughts("linear-session-221")
            ledger.prune(now=200)
            self.assertFalse(ledger.delivery_is_done("owed"))
            self.assertTrue(ledger.claim("reply", now=200))
            self.assertEqual(ledger.bind_clarify_reply("reply", "fresh"), "fresh")
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
