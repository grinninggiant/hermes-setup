"""Signed loopback ingress must interrupt running core work before slow locks."""
from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_native_platform as fixtures
import test_native_continuation as native


class StopIngressContentionTests(unittest.IsolatedAsyncioTestCase):
    make_payload = fixtures.AdapterWebhookTests.make_payload
    request_for = fixtures.AdapterWebhookTests.request_for

    async def asyncSetUp(self):
        await fixtures.AdapterWebhookTests.asyncSetUp(self)
        del self.adapter.handle_message
        self.adapter._native_goal_continuation_enabled = True
        self.adapter.gateway_runner = native.fake_gateway_runner()
        turn_client = native.FakeLinear()
        turn_client.actor_id = self.adapter._linear.actor_id
        self.adapter._linear.get_agent_turn_context = turn_client.get_agent_turn_context
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.payload = self.make_payload(agentSession={
            "id": "linear-session", "issue": {"id": "issue-164"}
        })
        app = web.Application()
        app.router.add_post("/linear/webhook", self.adapter._handle_webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for key in list(self.adapter._session_tasks):
            await self.adapter.cancel_session_processing(key)
        await fixtures.AdapterWebhookTests.asyncTearDown(self)

    async def test_stop_interrupts_core_before_locks_but_keeps_durable_fence(self):
        locks = (
            self.adapter._issue_lock("issue-164"),
            self.adapter._session_lock("linear-session"),
            self.adapter._outbox_drain_lock,
        )
        for ordinal, lock in enumerate(locks):
            with self.subTest(lock=ordinal):
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def handler(_event):
                    started.set()
                    try:
                        await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
                    finally:
                        cancelled.set()

                self.adapter.set_message_handler(handler)
                event = self.adapter._message_event(self.payload, f"work-{ordinal}", "fixture")
                await self.adapter.handle_message(event)
                self.assertIs(getattr(event, "_gateway_accepted", None), True)
                await asyncio.wait_for(started.wait(), timeout=1)
                row = self.adapter._ledger.reserve_turn_decision(
                    "linear-session", "issue-164", "hermes-session", 1, ordinal, "continue"
                )
                self.adapter._ledger.transition_turn_decision(
                    row["decision_id"], "pending", "enqueued"
                )
                request = self.request_for({**self.payload, "action": "prompted", "agentActivity": {
                    "id": f"stop-{ordinal}", "body": "stop", "signal": "stop"
                }})
                await lock.acquire()
                post = asyncio.create_task(self.client.post(
                    "/linear/webhook", data=request._body, headers=request.headers
                ))
                try:
                    # A blocked lock must not keep actual core execution running.
                    await asyncio.wait_for(cancelled.wait(), timeout=1)
                    self.assertFalse(post.done())  # No early success ACK before the fence.
                    # Admission already in flight can finish while Stop waits;
                    # the original locked cancellation must still catch it.
                    if lock is not self.adapter._outbox_drain_lock:
                        started.clear()
                        cancelled.clear()
                        late = self.adapter._message_event(self.payload, f"late-{ordinal}", "fixture")
                        await self.adapter.handle_message(late)
                        await asyncio.wait_for(started.wait(), timeout=1)
                finally:
                    lock.release()
                    response = await asyncio.wait_for(post, timeout=2)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(response.status, 200)
                self.assertEqual(self.adapter._ledger.get_turn_decision(
                    row["decision_id"]
                )["dispatch_state"], "fenced")
                self.assertEqual(self.adapter._ledger.get_turn_decision(
                    row["decision_id"]
                )["outcome"], "stopped")

    async def test_ledger_contention_bounds_ingress_without_losing_stop_or_replay(self):
        import sqlite3
        import threading
        import time
        from unittest import mock

        for ordinal, kind in enumerate(("sqlite", "python")):
            with self.subTest(lock=kind):
                ledger = self.adapter._ledger
                started, cancelled = asyncio.Event(), asyncio.Event()
                locked, entered, release = threading.Event(), threading.Event(), threading.Event()
                errors, calls, cancellation_times = [], [], []

                async def handler(event):
                    if event.text.lstrip().startswith("/stop"):
                        return
                    calls.append(event.message_id)
                    started.set()
                    try:
                        await asyncio.wait_for(asyncio.Event().wait(), timeout=15)
                    finally:
                        cancellation_times.append(time.monotonic())
                        cancelled.set()

                self.adapter.set_message_handler(handler)
                event = self.adapter._message_event(self.payload, f"running-{kind}", "fixture")
                await self.adapter.handle_message(event)
                await asyncio.wait_for(started.wait(), timeout=2)
                decision = ledger.reserve_turn_decision(
                    "linear-session", "issue-164", "hermes-session", 1, ordinal, "continue"
                )
                ledger.transition_turn_decision(decision["decision_id"], "pending", "enqueued")
                stop = {**self.payload, "action": "prompted", "agentActivity": {
                    "id": f"stop-ledger-{kind}", "body": "stop", "signal": "stop"
                }}
                data = {**self.payload, "type": "Issue", "action": "update",
                        "data": {"id": "issue-164"}}

                def holder():
                    connection = None
                    try:
                        if kind == "sqlite":
                            connection = sqlite3.connect(ledger.path)
                            connection.execute("BEGIN IMMEDIATE")
                        else:
                            ledger._lock.acquire()
                        locked.set()
                        if not entered.wait(2):
                            raise AssertionError("ingress never reached claim")
                        release.wait(6)  # Independent of the event loop being tested.
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        if connection is not None:
                            connection.rollback()
                            connection.close()
                        elif locked.is_set():
                            ledger._lock.release()

                claim = ledger.claim

                def observed_claim(*args, **kwargs):
                    entered.set()
                    return claim(*args, **kwargs)

                async def post(payload):
                    request = self.request_for(payload)
                    response = await asyncio.wait_for(self.client.post(
                        "/linear/webhook", data=request._body, headers=request.headers
                    ), timeout=10)
                    return response.status, time.monotonic() - before

                thread = threading.Thread(target=holder)
                thread.start()
                try:
                    self.assertTrue(await asyncio.to_thread(locked.wait, 2))
                    before = time.monotonic()
                    with mock.patch.object(ledger, "claim", side_effect=observed_claim):
                        results = await asyncio.wait_for(asyncio.gather(
                            post(self.payload), post(stop), post(data)
                        ), timeout=10)
                finally:
                    release.set()
                    await asyncio.to_thread(thread.join, 3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results[0][0], 503)  # No admission/late worker mutation.
                self.assertIn(results[1][0], (200, 503))
                self.assertIn(results[2][0], (200, 503))  # Sibling ingress must not leak 500.
                # No durable receipt was possible: refusal is not cancellation.
                self.assertEqual(results[1][0], 503)
                self.assertFalse(cancelled.is_set())
                self.assertEqual(len(calls), 1)
                # A refused durable fence needs a successful retry; no restart promise before it.
                self.assertEqual((await post(stop))[0], 200)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(ledger.get_turn_decision(decision["decision_id"])["outcome"], "stopped")
                started.clear()
                cancelled.clear()
                await self.adapter.handle_message(self.adapter._message_event(
                    self.payload, f"successor-{kind}", "fixture"
                ))
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual((await post(stop))[0], 200)
                self.assertFalse(cancelled.is_set(), "duplicate Stop interrupted a successor")
                for key in list(self.adapter._session_tasks):
                    await self.adapter.cancel_session_processing(key)
                self.assertLess(max(elapsed for _, elapsed in results), 5,
                                f"{kind} ledger held event loop: {results}")

    async def test_refused_stop_retry_does_not_cancel_successor(self):
        from unittest import mock

        started, finish, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(event):
            started.set()
            if event.message_id == "old":
                await finish.wait()
            else:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        self.adapter.set_message_handler(handler)
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "old", "fixture"))
        await asyncio.wait_for(started.wait(), 2)
        old_task, = self.adapter._session_tasks.values()
        stop = {**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-refused", "body": "stop", "signal": "stop",
        }}
        with mock.patch.object(self.adapter._ledger, "claim", side_effect=OSError("ledger unavailable")):
            response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 503)
        finish.set()
        await asyncio.wait_for(old_task, 2)
        started.clear()
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "successor", "fixture"))
        await asyncio.wait_for(started.wait(), 2)
        response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 200)
        self.assertFalse(cancelled.is_set(), "503 retry rebound Stop to a successor")

    async def test_refused_stop_retry_can_cancel_same_owner_after_event_initializes(self):
        from unittest import mock

        entering, release, started, cancelled = (asyncio.Event() for _ in range(4))
        on_start = self.adapter.on_processing_start

        async def delayed_start(event):
            entering.set()
            await release.wait()
            return await on_start(event)

        async def handler(_event):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.adapter.on_processing_start = delayed_start
        self.adapter.set_message_handler(handler)
        await self.adapter.handle_message(self.adapter._message_event(self.payload, "old", "fixture"))
        await asyncio.wait_for(entering.wait(), 2)
        old_task, = self.adapter._session_tasks.values()
        stop = {**self.payload, "action": "prompted", "agentActivity": {
            "id": "stop-before-event-bind", "body": "stop", "signal": "stop",
        }}
        with mock.patch.object(self.adapter._ledger, "claim", side_effect=OSError("ledger unavailable")):
            self.assertEqual((await self.adapter._handle_webhook(self.request_for(stop))).status, 503)
        release.set()
        await asyncio.wait_for(started.wait(), 2)
        self.assertIs(self.adapter._session_tasks[next(iter(self.adapter._session_tasks))], old_task)
        response = await self.adapter._handle_webhook(self.request_for(stop))
        self.assertEqual(response.status, 200)
        self.assertNotEqual(json.loads(response.text)["status"], "stale_stop")
        await asyncio.wait_for(cancelled.wait(), 2)

    async def test_failed_fence_requires_retry_before_restart_recovery(self):
        from unittest import mock

        for ordinal, crash in enumerate(("before_fence", "after_fence")):
            with self.subTest(crash=crash):
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def handler(event):
                    if event.text == "/stop":
                        return
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                self.adapter.set_message_handler(handler)
                event = self.adapter._message_event(self.payload, f"crash-running-{crash}", "fixture")
                await self.adapter.handle_message(event)
                await asyncio.wait_for(started.wait(), 2)
                decision = self.adapter._ledger.reserve_turn_decision(
                    "linear-session", "issue-164", "hermes-session", 1, ordinal, "continue"
                )
                self.adapter._ledger.transition_turn_decision(decision["decision_id"], "pending", "enqueued")
                stop = {**self.payload, "action": "prompted", "agentActivity": {
                    "id": f"stop-crash-{crash}", "body": "stop", "signal": "stop"
                }}
                fence = self.adapter._ledger.fence_turn_decisions

                def failed_fence(*args, **kwargs):
                    if crash == "after_fence":
                        fence(*args, **kwargs)  # Real commit, then lose the settlement reply.
                    raise OSError(f"injected crash window: {crash}")

                with mock.patch.object(self.adapter._ledger, "fence_turn_decisions",
                                       side_effect=failed_fence):
                    response = await self.adapter._handle_webhook(self.request_for(stop))
                self.assertEqual(response.status, 503)
                await asyncio.wait_for(cancelled.wait(), 2)
                # Fresh adapter AND SQLite connection; no in-memory receipt copied.
                restarted = fixtures.LinearPlatformAdapter(self.adapter.config, fixtures.Platform.WEBHOOK)
                restarted._ledger = fixtures.DeliveryLedger(str(self.adapter._ledger.path))
                restarted._linear = self.adapter._linear
                restarted._signing_secrets = self.adapter._signing_secrets
                restarted._native_goal_continuation_enabled = True
                restarted.gateway_runner = native.fake_gateway_runner()
                try:
                    self.assertEqual(restarted._ledger.get_turn_decision(decision["decision_id"])["outcome"],
                                     "stopped" if crash == "after_fence" else "continue")
                    # No independent journal: the 503 requires a successful retry
                    # before restart recovery, not a pre-retry durable Stop gate.
                    response = await restarted._handle_webhook(self.request_for(stop))
                    self.assertEqual(response.status, 200)
                    await restarted._recover_turn_decisions()
                    self.assertEqual(restarted._ledger.get_turn_decision(decision["decision_id"])["outcome"], "stopped")
                finally:
                    restarted._ledger.close()


class StopRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise native worker gates and targeted interruption in the paired core."""

    async def test_worker_entry_rechecks_stop_gate_for_external_and_internal_turns(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from gateway.run import GatewayRunner
        from gateway.session import build_session_key

        for internal in (False, True):
            with self.subTest(internal=internal):
                entered, release = asyncio.Event(), asyncio.Event()
                gate_open = True
                calls = []
                event = native.turn_event(internal=internal)
                key = build_session_key(event.source)
                event.metadata.update(gateway_session_strict=True,
                                      gateway_session_key=key,
                                      gateway_session_id="hermes-session")
                adapter = fixtures.LinearPlatformAdapter(
                    fixtures.PlatformConfig(enabled=True), fixtures.Platform.WEBHOOK,
                )

                async def allowed(_event):
                    return gate_open

                async def lookup(_key):
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=2)
                    return SimpleNamespace(session_id="hermes-session")

                async def worker(*_args, **_kwargs):
                    calls.append(event.message_id)
                    return {"completed": True, "interrupted": False}

                adapter.allow_execution = allowed
                runner = object.__new__(GatewayRunner)
                runner._profile_scope_for_source = lambda _source: nullcontext()
                runner._adapter_for_source = lambda _source: adapter
                runner._session_key_for_source = lambda _source: key
                runner.session_store = object()
                runner._async_session_store = SimpleNamespace(
                    _store=runner.session_store, lookup_by_session_key=lookup,
                )
                runner._run_agent_inner = worker  # No model/vendor call; observe the real worker boundary.
                if not internal:
                    gate_open = False
                task = asyncio.create_task(runner._run_agent(
                    "fixture", "", [], event.source, "hermes-session",
                    gateway_event=event, session_key=key,
                ))
                try:
                    if internal:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        gate_open = False  # Durable Stop arrives after the policy read.
                        release.set()
                    result = await asyncio.wait_for(task, timeout=2)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(calls, [], "closed Stop gate reached the real core worker boundary")
                self.assertTrue(result["interrupted"])

    async def test_public_interrupt_cannot_retarget_same_session_successor_during_lookup(self):
        from types import SimpleNamespace
        from gateway.run import GatewayRunner
        from gateway.session import build_session_key

        class Agent:
            def __init__(self):
                self.reasons = []

            def hard_interrupt(self, reason):
                self.reasons.append(reason)

        source = native.source()
        key = build_session_key(source)
        entered, release = asyncio.Event(), asyncio.Event()

        async def lookup(_key):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)
            return SimpleNamespace(session_id="hermes-session")

        runner = object.__new__(GatewayRunner)
        runner._session_key_for_source = lambda _source: key
        runner._adapter_for_source = lambda _source: None
        runner._persist_active_agents = lambda: None  # Isolate dashboard I/O, not the interrupt funnel.
        runner.session_store = object()
        runner._async_session_store = SimpleNamespace(
            _store=runner.session_store, lookup_by_session_key=lookup,
        )
        runner._agent_cache = {}
        old, successor = Agent(), Agent()
        state = runner._session_state(key)
        state.turn.agent = old
        old_generation = runner._begin_session_run_generation(key)
        task = asyncio.create_task(runner.interrupt_session_processing(
            source, reason="linear_authoritative_stop", expected_session_id="hermes-session",
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            state.turn.agent = successor
            successor_generation = runner._begin_session_run_generation(key)
            self.assertNotEqual(old_generation, successor_generation)
            release.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(successor.reasons, [], "session identity is not exact turn-generation identity")
        self.assertTrue(runner._is_session_run_current(key, successor_generation))

    async def test_plugin_cancel_cannot_cancel_successor_after_core_lookup(self):
        from types import SimpleNamespace
        from gateway.run import GatewayRunner
        from gateway.session import build_session_key
        from gateway.platforms.base import BasePlatformAdapter

        adapter = fixtures.LinearPlatformAdapter(
            fixtures.PlatformConfig(enabled=True), fixtures.Platform.WEBHOOK,
        )
        adapter._native_goal_continuation_enabled = True
        old_started, old_release = asyncio.Event(), asyncio.Event()
        successor_started, successor_cancelled = asyncio.Event(), asyncio.Event()
        entered, release = asyncio.Event(), asyncio.Event()
        reasons = []

        async def handler(event):
            if event.message_id == "old":
                old_started.set()
                await old_release.wait()
            else:
                successor_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    successor_cancelled.set()

        async def lookup(_key):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return SimpleNamespace(session_id="hermes-session")

        old = native.turn_event()
        old.message_id = "old"
        old.metadata["gateway_session_id"] = "hermes-session"
        key = build_session_key(old.source)
        runner = object.__new__(GatewayRunner)
        runner._session_key_for_source = lambda _source: key
        runner._adapter_for_source = lambda _source: adapter
        runner._persist_active_agents = lambda: None
        runner.session_store = object()
        runner._async_session_store = SimpleNamespace(
            _store=runner.session_store, lookup_by_session_key=lookup,
        )
        runner._agent_cache = {}
        adapter.gateway_runner = runner
        adapter.set_message_handler(handler)
        await BasePlatformAdapter.handle_message(adapter, old)
        await asyncio.wait_for(old_started.wait(), 2)
        old_task = adapter._session_tasks[key]
        runner._session_state(key).turn.agent = SimpleNamespace(hard_interrupt=lambda _reason: None)
        generation = runner._begin_session_run_generation(key)
        runner._bind_adapter_run_generation(adapter, key, generation)
        stopping = asyncio.create_task(adapter._cancel_linear_session_processing("linear-session"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            old_release.set()
            await asyncio.wait_for(old_task, 2)
            successor = native.turn_event()
            successor.message_id = "successor"
            await BasePlatformAdapter.handle_message(adapter, successor)
            await asyncio.wait_for(successor_started.wait(), 2)
            runner._session_state(key).turn.agent = SimpleNamespace(hard_interrupt=reasons.append)
            successor_generation = runner._begin_session_run_generation(key)
            runner._bind_adapter_run_generation(adapter, key, successor_generation)
            release.set()
            await asyncio.wait_for(stopping, 2)
            self.assertEqual(reasons, [], "paired core must preserve the successor agent")
            self.assertFalse(successor_cancelled.is_set(),
                             "plugin's unscoped finally cancelled the successor adapter task")
            self.assertTrue(runner._is_session_run_current(key, successor_generation))
        finally:
            release.set()
            old_release.set()
            if not stopping.done():
                stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            await adapter.cancel_session_processing(key)


if __name__ == "__main__":
    unittest.main()
