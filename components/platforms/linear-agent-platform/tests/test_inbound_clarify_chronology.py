"""Signed live-waiter corrections and real core resets; vendor transport is offline."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import os
from pathlib import Path
import unittest
from unittest import mock

import test_inbound_clarify_owner as inbound
from tools import clarify_gateway
from gateway.config import GatewayConfig
from gateway.session import SessionStore, AsyncSessionStore


class InboundClarifyChronologyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        inbound.InboundClarifyOwnerTests.setUp(self)
        env = mock.patch.dict(
            os.environ, {"HERMES_HOME": self.tmp.name + "/hermes-test-home"}
        )
        env.start()
        self.addCleanup(env.stop)

    tearDown = inbound.InboundClarifyOwnerTests.tearDown
    asyncSetUp = inbound.InboundClarifyOwnerTests.asyncSetUp
    asyncTearDown = inbound.InboundClarifyOwnerTests.asyncTearDown
    payload = inbound.InboundClarifyOwnerTests.payload
    post = inbound.InboundClarifyOwnerTests.post
    done = inbound.InboundClarifyOwnerTests.done

    async def publish(self, cid="review", owner=None):
        entry = clarify_gateway.register(
            cid,
            self.key,
            "Target?",
            ["yes"],
            turn_owner=owner or ("hermes-session-221", "turn-221"),
        )
        result = await self.adapter.send_clarify(
            "linear-session-221", "Target?", ["yes"], cid, self.key
        )
        self.assertTrue(result.success, result.error)
        return entry

    async def core_store(self):
        active = self.adapter._active_turn_events["linear-session-221"]
        store = SessionStore(Path(self.tmp.name) / "sessions", GatewayConfig())
        facade = AsyncSessionStore(store)
        original = await facade.get_or_create_session(active.source)
        self.assertEqual(original.session_key, self.key)
        active.metadata["gateway_session_id"] = original.session_id
        self.adapter.gateway_runner.async_session_store = facade
        return facade, original

    async def test_vendor_provenance_and_strict_chronology(self):
        entry = await self.publish()
        payload = self.payload(body="1", webhook="review-proof")
        aid = payload["agentActivity"]["id"]
        qid = self.adapter._ledger.get_outbox_item("activity:clarify:review")[
            "payload"
        ]["activity_id"]
        pristine = deepcopy(self.transport.evidence)
        for gate in (
            "missing-question",
            "missing-answer",
            "id",
            "body",
            "actor",
            "app",
            "question-owner",
            "question-type",
            "answer-type",
            "equal-time",
            "answer-before-question",
            "stop",
            "different-session",
            "malformed-time",
        ):
            with self.subTest(gate=gate):
                self.transport.evidence = deepcopy(pristine)
                evidence = self.transport.evidence
                q, a = evidence[qid], evidence[aid]
                if gate == "missing-question":
                    del evidence[qid]
                elif gate == "missing-answer":
                    del evidence[aid]
                elif gate == "id":
                    a["id"] = "different-answer"
                elif gate == "body":
                    a["content"]["body"] = "not the signed body"
                elif gate == "actor":
                    a["user"]["id"] = "different-human"
                elif gate == "app":
                    a["user"]["app"] = True
                elif gate == "question-owner":
                    q["user"]["id"] = "different-app"
                elif gate == "question-type":
                    q["content"]["__typename"] = "AgentActivityThoughtContent"
                elif gate == "answer-type":
                    a["content"]["__typename"] = "AgentActivityElicitationContent"
                elif gate == "equal-time":
                    a["createdAt"] = q["createdAt"]
                elif gate == "answer-before-question":
                    q["createdAt"], a["createdAt"] = a["createdAt"], q["createdAt"]
                elif gate == "stop":
                    evidence["stop"] = deepcopy(a)
                    evidence["stop"].update(id="stop", signal="stop")
                elif gate == "different-session":
                    a["agentSessionId"] = "not-this-session"
                elif gate == "malformed-time":
                    a["createdAt"] = "bad"
                self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
                self.assertFalse(self.done(payload))
                self.assertIsNone(entry.response)
                self.assertIsNone(
                    self.adapter._ledger.get_outbox_item(
                        "activity:clarify-resolved:review"
                    )
                )
        # Ordinary earlier corrections are allowed, not ambiguous chronology,
        # app prompts, commands, a different question, terminal or unknown types.
        qt = datetime.fromisoformat(pristine[qid]["createdAt"])
        at = datetime.fromisoformat(pristine[aid]["createdAt"])
        between = (qt + (at - qt) / 2).isoformat()
        for kind, signal, body, app, stamp in (
            ("Prompt", None, "/approve", False, between),
            ("Prompt", None, "1", True, between),
            ("Prompt", None, "1", False, pristine[qid]["createdAt"]),
            ("Prompt", None, "1", False, (at + timedelta(seconds=1)).isoformat()),
            ("Elicitation", None, "New?", True, between),
            ("Response", None, "Done.", True, between),
            ("Error", None, "Failed.", True, between),
            ("Unknown", None, "Unknown.", True, between),
        ):
            with self.subTest(kind=kind, body=body, app=app, stamp=stamp):
                self.transport.evidence = deepcopy(pristine)
                extra = deepcopy(pristine[aid])
                extra.update(id="extra", createdAt=stamp, signal=signal)
                extra["content"].update(
                    __typename="AgentActivity" + kind + "Content", body=body
                )
                extra["user"]["app"] = app
                self.transport.evidence["extra"] = extra
                self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
                self.assertIsNone(entry.response)
                self.assertFalse(self.done(payload))
        self.transport.evidence = pristine
        self.assertEqual(await self.post(payload), (200, "clarify_resolved"))
        self.assertEqual(entry.response, "yes")

    async def test_authorized_answer_after_rejected_actor_still_resolves(self):
        entry = await self.publish()
        rejected = self.payload(actor="other-human", body="1", webhook="other-human")
        self.assertEqual(await self.post(rejected), (200, "clarify_actor_mismatch"))
        self.assertTrue(self.done(rejected))
        self.assertIsNone(entry.response)
        reply = self.payload(body="1", webhook="actual-owner")
        qid = self.adapter._ledger.get_outbox_item("activity:clarify:review")[
            "payload"
        ]["activity_id"]
        self.assertFalse(
            await self.adapter._linear.verify_late_clarify_reply(
                "linear-session-221", qid, reply["agentActivity"]["id"], "user-221", "1"
            )
        )
        self.assertEqual(await self.post(reply), (200, "clarify_resolved"))
        self.assertEqual(entry.response, "yes")
        self.assertTrue(self.done(reply))

    async def test_vendor_shaped_owner_after_rejected_requester_still_resolves(self):
        active = self.adapter._active_turn_events["linear-session-221"]
        active.source.user_id = None
        context = await self.adapter._linear.get_agent_turn_context(
            "linear-session-221"
        )
        context["issue"]["assignee"] = {"id": "user-221", "app": False}
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value=context
        )
        entry = await self.publish()

        def vendor_prompt(actor, webhook):
            payload = self.payload(actor=actor, body="1", webhook=webhook)
            payload.pop("actor")
            activity = payload["agentActivity"]
            activity.pop("body")
            activity.update(
                userId=actor,
                user={"id": actor},
                agentSessionId="linear-session-221",
                content={"type": "prompt", "body": "1"},
            )
            return payload

        rejected = vendor_prompt("other-human", "vendor-other-human")
        self.assertEqual(
            await self.post(rejected), (200, "clarify_requester_binding_unavailable")
        )
        self.assertTrue(self.done(rejected))
        self.assertIsNone(entry.response)
        reply = vendor_prompt("user-221", "vendor-actual-owner")
        self.assertEqual(await self.post(reply), (200, "clarify_resolved"))
        self.assertEqual(entry.response, "yes")
        self.assertTrue(self.done(reply))
        self.assertIsNone(active.source.user_id)

    async def test_rotation_during_vendor_read_does_not_answer_old_session(self):
        facade, original = await self.core_store()
        entry = await self.publish(owner=(original.session_id, "turn-221"))
        payload = self.payload(body="1", webhook="rotation-after-lookup")
        read = self.adapter._linear.graphql
        rotated = None

        async def rotate_while_reading(query, variables):
            nonlocal rotated
            rotated = await facade.reset_session(self.key)
            self.assertNotEqual(rotated.session_id, original.session_id)
            return await read(query, variables)

        with mock.patch.object(self.adapter._linear, "graphql", rotate_while_reading):
            result = await self.post(payload)
        self.assertEqual(result, (503, "clarify_unavailable"))
        self.assertFalse(self.done(payload))
        self.assertIsNone(entry.response)
        self.assertEqual(
            (await facade.lookup_by_session_key(self.key)).session_id,
            rotated.session_id,
        )
        self.assertIsNone(
            self.adapter._ledger.get_outbox_item("activity:clarify-resolved:review")
        )

    async def test_rotation_after_final_async_snapshot_does_not_answer_old_or_successor(
        self,
    ):
        facade, original = await self.core_store()
        entry = await self.publish(owner=(original.session_id, "turn-221"))
        payload = self.payload(body="1", webhook="after-snapshot")
        lookup = facade.lookup_by_session_key
        successor = None

        async def rotate_after_snapshot(key):
            nonlocal successor
            snapshot = await lookup(key)
            rotated = await facade.reset_session(key)
            self.assertNotEqual(rotated.session_id, snapshot.session_id)
            successor = clarify_gateway.register(
                "successor",
                key,
                "New?",
                ["yes"],
                turn_owner=(rotated.session_id, "next-turn"),
            )
            return snapshot

        with mock.patch.object(facade, "lookup_by_session_key", rotate_after_snapshot):
            self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
        self.assertIsNone(entry.response)
        self.assertIsNone(successor.response)
        self.assertFalse(self.done(payload))
        self.assertIsNone(
            self.adapter._ledger.get_outbox_item("activity:clarify-resolved:review")
        )

    async def test_contended_core_store_lock_is_retryable_without_blocking_stop(self):
        facade, original = await self.core_store()
        entry = await self.publish(owner=(original.session_id, "turn-221"))
        payload = self.payload(body="1", webhook="contended-store")
        lookup = facade.lookup_by_session_key

        async def contend_after_snapshot(key):
            snapshot = await lookup(key)
            self.assertTrue(facade._lock.acquire(blocking=False))
            return snapshot

        try:
            with mock.patch.object(
                facade, "lookup_by_session_key", contend_after_snapshot
            ):
                self.assertEqual(await self.post(payload), (503, "clarify_unavailable"))
            stop = self.payload(body="stop", webhook="stop-contended")
            stop["agentActivity"]["signal"] = "stop"
            with (
                mock.patch.object(self.adapter, "handle_message", mock.AsyncMock()),
                mock.patch.object(
                    self.adapter,
                    "_cancel_linear_session_processing",
                    mock.AsyncMock(
                        side_effect=lambda *a: clarify_gateway.clear_session(self.key)
                    ),
                ),
            ):
                self.assertEqual(await self.post(stop), (200, "accepted"))
        finally:
            if facade._lock.locked():
                facade._lock.release()
        self.assertNotEqual(entry.response, "yes")
        self.assertFalse(self.done(payload))
        self.assertIsNone(
            self.adapter._ledger.get_outbox_item("activity:clarify-resolved:review")
        )

    async def test_signed_stop_finishes_during_vendor_read(self):
        entry = await self.publish()
        payload = self.payload(body="1", webhook="blocked-evidence")
        entered, release = asyncio.Event(), asyncio.Event()
        read = self.adapter._linear.graphql

        async def blocked_read(*args):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await read(*args)

        with mock.patch.object(self.adapter._linear, "graphql", blocked_read):
            post = asyncio.create_task(self.post(payload))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                stop = self.payload(body="stop", webhook="stop-during-read")
                stop["agentActivity"]["signal"] = "stop"
                with (
                    mock.patch.object(self.adapter, "handle_message", mock.AsyncMock()),
                    mock.patch.object(
                        self.adapter,
                        "_cancel_linear_session_processing",
                        mock.AsyncMock(
                            side_effect=lambda *a: clarify_gateway.clear_session(
                                self.key
                            )
                        ),
                    ),
                ):
                    self.assertEqual(await self.post(stop), (200, "accepted"))
            finally:
                release.set()
                result = await asyncio.wait_for(post, 5)
        self.assertEqual(result, (503, "clarify_unavailable"))
        self.assertNotEqual(entry.response, "yes")
        self.assertFalse(self.done(payload))
        self.assertIsNone(
            self.adapter._ledger.get_outbox_item("activity:clarify-resolved:review")
        )

    async def test_core_store_accepts_owner_after_rejected_actor(self):
        _, original = await self.core_store()
        entry = await self.publish(owner=(original.session_id, "turn-221"))
        bad = self.payload(actor="other-human", body="1", webhook="bad-after-ack")
        self.assertEqual(await self.post(bad), (200, "clarify_actor_mismatch"))
        self.assertTrue(self.done(bad))
        self.assertIsNone(entry.response)
        good = self.payload(body="1", webhook="good-after-ack")
        self.assertEqual(await self.post(good), (200, "clarify_resolved"))
        self.assertEqual(entry.response, "yes")
        self.assertTrue(self.done(good))

    async def test_corrected_choice_after_invalid_reply_before_create_ack(self):
        entry = clarify_gateway.register(
            "before-ack",
            self.key,
            "Target?",
            ["yes"],
            turn_owner=("hermes-session-221", "turn-221"),
        )
        entered, release = asyncio.Event(), asyncio.Event()
        create = self.transport.create_activity

        async def delayed_ack(*args, **kwargs):
            aid = await create(*args, **kwargs)
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return aid

        with mock.patch.object(self.transport, "create_activity", delayed_ack):
            send = asyncio.create_task(
                self.adapter.send_clarify(
                    "linear-session-221", "Target?", ["yes"], "before-ack", self.key
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                bad = self.payload(body="7", webhook="bad-selection")
                self.assertEqual(await self.post(bad), (200, "clarify_rejected"))
                self.assertTrue(self.done(bad))
                self.assertIsNone(entry.response)
                good = self.payload(body="1", webhook="corrected-selection")
                self.assertEqual(await self.post(good), (200, "clarify_resolved"))
                self.assertEqual(entry.response, "yes")
                self.assertTrue(self.done(good))
            finally:
                release.set()
                self.assertTrue((await asyncio.wait_for(send, 5)).success)
