from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Callable, ClassVar
from unittest import mock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    GoalStatusNotice,
    GoalStatusNoticeKind,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.session import SessionSource


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "linear_native_continuation_test_plugin"
spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert spec is not None and spec.loader is not None
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = package
spec.loader.exec_module(package)

adapter_mod = __import__(f"{PACKAGE_NAME}.adapter", fromlist=["*"])
client_mod = __import__(f"{PACKAGE_NAME}.linear_client", fromlist=["*"])
ledger_mod = __import__(f"{PACKAGE_NAME}.ledger", fromlist=["*"])

LinearPlatformAdapter = adapter_mod.LinearPlatformAdapter
LinearClient = client_mod.LinearClient
LinearAPIError = client_mod.LinearAPIError
DeliveryLedger = ledger_mod.DeliveryLedger
GoalContract = adapter_mod.GoalContract


def source(session_id: str = "linear-session") -> SessionSource:
    return SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=session_id,
        user_id="human-1",
        user_name="Human",
        chat_name="OPS-164",
        chat_type="dm",
    )


def turn_event(*, internal: bool = False, decision_id: str = "") -> MessageEvent:
    event = MessageEvent(
        text="continue canonically" if internal else "work the issue",
        message_type=MessageType.TEXT,
        source=source(),
        message_id=decision_id or "webhook-event",
        internal=internal,
        metadata={
            "linear_agent_session_id": "linear-session",
            "linear_issue_id": "issue-164",
            "linear_delivery_key": "delivery-164",
        },
    )
    if decision_id:
        event.metadata["linear_continuation_decision_id"] = decision_id
    event._gateway_turn_result = MappingProxyType(
        {
            "completed": False,
            "failed": False,
            "interrupted": False,
            "turn_exit_reason": "max_iterations_reached(90)",
            "session_id": "hermes-session",
            "input_tokens": 11,
            "output_tokens": 7,
        }
    )
    return event


class FakeLinear:
    actor_id = "app-user"
    organization_id = "org"

    def __init__(
        self,
        *,
        status: str = "active",
        state_type: str = "started",
        description: str = "## Acceptance\n- [ ] tests pass\n- [ ] restart is safe",
    ) -> None:
        self.status = status
        self.state_type = state_type
        self.description = description

    async def get_agent_turn_context(self, session_id: str) -> dict:
        return {
            "id": session_id,
            "status": self.status,
            "app_user_id": self.actor_id,
            "issue": {
                "id": "issue-164",
                "identifier": "OPS-164",
                "title": "Native continuation",
                "description": self.description,
                "state": {"id": "started", "name": "In Progress", "type": self.state_type},
                "delegate": {"id": self.actor_id},
            },
            "open_blockers": [],
        }


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, separators=(",", ":")).encode()
        self.headers = {
            "Linear-Signature": hmac.new(b"s" * 32, self._body, hashlib.sha256).hexdigest()
        }

    async def read(self) -> bytes:
        return self._body


class FakeGoalManager:
    instances: list["FakeGoalManager"] = []
    existing = False
    decision = {
        "status": "active",
        "should_continue": True,
        "continuation_prompt": "NATIVE CANONICAL CONTINUATION",
        "verdict": "continue",
        "reason": "acceptance evidence missing",
        "message": "continuing",
    }
    before_evaluate: ClassVar[Callable[[], None] | None] = None
    owner_checks: list[Callable[[], bool] | None] = []
    resume_calls = 0
    resume_reset_budget: list[bool] = []
    pause_calls = 0
    existing_status = "active"
    existing_turns = 2
    existing_paused_reason: str | None = None
    waiting = False
    background_snapshots: list[object] = []

    def __init__(self, session_id: str, **_kwargs) -> None:
        self.session_id = session_id
        self.set_calls = []
        self.state = (
            SimpleNamespace(
                status=self.existing_status, created_at=123.0,
                turns_used=self.existing_turns, max_turns=20,
                paused_reason=self.existing_paused_reason,
            )
            if self.existing
            else None
        )
        self.instances.append(self)

    def has_goal(self) -> bool:
        return self.state is not None

    def is_active(self) -> bool:
        return self.state is not None and self.state.status == "active"

    def set(self, goal: str, *, contract) -> object:
        self.set_calls.append((goal, contract))
        self.state = SimpleNamespace(
            status="active", created_at=123.0, turns_used=0,
            max_turns=20, paused_reason=None,
        )
        return self.state

    def evaluate_after_turn(
        self, response: str, *, user_initiated: bool, background_processes=None,
        active_delegations: int = 0, owner_check=None,
    ) -> dict:
        assert response
        assert user_initiated is True
        type(self).owner_checks.append(owner_check)
        if owner_check is not None and not owner_check():
            return {
                "status": None,
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "stale",
                "reason": "goal ownership changed",
                "message": "",
                "stale_owner": True,
            }
        type(self).background_snapshots.append(background_processes)
        if type(self).waiting:
            return dict(self.decision)
        callback = type(self).before_evaluate
        if callback is not None:
            callback()
        self.state.turns_used += 1
        self.state.status = str(self.decision.get("status") or "active")
        self.state.paused_reason = (
            str(self.decision.get("reason") or "")
            if self.state.status == "paused" else None
        )
        return dict(self.decision)

    def resume(self, *, reset_budget: bool = True) -> object:
        type(self).resume_calls += 1
        type(self).resume_reset_budget.append(reset_budget)
        self.state.status = "active"
        self.state.paused_reason = None
        if reset_budget:
            self.state.turns_used = 0
        return self.state

    def pause(self, reason: str = "user-paused") -> object:
        type(self).pause_calls += 1
        self.state.status = "paused"
        self.state.paused_reason = reason
        return self.state

    def next_continuation_prompt(self) -> str:
        return "NATIVE CANONICAL CONTINUATION"

    def is_waiting(self) -> bool:
        return type(self).waiting


class FakeSessionStore:
    def __init__(self, session_id: str = "hermes-session") -> None:
        self._store = object()
        self.lookup_keys: list[str] = []
        self.entry = SimpleNamespace(
            session_id=session_id,
            session_key="agent:main:webhook:dm:linear-session",
        )

    async def get_or_create_session(self, _source, **_kwargs):
        return self.entry

    async def lookup_by_session_key(self, session_key: str):
        self.lookup_keys.append(session_key)
        return self.entry if session_key == self.entry.session_key else None


def attach_fake_goal_api(runner):
    async def goal_state_for_source(_source, *, session_id=None):
        return FakeGoalManager(session_id).state

    async def ensure_goal_for_source(_source, goal, *, contract, session_id=None):
        manager = FakeGoalManager(session_id)
        return manager.state if manager.has_goal() else manager.set(goal, contract=contract)

    async def resume_goal_for_source(
        _source, *, reset_budget, session_id=None
    ):
        manager = FakeGoalManager(session_id)
        state = manager.resume(reset_budget=reset_budget)
        return SimpleNamespace(
            state=state,
            continuation_prompt=manager.next_continuation_prompt(),
        )

    async def next_goal_continuation_prompt_for_source(_source, *, session_id=None):
        return FakeGoalManager(session_id).next_continuation_prompt()

    runner.goal_state_for_source = goal_state_for_source
    runner.ensure_goal_for_source = ensure_goal_for_source
    runner.resume_goal_for_source = resume_goal_for_source
    runner.next_goal_continuation_prompt_for_source = (
        next_goal_continuation_prompt_for_source
    )
    return runner


def fake_gateway_runner(store=None):
    runner = SimpleNamespace(
            async_session_store=store or FakeSessionStore(),
            interrupt_session_processing=mock.AsyncMock(
                spec=GatewayAgentCacheMixin.interrupt_session_processing,
                return_value=True,
            ),
            _profile_name_for_source=lambda _source: None,
    )
    return attach_fake_goal_api(runner)


class DecisionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger = DeliveryLedger(self.path)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_additive_schema_contains_no_prompt_or_summary_columns(self):
        columns = {
            row[1]
            for row in self.ledger._db.execute("PRAGMA table_info(turn_decisions)")
        }
        self.assertEqual(
            columns,
            {
                "decision_id",
                "agent_session_id",
                "issue_id",
                "hermes_session_id",
                "goal_generation",
                "ordinal",
                "outcome",
                "dispatch_state",
                "error",
                "created_at",
                "updated_at",
                "completed_at",
                "source_json",
            },
        )
        self.assertFalse({"prompt", "prompt_json", "summary", "summary_json"} & columns)

    def test_decision_identity_and_dispatch_cas_are_idempotent(self):
        args = ("linear-session", "issue-164", "hermes-session", 123000000, 1, "continue")
        first = self.ledger.reserve_turn_decision(*args, now=10)
        second = self.ledger.reserve_turn_decision(*args, now=11)
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertTrue(self.ledger.transition_turn_decision(first["decision_id"], "pending", "enqueued", now=12))
        self.assertFalse(self.ledger.transition_turn_decision(first["decision_id"], "pending", "enqueued", now=13))

    def test_running_rows_are_not_recovered_and_terminal_rows_are_pruned(self):
        pending = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1, 1, "continue", now=10
        )
        running = self.ledger.reserve_turn_decision(
            "s2", "i2", "h2", 1, 1, "continue", now=10
        )
        terminal = self.ledger.reserve_turn_decision(
            "s3", "i3", "h3", 1, 1, "blocked", now=10
        )
        self.ledger.transition_turn_decision(running["decision_id"], "pending", "enqueued", now=11)
        self.ledger.transition_turn_decision(running["decision_id"], "enqueued", "running", now=12)
        self.ledger.transition_turn_decision(terminal["decision_id"], "pending", "completed", now=12)
        self.assertEqual(
            [row["decision_id"] for row in self.ledger.recoverable_turn_decisions(limit=10)],
            [pending["decision_id"]],
        )
        self.ledger.retention_seconds = 5
        self.assertEqual(self.ledger.prune(now=20), 1)

    def test_recovery_query_returns_only_continue_outcomes(self):
        continuing = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1, 1, "continue", now=10
        )
        self.ledger.reserve_turn_decision(
            "s2", "i2", "h2", 1, 1, "blocked", now=11
        )

        self.assertEqual(
            [row["decision_id"] for row in self.ledger.recoverable_turn_decisions()],
            [continuing["decision_id"]],
        )

    def test_budget_rollover_count_survives_turn_decision_retention(self):
        row = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1000, 20, "continue", now=10
        )
        self.assertTrue(
            self.ledger.claim_budget_rollover(row["decision_id"], "s1", 1000, 2)
        )
        self.assertTrue(
            self.ledger.complete_budget_rollover(row["decision_id"], "s1", 1000)
        )
        self.ledger.transition_turn_decision(
            row["decision_id"], "pending", "completed", now=11
        )
        self.ledger.retention_seconds = 5

        self.ledger.prune(now=20)

        self.assertEqual(self.ledger.count_budget_rollovers("s1", 1000), 1)

    def test_continuation_admission_attempts_are_durable_and_bounded(self):
        row = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1000, 2, "continue", now=10
        )
        self.ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued", now=11
        )

        self.assertTrue(self.ledger.claim_turn_admission_attempt(row["decision_id"], 3))
        self.assertTrue(self.ledger.claim_turn_admission_attempt(row["decision_id"], 3))
        self.assertTrue(self.ledger.claim_turn_admission_attempt(row["decision_id"], 3))
        self.assertFalse(self.ledger.claim_turn_admission_attempt(row["decision_id"], 3))

        self.ledger.close()
        self.ledger = DeliveryLedger(self.path)
        self.assertFalse(self.ledger.claim_turn_admission_attempt(row["decision_id"], 3))
        self.assertEqual(
            self.ledger.turn_admission_attempts(row["decision_id"]), 3
        )

    def test_fence_pending_is_same_transactional_boundary_and_duplicate_safe(self):
        row = self.ledger.reserve_turn_decision(
            "linear-session", "issue-164", "h1", 1, 1, "continue", now=10
        )
        self.ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued", now=11)
        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stopped", now=12), 1)
        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stopped", now=13), 0)
        self.assertEqual(self.ledger.get_turn_decision(row["decision_id"])["dispatch_state"], "fenced")

    def test_authoritative_fence_also_stops_running_decision(self):
        row = self.ledger.reserve_turn_decision(
            "linear-session", "issue-164", "h1", 1, 2, "continue", now=10
        )
        self.ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued", now=11)
        self.ledger.transition_turn_decision(row["decision_id"], "enqueued", "running", now=12)

        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stop", now=13), 1)
        fenced = self.ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(fenced["dispatch_state"], "fenced")
        self.assertEqual(fenced["outcome"], "stopped")

    def test_disabled_startup_prune_does_not_mutate_continuation_state(self):
        row = self.ledger.reserve_turn_decision(
            "linear-session", "issue-164", "h1", 1, 2, "blocked", now=1
        )
        self.ledger.transition_turn_decision(
            row["decision_id"], "pending", "fenced", now=2
        )
        self.ledger.close()
        self.ledger = DeliveryLedger(
            str(Path(self.temp.name) / "ledger.sqlite3"),
            retention_seconds=1,
            startup_recovery=True,
            continuation_state_enabled=False,
        )

        self.assertIsNotNone(self.ledger.get_turn_decision(row["decision_id"]))


class NativeContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeGoalManager.instances.clear()
        FakeGoalManager.existing = False
        FakeGoalManager.before_evaluate = None
        FakeGoalManager.owner_checks = []
        FakeGoalManager.resume_calls = 0
        FakeGoalManager.resume_reset_budget = []
        FakeGoalManager.pause_calls = 0
        FakeGoalManager.existing_status = "active"
        FakeGoalManager.existing_turns = 2
        FakeGoalManager.existing_paused_reason = None
        FakeGoalManager.waiting = False
        FakeGoalManager.background_snapshots = []
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "NATIVE CANONICAL CONTINUATION",
            "verdict": "continue",
            "reason": "acceptance evidence missing",
            "message": "continuing",
        }
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.adapter = LinearPlatformAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "database_path": path,
                    "native_goal_continuation_enabled": True,
                },
            ),
            Platform.WEBHOOK,
        )
        self.adapter._ledger = DeliveryLedger(path)
        self.adapter._linear = FakeLinear()
        self.adapter._running = True
        self.admitted: list[MessageEvent] = []

        async def admit(event: MessageEvent) -> None:
            self.admitted.append(event)
            event._gateway_accepted = True

        self.adapter.handle_message = admit
        self.adapter.gateway_runner = fake_gateway_runner()
        self.drain_patch = mock.patch.object(
            self.adapter, "_drain_outbox_once", new=mock.AsyncMock(return_value=False)
        )
        self.drain_patch.start()

    async def asyncTearDown(self) -> None:
        self.drain_patch.stop()
        self.adapter._ledger.close()
        self.temp.cleanup()

    async def test_verified_blocker_dispatches_turkish_structured_notice(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )

        self.assertTrue(
            self.adapter._enqueue_turn_terminal_activity(
                row, "blocked", expected_state="pending", final_state="fenced"
            )
        )

        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        body = activity["payload"]["body"]
        self.assertIn("Etkilenen adım: devam teslimi.", body)
        self.assertIn("Devam durduruldu; ayrıntılı neden doğrulanamadı.", body)
        self.assertIn("Gerekli işlem:", body)
        self.assertNotIn("İnsan", body)
        self.assertNotIn("issue-164", body)
        self.assertNotIn("acceptance evidence missing", body)

    async def test_verified_stopped_dispatches_closed_session_notice(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )

        self.assertTrue(
            self.adapter._enqueue_turn_terminal_activity(
                row, "stopped", expected_state="pending", final_state="fenced"
            )
        )

        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        body = activity["payload"]["body"]
        self.assertIn("Etkilenen adım: devam teslimi.", body)
        self.assertIn("kesin terminal nedeni doğrulanamadı", body)
        self.assertIn("Gerekli işlem:", body)
        self.assertNotIn("İnsan", body)
        self.assertNotIn("issue-164", body)

    async def test_unknown_blocker_code_is_unverified_without_invented_details(self):
        event = turn_event()

        self.assertTrue(
            await self.adapter._visible_ingress_veto(
                event, "judge prose: secret actor should delete credentials"
            )
        )

        activity = self.adapter._ledger.get_outbox_item(
            self.adapter._ledger._db.execute(
                "SELECT id FROM outbox ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
        body = activity["payload"]["body"]
        self.assertIn("Devam durduruldu; ayrıntılı neden doğrulanamadı", body)
        self.assertIn("Sorumlu ajan: sorumlu ajan.", body)
        self.assertNotIn("secret actor", body)
        self.assertNotIn("delete credentials", body)
        self.assertNotIn("OPS-164", body)

    async def test_ingress_awaiting_input_does_not_forward_unverified_request(self):
        event = turn_event()
        event.raw_message = {
            "agentActivity": {
                "body": "Which deployment target should I use?",
            }
        }

        self.assertTrue(await self.adapter._visible_ingress_veto(event, "awaiting_input"))

        item = self.adapter._ledger.get_outbox_item(
            self.adapter._ledger._db.execute(
                "SELECT id FROM outbox ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(item["payload"]["activity_type"], "elicitation")
        self.assertNotIn("Which deployment target should I use?", item["payload"]["body"])
        self.assertIn("Gerekli işlem:", item["payload"]["body"])
        self.assertIn("teknik sorumlu ajan", item["payload"]["body"])
        self.assertIn("yetkili insan", item["payload"]["body"])
        self.assertNotIn("live safety", item["payload"]["body"])

    async def test_ingress_blocked_does_not_invent_human_request_and_deduplicates(self):
        event = turn_event()
        event.raw_message = {
            "agentActivity": {"body": "judge prose with secret PII"}
        }

        self.assertTrue(await self.adapter._visible_ingress_veto(event, "blocked"))
        self.assertTrue(await self.adapter._visible_ingress_veto(event, "unknown judge prose"))

        rows = self.adapter._ledger._db.execute(
            "SELECT payload_json FROM outbox"
        ).fetchall()
        rows = [row for row in rows if json.loads(row[0]).get("activity_type") == "error"]
        self.assertEqual(len(rows), 1)
        body = json.loads(rows[0][0])["body"]
        self.assertNotIn("secret PII", body)
        self.assertNotIn("judge prose", body)
        self.assertNotIn("İnsan isteği", body)

    async def test_terminal_elicitation_does_not_forward_raw_message_or_persist_it(self):
        fake_messages = {
            "approval": "Approve production? SECRET=judge-prose-PII-7",
            "awaiting_input": "Which target? raw judge prose with secret token-8",
        }
        for ordinal, (outcome, fake_message) in enumerate(fake_messages.items(), start=1):
            with self.subTest(outcome=outcome):
                row = self.adapter._ledger.reserve_turn_decision(
                    "linear-session", "issue-164", "hermes-session", 1, ordinal, "continue"
                )
                self.assertTrue(
                    self.adapter._enqueue_turn_terminal_activity(
                        row,
                        outcome,
                        fake_message,
                        expected_state="pending",
                        final_state="fenced",
                    )
                )
                activity = self.adapter._ledger.get_outbox_item(
                    f"activity:turn-decision:{row['decision_id']}"
                )
                self.assertEqual(activity["payload"]["activity_type"], "elicitation")
                self.assertNotIn(fake_message, activity["payload"]["body"])
                self.assertIn("Gerekli işlem:", activity["payload"]["body"])
                self.assertIn("teknik sorumlu ajan", activity["payload"]["body"])
                self.assertIn("yetkili insan", activity["payload"]["body"])
                stored = self.adapter._ledger.get_turn_decision(row["decision_id"])
                self.assertNotIn(fake_message, str(stored["error"] or ""))
                self.assertEqual(stored["error"], outcome)

    def test_continuation_uses_public_platform_callback_only(self):
        source_text = (PLUGIN_DIR / "adapter.py").read_text(encoding="utf-8")
        plugin_text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        combined = source_text + plugin_text
        for forbidden in (
            "_gateway_injection" + "_allowed",
            "_profile_runtime" + "_scope",
            "_resolve_profile_home" + "_for_source",
            "_profile_name" + "_for_source",
            "inject_" + "message(",
            "set_continuation_" + "injector",
            "set_continuation_injection_" + "allowed",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn("await admit_internal_event(self, event)", source_text)
        self.assertNotIn(".evaluate_after_turn(", source_text)
        self.assertNotIn("GoalManager", source_text)

    async def test_initial_goal_uses_source_scoped_gateway_goal_api(self):
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        store = FakeSessionStore()
        ensured = SimpleNamespace(
            status="active", created_at=123.0, turns_used=0,
            max_turns=20, paused_reason=None,
        )
        runner = SimpleNamespace(
            async_session_store=store,
            goal_state_for_source=mock.AsyncMock(return_value=None),
            ensure_goal_for_source=mock.AsyncMock(return_value=ensured),
            interrupt_session_processing=mock.AsyncMock(
                spec=GatewayAgentCacheMixin.interrupt_session_processing,
                return_value=True,
            ),
        )
        self.adapter.gateway_runner = runner
        event = turn_event()

        vetoed = await self.adapter._prepare_bound_linear_ingress(event)

        self.assertFalse(vetoed)
        runner.goal_state_for_source.assert_awaited_once_with(
            event.source, session_id="hermes-session"
        )
        runner.ensure_goal_for_source.assert_awaited_once()
        args, kwargs = runner.ensure_goal_for_source.await_args
        self.assertIs(args[0], event.source)
        self.assertIn("OPS-164", args[1])
        self.assertEqual(kwargs["session_id"], "hermes-session")
        self.assertIsInstance(kwargs["contract"], GoalContract)

    async def test_supported_hook_result_is_classified_at_adapter_send_boundary(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-1",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        result = await self.adapter.send("linear-session", "budget summary")

        self.assertTrue(result.success)
        self.assertEqual(self.admitted, [])
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)

    async def test_core_delivery_stages_until_native_judge_then_delivers_once(self):
        """The core calls response preparation before its native goal judge."""
        from gateway.platforms.base import BasePlatformAdapter

        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "active"
        FakeGoalManager.decision = {
            "status": "done",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "done",
            "reason": "evidence complete",
            "message": "",
        }
        event = turn_event()
        await self.adapter.on_processing_start(event)
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True, "turn_exit_reason": "completed"}
        )
        self.assertFalse(hasattr(event, "_gateway_post_turn_response"))
        response = await BasePlatformAdapter._response_after_delivery_decision(
            self.adapter, event, "final response with evidence"
        )

        self.assertIsNone(response)
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)
        from gateway.run_goals import GatewayGoalsMixin

        self.assertEqual(
            GatewayGoalsMixin._final_text_for_post_turn_hooks(
                event._gateway_turn_result, event
            ),
            "final response with evidence",
        )

        # Native core goal judging runs after response preparation and sees the original text.
        FakeGoalManager.existing_status = "done"
        self.adapter._linear.description = "## Acceptance\n- [x] tests pass\n- [x] restart is safe"
        self.assertTrue(
            self.adapter._acceptance_is_fully_checked(
                {"description": self.adapter._linear.description}
            )
        )
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-native-judge",
            completed=True,
            failed=False,
            interrupted=False,
            turn_exit_reason="completed",
        )
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "success")
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 1)

    async def test_structured_turn_hook_never_pauses_native_goal_owner(self):
        FakeGoalManager.existing = True
        event = turn_event(internal=True)
        await self.adapter.on_processing_start(event)

        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-1",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        self.assertEqual(FakeGoalManager.pause_calls, 0)

    async def test_ordinary_turn_never_calls_plugin_goal_judge_or_schedules(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-native-owned",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        await self.adapter.send("linear-session", "native loop owns this summary")

        self.assertEqual(self.admitted, [])
        self.assertEqual(FakeGoalManager.background_snapshots, [])
        self.assertEqual(FakeGoalManager.instances, [])

    async def test_final_classification_waits_until_native_goal_judge_completed(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-deferred",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        await self.adapter.send("linear-session", "deferred summary")
        self.assertEqual(self.adapter._ledger.list_turn_decisions("linear-session"), [])

        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "active"
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "continue")
        self.assertEqual(rows[0]["dispatch_state"], "completed")
        self.assertEqual(self.admitted, [])

    async def test_real_preparation_completion_preserves_continue_and_reasoned_block(self):
        """A capped turn is resumable while live, but a live error is terminal."""
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "active"
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session", hermes_session_id="hermes-session",
            turn_id="turn-regression-live", completed=False, failed=False,
            interrupted=False, turn_exit_reason="max_iterations_reached(90/90)",
        )
        await self.adapter.send("linear-session", "capped turn summary")
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        live = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["outcome"], "continue")
        self.assertEqual(
            self.adapter._ledger._db.execute(
                "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\\\"activity_type\\\":\\\"error\\\"%'"
            ).fetchone()[0],
            0,
        )

        self.adapter._linear.status = "error"
        FakeGoalManager.existing_turns = 3
        failed_event = turn_event()
        failed_event.message_id = "webhook-live-session-error"
        await self.adapter.on_processing_start(failed_event)
        self.adapter.record_completed_turn(
            chat_id="linear-session", hermes_session_id="hermes-session",
            turn_id="turn-regression-error", completed=False, failed=False,
            interrupted=False, turn_exit_reason="max_iterations_reached(90/90)",
        )
        await self.adapter.send("linear-session", "error summary")
        await self.adapter.on_processing_complete(failed_event, ProcessingOutcome.SUCCESS)

        blocked = [
            row for row in self.adapter._ledger.list_turn_decisions("linear-session")
            if row["decision_id"] != live[0]["decision_id"]
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["outcome"], "blocked")
        self.assertEqual(blocked[0]["error"], "session_error")
        terminal = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{blocked[0]['decision_id']}"
        )
        self.assertIn("session_error", terminal["payload"]["body"])

    async def test_unchecked_native_done_resumes_same_goal_without_budget_reset_once(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-unchecked",
            completed=True,
            failed=False,
            interrupted=False,
            turn_exit_reason="completed",
        )
        await self.adapter.send("linear-session", "native judge said done")
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"

        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        self.assertEqual(FakeGoalManager.resume_reset_budget, [False])
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].metadata["gateway_session_id"], "hermes-session")
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)

    async def test_budget_rollover_only_resets_budget_after_native_auto_pause(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-budget",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )
        await self.adapter.send("linear-session", "budget summary")
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"

        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        self.assertEqual(FakeGoalManager.resume_reset_budget, [True])
        self.assertEqual(len(self.admitted), 1)

    async def test_public_wake_receipt_is_required_for_continuation_admission(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        self.adapter.handle_message = mock.AsyncMock(return_value=None)

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unchecked done", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "enqueued")
        self.assertTrue(self.adapter._turn_recovery_requested)

    async def test_retryable_wake_admission_exhausts_durable_cap_before_fencing(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        self.adapter.handle_message = mock.AsyncMock(return_value=None)
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unchecked done", event._gateway_turn_result
        )
        decision_id = event._linear_turn_decision_id

        FakeGoalManager.existing_status = "active"
        await self.adapter._recover_turn_decisions()
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"],
            "enqueued",
        )
        await self.adapter._recover_turn_decisions()
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"],
            "fenced",
        )
        self.assertEqual(self.adapter._ledger.turn_admission_attempts(decision_id), 3)

    async def test_internal_wake_terminal_gate_vetoes_before_base_scheduling(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        event = self.adapter._continuation_event(
            source=source(), prompt="continue", decision=row
        )
        self.adapter._linear = FakeLinear(state_type="completed")

        with mock.patch.object(
            BasePlatformAdapter, "handle_message", new=mock.AsyncMock()
        ) as base_handle:
            await LinearPlatformAdapter.handle_message(self.adapter, event)

        base_handle.assert_not_awaited()
        self.assertTrue(event._gateway_accepted)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )

    async def test_recovery_preserves_running_decision_with_live_local_owner(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "enqueued", "running"
        )
        self.adapter._active_turn_events["linear-session"] = turn_event(internal=True)

        await self.adapter._recover_turn_decisions()

        recovered = self.adapter._ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(recovered["dispatch_state"], "running")

    async def test_staged_send_keeps_owner_alive_until_native_completion(self):
        FakeGoalManager.existing = True
        event = turn_event()
        await self.adapter.on_processing_start(event)
        decision = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            decision["decision_id"], "pending", "enqueued"
        )
        self.adapter._ledger.transition_turn_decision(
            decision["decision_id"], "enqueued", "running"
        )
        event.metadata["linear_continuation_decision_id"] = decision["decision_id"]
        event._linear_turn_decision_id = decision["decision_id"]
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-staged-owner",
            completed=True,
            failed=False,
            interrupted=False,
            turn_exit_reason="completed",
        )

        result = await self.adapter.send("linear-session", "native judged final")

        self.assertTrue(result.success)
        self.assertIn("linear-session", self.adapter._pending_turn_deliveries)
        await self.adapter._recover_turn_decisions()
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(decision["decision_id"])["dispatch_state"],
            "running",
        )

        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        self.assertNotIn("linear-session", self.adapter._pending_turn_deliveries)
        self.assertNotIn("linear-session", self.adapter._active_turn_events)

        crashed = self.adapter._ledger.reserve_turn_decision(
            "crashed-session", "issue-164", "crashed-hermes", 123000000, 3, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            crashed["decision_id"], "pending", "enqueued"
        )
        self.adapter._ledger.transition_turn_decision(
            crashed["decision_id"], "enqueued", "running"
        )
        await self.adapter._recover_turn_decisions()
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(crashed["decision_id"])["dispatch_state"],
            "fenced",
        )

    async def test_internal_wake_allows_active_native_goal_to_collect_evidence(self):
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "active"
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        decision = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            decision["decision_id"], "pending", "enqueued"
        )
        event = self.adapter._continuation_event(
            source=source(),
            prompt="collect durable evidence",
            decision=decision,
        )

        vetoed = await self.adapter._prepare_bound_linear_ingress(event)

        self.assertFalse(vetoed)


    async def test_internal_execution_hook_rechecks_metadata_light_native_wake(self):
        FakeGoalManager.existing = True
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        event = MessageEvent(text="native continuation", source=source(), internal=True)

        self.assertFalse(await self.adapter._prepare_bound_linear_ingress(event))
        self.adapter._linear = FakeLinear(state_type="completed")

        self.assertFalse(await self.adapter.allow_internal_execution(event))

    async def test_native_goal_status_notice_is_not_a_linear_response(self):
        notice = GoalStatusNotice(
            kind=GoalStatusNoticeKind.GOAL,
            status="continue",
            text="Continuing persistent goal.",
        )

        self.assertIsNone(
            await self.adapter.prepare_goal_status_notice(source(), notice)
        )

    async def test_real_base_lifecycle_creates_initial_goal_before_handler_execution(self):
        observed = []
        self.adapter.gateway_runner = fake_gateway_runner()
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")

        async def handler(event):
            observed.append(
                (
                    event.metadata.get("gateway_session_id"),
                    any(instance.set_calls for instance in FakeGoalManager.instances),
                )
            )
            return None

        self.adapter.set_message_handler(handler)
        event = turn_event()
        await LinearPlatformAdapter.handle_message(self.adapter, event)
        await __import__("asyncio").gather(*list(self.adapter._session_tasks.values()))

        self.assertEqual(observed, [("hermes-session", True)])

    async def test_real_base_lifecycle_stamps_and_gates_metadata_light_native_wake(self):
        FakeGoalManager.existing = True
        observed = []
        self.adapter.gateway_runner = fake_gateway_runner()
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")

        async def handler(event):
            observed.append(dict(event.metadata))
            return None

        self.adapter.set_message_handler(handler)
        event = MessageEvent(text="native continuation", source=source(), internal=True)
        await LinearPlatformAdapter.handle_message(self.adapter, event)
        await __import__("asyncio").gather(*list(self.adapter._session_tasks.values()))

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["linear_agent_session_id"], "linear-session")
        self.assertEqual(observed[0]["linear_issue_id"], "issue-164")
        self.assertEqual(observed[0]["gateway_session_id"], "hermes-session")
        self.assertIs(observed[0]["gateway_session_strict"], True)

    async def test_real_gateway_runner_resolves_stamped_native_wake_strictly(self):
        from gateway.run import GatewayRunner
        FakeGoalManager.existing = True
        store = FakeSessionStore()
        runner = object.__new__(GatewayRunner)
        runner.session_store = store._store
        runner._async_session_store = store
        runner._session_key_for_source = lambda _source: store.entry.session_key
        runner._cache_session_source = lambda *_args: None
        runner._is_telegram_topic_lane = lambda _source: False
        attach_fake_goal_api(runner)
        self.adapter.gateway_runner = runner
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        resolved = []

        async def handler(event):
            resolved.append(await runner._hmwa_resolve_session(event, event.source))
            return None

        self.adapter.set_message_handler(handler)
        event = MessageEvent(text="native continuation", source=source(), internal=True)
        await LinearPlatformAdapter.handle_message(self.adapter, event)
        await __import__("asyncio").gather(*list(self.adapter._session_tasks.values()))

        self.assertEqual(resolved[0][1].session_id, "hermes-session")
        self.assertEqual(event.metadata["gateway_session_id"], "hermes-session")

    async def test_real_gateway_runner_post_turn_judges_ingress_created_goal(self):
        from gateway.run import GatewayRunner
        store = FakeSessionStore()
        runner = object.__new__(GatewayRunner)
        runner.session_store = store._store
        runner._async_session_store = store
        runner._goal_max_turns_from_config = lambda: 20
        runner._warm_goals_session_db = mock.AsyncMock(return_value=None)
        attach_fake_goal_api(runner)

        async def run_in_context(call):
            return call()

        runner._run_in_executor_with_context = run_in_context
        self.adapter.gateway_runner = runner
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        FakeGoalManager.decision = {
            "status": "done",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "done",
            "reason": "evidence complete",
            "message": "",
        }
        judged = []

        async def handler(event):
            manager = next(instance for instance in FakeGoalManager.instances if instance.state)
            with mock.patch(
                "hermes_cli.goals.GoalManager",
                side_effect=lambda **_kwargs: manager,
            ):
                await runner._post_turn_goal_continuation(
                    session_entry=store.entry,
                    source=event.source,
                    final_response="evidence-complete result",
                )
            judged.append(manager.state.status)
            return None

        self.adapter.set_message_handler(handler)
        await LinearPlatformAdapter.handle_message(self.adapter, turn_event())
        await __import__("asyncio").gather(*list(self.adapter._session_tasks.values()))

        self.assertEqual(judged, ["done"])

    def test_fake_goal_manager_owner_check_fences_stale_evaluation(self):
        manager = FakeGoalManager("hermes-session")
        manager.state = SimpleNamespace(
            status="active", created_at=123.0, turns_used=2,
            max_turns=20, paused_reason=None,
        )
        callback = mock.Mock(return_value=False)

        decision = manager.evaluate_after_turn(
            "stale response", user_initiated=True, owner_check=callback
        )

        self.assertIs(FakeGoalManager.owner_checks[-1], callback)
        self.assertEqual(decision["status"], None)
        self.assertEqual(decision["verdict"], "stale")
        self.assertTrue(decision["stale_owner"])
        self.assertEqual(manager.state.turns_used, 2)
        self.assertEqual(FakeGoalManager.background_snapshots, [])

    async def test_real_base_lifecycle_strict_mismatch_is_visible_and_never_executes(self):
        FakeGoalManager.existing = True
        self.adapter.gateway_runner = fake_gateway_runner(
            FakeSessionStore("current-hermes-session")
        )
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        handler = mock.AsyncMock(return_value=None)
        self.adapter.set_message_handler(handler)
        event = MessageEvent(
            text="stale continuation",
            source=source(),
            internal=True,
            metadata={"gateway_session_id": "stale-hermes-session"},
        )

        await LinearPlatformAdapter.handle_message(self.adapter, event)
        await __import__("asyncio").sleep(0)

        handler.assert_not_awaited()
        errors = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%strict_session_mismatch%'"
        ).fetchone()[0]
        self.assertEqual(errors, 1)

    async def test_real_base_race_fence_aborts_before_handler_execution(self):
        FakeGoalManager.existing = True
        self.adapter.gateway_runner = fake_gateway_runner()
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        event = self.adapter._continuation_event(
            source=source(), prompt="continue", decision=row
        )
        handler = mock.AsyncMock(return_value=None)
        self.adapter.set_message_handler(handler)
        original_hook = self.adapter.on_processing_start

        async def fence_then_start(started_event):
            self.adapter._ledger.fence_turn_decisions("linear-session", "racing cancellation")
            return await original_hook(started_event)

        self.adapter.on_processing_start = fence_then_start
        await LinearPlatformAdapter.handle_message(self.adapter, event)
        await __import__("asyncio").gather(
            *list(self.adapter._session_tasks.values()), return_exceptions=True
        )

        handler.assert_not_awaited()
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )

    def test_acceptance_requires_named_h2_and_ignores_other_task_lists(self):
        accepted = {
            "description": "# Work\n- [x] unrelated\n\n## Acceptance\n- [x] evidence\n\n## Notes\n- [ ] later"
        }
        unrelated_only = {"description": "# Work\n- [x] unrelated task"}
        malformed = {"description": "### Acceptance\n- [x] evidence"}

        self.assertTrue(self.adapter._acceptance_is_fully_checked(accepted))
        self.assertFalse(self.adapter._acceptance_is_fully_checked(unrelated_only))
        self.assertFalse(self.adapter._acceptance_is_fully_checked(malformed))

    def test_quoted_heading_does_not_hide_unchecked_acceptance(self):
        issue = {"description": "## Acceptance\n- [x] completed\n\n> ## Notes\n\n- [ ] required but unfinished\n"}
        self.assertEqual(len(self.adapter._acceptance_checkbox_matches(issue)), 2)
        self.assertFalse(self.adapter._acceptance_is_fully_checked(issue))

    def test_acceptance_nested_fenced_source_brief_uses_only_live_criteria(self):
        description = """## Previous description
````markdown
## Kabul kriterleri
- [ ] old fenced criterion one
- [ ] old fenced criterion two

```text
## Kabul kriterleri
- [ ] inner fenced criterion
```
````

## Kabul kriterleri
- [X] current criterion one
- [x] current criterion two
- [X] current criterion three
- [x] current criterion four
- [X] current criterion five
- [x] current criterion six
- [X] current criterion seven
"""
        issue = {"description": description}

        self.assertEqual(len(self.adapter._acceptance_checkbox_matches(issue)), 7)
        self.assertTrue(self.adapter._acceptance_is_fully_checked(issue))

    async def test_disabled_restart_recovery_and_fencing_are_inert(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.adapter._native_goal_continuation_enabled = False
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()

        await self.adapter._recover_turn_decisions()
        stopped = await self.adapter._stop_bound_turns("issue-164", "disabled stop")

        self.assertFalse(stopped)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "enqueued",
        )
        self.assertEqual(self.admitted, [])
        self.adapter._cancel_linear_session_processing.assert_not_awaited()

    async def test_unchecked_done_recovery_before_resume_is_effectively_once(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue",
            source=self.adapter._source_snapshot(source()),
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "pending", error="unchecked_acceptance"
        )
        self.adapter._ledger.mark_goal_resume_required(row["decision_id"])

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_reset_budget, [False])
        self.assertEqual(self.adapter._ledger.goal_resume_phase(row["decision_id"]), "resume_applied")
        self.assertEqual(len(self.admitted), 1)

    async def test_unchecked_done_recovery_after_resume_does_not_resume_again(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "active"
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue",
            source=self.adapter._source_snapshot(source()),
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "pending", error="unchecked_acceptance"
        )
        self.adapter._ledger.mark_goal_resume_required(row["decision_id"])

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_reset_budget, [])
        self.assertEqual(self.adapter._ledger.goal_resume_phase(row["decision_id"]), "resume_applied")
        self.assertEqual(len(self.admitted), 1)

    async def test_internal_wake_live_read_failure_is_retryable_not_terminal_veto(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 3, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        event = self.adapter._continuation_event(
            source=source(), prompt="continue", decision=row
        )
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=LinearAPIError("temporary", retryable=True)
        )

        with self.assertRaises(LinearAPIError):
            await LinearPlatformAdapter.handle_message(self.adapter, event)

        self.assertFalse(event._gateway_accepted)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "enqueued",
        )

    async def test_missing_supported_hook_result_fails_closed_before_final_response(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)

        result = await self.adapter.send("linear-session", "must not leak as final")

        self.assertFalse(result.success)
        self.assertIn("structured turn result", result.error)
        outbox_count = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox"
        ).fetchone()[0]
        self.assertEqual(outbox_count, 0)

    async def test_goal_state_read_failure_persists_blocked_decision(self):
        self.adapter.gateway_runner.goal_state_for_source = mock.AsyncMock(
            side_effect=RuntimeError("goal database unavailable")
        )
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not escape", event._gateway_turn_result
        )

        self.assertIsNone(result)
        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "blocked")
        self.assertEqual(rows[0]["dispatch_state"], "completed")

    async def test_disabled_feature_fails_before_goal_or_decision_mutation(self):
        self.adapter._native_goal_continuation_enabled = False
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "ordinary response", event._gateway_turn_result
        )

        self.assertEqual(result, "ordinary response")
        self.assertEqual(FakeGoalManager.instances, [])
        decision_count = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM turn_decisions"
        ).fetchone()[0]
        self.assertEqual(decision_count, 0)

    async def test_callback_failure_remains_recoverable_until_bounded_retry(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter.handle_message = mock.AsyncMock(
            side_effect=RuntimeError("callback unavailable")
        )
        event = turn_event()

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "enqueued")
        self.assertEqual(self.adapter._ledger.turn_admission_attempts(row["decision_id"]), 1)
        self.assertTrue(self.adapter._turn_recovery_requested)

    async def test_callback_return_is_only_scheduling_until_processing_start(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )
        continuation = self.admitted[0]
        row = self.adapter._ledger.get_turn_decision(
            continuation.metadata["linear_continuation_decision_id"]
        )
        self.assertEqual(row["dispatch_state"], "enqueued")

        self.assertTrue(await self.adapter.on_processing_start(continuation))
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "running",
        )

    async def test_max_iteration_summary_becomes_ephemeral_thought_without_plugin_prompt(self):
        FakeGoalManager.existing = True
        event = turn_event()
        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "summary that must not become a response", event._gateway_turn_result
        )
        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        decision = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        outbox = self.adapter._ledger.get_outbox_item(
            f"activity:turn-summary:{decision['decision_id']}"
        )
        self.assertEqual(outbox["payload"]["activity_type"], "thought")
        self.assertTrue(outbox["payload"]["ephemeral"])
        self.assertEqual(decision["dispatch_state"], "completed")

    async def test_documented_delivery_boundary_routes_structured_turn_result(self):
        FakeGoalManager.existing = True
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "summary that must not become a response", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)["dispatch_state"],
            "completed",
        )

    def test_linear_policy_owned_delivery_disables_response_streaming(self):
        self.assertIs(self.adapter.supports_response_streaming, False)

    async def test_inflight_recovery_requests_followup_pass_for_new_enqueued_decision(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter.handle_message = mock.AsyncMock(return_value=None)
        self.adapter._turn_recovery_task = __import__("asyncio").current_task()
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "summary", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "enqueued")

    async def test_partial_stream_metadata_cannot_bypass_failed_turn_classification(self):
        event = turn_event()
        event.metadata["streamed"] = True
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "failed": True}
        )

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "undelivered terminal error", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_non_budget_incomplete_turn_fails_closed(self):
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "turn_exit_reason": "unknown_incomplete"}
        )

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_goal_pause_blocks_and_emits_visible_error_without_admission(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_paused_reason = "judged unachievable: blocked"
        event = turn_event()
        result = await self.adapter._prepare_native_owned_turn_delivery(event, "unfinished", event._gateway_turn_result)
        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(rows[-1]["outcome"], "blocked")
        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{rows[-1]['decision_id']}"
        )
        self.assertEqual(activity["payload"]["activity_type"], "error")

    async def test_budget_pause_rolls_over_same_native_goal_after_fresh_linear_gates(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"
        self.adapter._goal_max_budget_rollovers = 2
        context = await FakeLinear().get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[context, context]
        )
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(self.adapter._linear.get_agent_turn_context.await_count, 2)
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].text, "NATIVE CANONICAL CONTINUATION")
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["budget_rollover"], 1)

    async def test_budget_rollover_cap_fails_closed_without_resuming(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"
        self.adapter._goal_max_budget_rollovers = 0
        event = turn_event()

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertEqual(FakeGoalManager.resume_calls, 0)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_native_process_wait_remains_native_and_is_not_replayed_by_plugin(self):
        FakeGoalManager.existing = True
        FakeGoalManager.waiting = True
        event = turn_event()

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "waiting for process", event._gateway_turn_result
        )

        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "completed")
        self.assertIsNone(row["error"])
        self.assertFalse(self.adapter._turn_recovery_requested)
        self.assertEqual(FakeGoalManager.background_snapshots, [])
        await self.adapter._recover_turn_decisions()

        self.assertEqual(self.admitted, [])

    async def test_completed_turn_with_checked_acceptance_allows_true_final_response(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "final evidence", event._gateway_turn_result
        )
        self.assertIsNone(result)
        decision = self.adapter._ledger.list_turn_decisions("linear-session")[-1]
        self.assertEqual(decision["outcome"], "success")
        self.assertEqual(decision["dispatch_state"], "completed")
        outbox = self.adapter._ledger.get_outbox_item(
            f"activity:turn-success:{decision['decision_id']}"
        )
        self.assertEqual(outbox["payload"]["activity_type"], "response")
        self.assertEqual(outbox["payload"]["body"], "final evidence")
        duplicate = await self.adapter._prepare_native_owned_turn_delivery(
            event, "final evidence", event._gateway_turn_result
        )
        self.assertIsNone(duplicate)
        self.assertEqual(
            self.adapter._ledger.get_outbox_item(
                f"activity:turn-success:{decision['decision_id']}"
            )["payload"]["body"],
            "final evidence",
        )
        self.assertGreaterEqual(len(FakeGoalManager.instances), 1)

    async def test_immediate_success_rechecks_matching_authoritative_hermes_session(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        store = self.adapter.gateway_runner.async_session_store
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "accepted evidence", event._gateway_turn_result
        )

        decision = self.adapter._ledger.get_turn_decision(
            event._linear_turn_decision_id
        )
        self.assertEqual(decision["dispatch_state"], "completed")
        self.assertEqual(
            store.lookup_keys,
            ["agent:main:webhook:dm:linear-session"],
        )
        self.assertIsNotNone(
            self.adapter._ledger.get_outbox_item(
                f"activity:turn-success:{decision['decision_id']}"
            )
        )

    async def test_immediate_success_fences_hermes_session_rotation_without_response(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        store = self.adapter.gateway_runner.async_session_store

        async def rotate_before_immediate_reservation(_session_id):
            if self.adapter._linear.get_agent_turn_context.await_count == 2:
                store.entry.session_id = "rotated-hermes-session"
            return checked

        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=rotate_before_immediate_reservation
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not become response", event._gateway_turn_result
        )

        decision = self.adapter._ledger.get_turn_decision(
            event._linear_turn_decision_id
        )
        self.assertEqual(decision["dispatch_state"], "fenced")
        self.assertEqual(
            self.adapter._ledger._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            0,
        )

    async def test_success_revalidates_all_live_gates_immediately_before_response(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        delegate_removed = {
            **checked,
            "issue": {**checked["issue"], "delegate": {"id": "someone-else"}},
        }
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[checked, delegate_removed]
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not become response", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(self.adapter._linear.get_agent_turn_context.await_count, 2)
        self.assertEqual(row["outcome"], "stopped")
        responses = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(responses, 0)

    async def test_delayed_success_outbox_revalidates_before_linear_dispatch(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[checked, checked]
        )
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "accepted evidence", event._gateway_turn_result
        )
        drifted = {
            **checked,
            "issue": {**checked["issue"], "delegate": {"id": "someone-else"}},
        }
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value=drifted
        )

        self.assertTrue(
            await LinearPlatformAdapter._drain_outbox_once(self.adapter)
        )

        self.adapter._linear.create_activity.assert_not_awaited()
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "fenced")
        self.assertNotEqual(row["outcome"], "success")

    async def test_delayed_success_revalidation_read_failure_retains_retry(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[checked, checked]
        )
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "accepted evidence", event._gateway_turn_result
        )
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=LinearAPIError("temporary timeout", retryable=True)
        )

        self.assertTrue(await LinearPlatformAdapter._drain_outbox_once(self.adapter))

        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-success:{event._linear_turn_decision_id}"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "pending")
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "success")
        self.assertEqual(row["dispatch_state"], "completed")

    async def test_delayed_success_rechecks_matching_authoritative_hermes_session(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value=checked
        )
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        store = self.adapter.gateway_runner.async_session_store
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "accepted evidence", event._gateway_turn_result
        )

        self.assertTrue(await LinearPlatformAdapter._drain_outbox_once(self.adapter))

        self.adapter._linear.create_activity.assert_awaited_once()
        self.assertEqual(
            store.lookup_keys,
            [
                "agent:main:webhook:dm:linear-session",
                "agent:main:webhook:dm:linear-session",
            ],
        )

    async def test_delayed_success_fences_hermes_session_rotation_without_dispatch(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value=checked
        )
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        self.adapter._validate_activity_target = mock.AsyncMock(return_value=None)
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not dispatch", event._gateway_turn_result
        )
        self.adapter.gateway_runner.async_session_store.entry.session_id = (
            "rotated-hermes-session"
        )

        self.assertTrue(await LinearPlatformAdapter._drain_outbox_once(self.adapter))

        self.adapter._linear.create_activity.assert_not_awaited()
        decision = self.adapter._ledger.get_turn_decision(
            event._linear_turn_decision_id
        )
        self.assertEqual(decision["dispatch_state"], "fenced")
        terminal_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE state != 'delivered'"
        ).fetchone()[0]
        self.assertEqual(terminal_rows, 0)

    async def test_terminal_activity_and_decision_complete_without_second_cas(self):
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "failed": True, "turn_exit_reason": "failed"}
        )
        original = self.adapter._ledger.transition_turn_decision

        def reject_terminal_cas(decision_id, expected, new, **kwargs):
            if new == "completed":
                raise AssertionError("terminal persistence used a second transaction")
            return original(decision_id, expected, new, **kwargs)

        with mock.patch.object(
            self.adapter._ledger,
            "transition_turn_decision",
            side_effect=reject_terminal_cas,
        ):
            await self.adapter._prepare_native_owned_turn_delivery(
                event, "generic failure text", event._gateway_turn_result
            )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "completed")
        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertNotEqual(item["payload"]["body"], "generic failure text")

    async def test_unknown_workflow_state_cannot_authorize_success(self):
        self.adapter._linear = FakeLinear(
            state_type="",
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe",
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not deliver", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(
            self.adapter._ledger.list_turn_decisions("linear-session")[-1]["outcome"],
            "blocked",
        )

    async def test_native_done_with_unchecked_acceptance_resumes_without_response(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "blocked", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "continue")
        self.assertEqual(row["dispatch_state"], "enqueued")
        self.assertEqual(FakeGoalManager.resume_reset_budget, [False])
        self.assertEqual(len(self.admitted), 1)

    async def test_native_done_block_reason_never_succeeds_with_checked_acceptance(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_paused_reason = "judged unachievable: needs input"
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "blocked", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_decision_is_persisted_without_plugin_goal_evaluation(self):
        FakeGoalManager.existing = True
        event = turn_event()

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatch_state"], "completed")
        self.assertEqual(FakeGoalManager.background_snapshots, [])

    async def test_awaiting_input_and_approval_use_elicitation_and_never_continue(self):
        for reason, expected in (("awaiting_input", "awaiting_input"), ("approval", "approval")):
            with self.subTest(reason=reason):
                self.adapter._linear = FakeLinear(status="awaitingInput")
                event = turn_event()
                event.message_id = f"webhook-{reason}"
                event._gateway_turn_result = MappingProxyType(
                    {**dict(event._gateway_turn_result), "turn_exit_reason": reason}
                )
                result = await self.adapter._prepare_native_owned_turn_delivery(event, "not final", event._gateway_turn_result)
                self.assertIsNone(result)
                row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
                self.assertEqual(row["outcome"], expected)
                item = self.adapter._ledger.get_outbox_item(
                    f"activity:turn-decision:{row['decision_id']}"
                )
                self.assertEqual(item["payload"]["activity_type"], "elicitation")
        self.assertEqual(self.admitted, [])

    async def test_processing_hooks_mark_running_then_completed(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()
        await self.adapter._prepare_native_owned_turn_delivery(event, "unfinished", event._gateway_turn_result)
        continuation = self.admitted[0]
        decision_id = continuation.metadata["linear_continuation_decision_id"]
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "enqueued")
        await self.adapter.on_processing_start(continuation)
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "running")
        await self.adapter.on_processing_complete(continuation, ProcessingOutcome.SUCCESS)
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "completed")

    async def test_fenced_continuation_is_rejected_before_handler_execution(self):
        event = turn_event(internal=True, decision_id="fenced-decision")
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        event.metadata["linear_continuation_decision_id"] = row["decision_id"]
        self.adapter._ledger.fence_turn_decisions("linear-session", "stop")

        with self.assertRaises(__import__("asyncio").CancelledError):
            await self.adapter.on_processing_start(event)

    async def test_restart_recovery_uses_real_admit_hook_and_native_prompt(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        FakeGoalManager.existing = True
        await self.adapter._recover_turn_decisions()
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].text, "NATIVE CANONICAL CONTINUATION")
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])

    async def test_restart_recovery_preserves_exact_public_session_source(self):
        original = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="linear-session",
            user_id="human-1",
            user_name="Human",
            chat_name="OPS-164",
            chat_type="dm",
            thread_id="thread-9",
            scope_id="org-scope",
            profile="researcher",
        )
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session",
            "issue-164",
            "hermes-session",
            123000000,
            2,
            "continue",
            source=self.adapter._source_snapshot(original),
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        FakeGoalManager.existing = True

        await self.adapter._recover_turn_decisions()

        self.assertEqual(self.admitted[0].source, original)
        self.assertEqual(
            self.admitted[0].metadata["gateway_session_id"], "hermes-session"
        )

    async def test_restart_recovery_completes_reserved_budget_rollover_exactly_once(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 20, "continue"
        )
        self.assertTrue(
            self.adapter._ledger.claim_budget_rollover(
                row["decision_id"], "linear-session", 123000000, 3
            )
        )
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "enqueued",
        )

    async def test_restart_recovery_claims_pause_persisted_before_rollover_marker(self):
        self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 20, "continue"
        )
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(
            self.adapter._ledger.count_budget_rollovers("linear-session", 123000000),
            1,
        )
        self.assertEqual(len(self.admitted), 1)

    async def test_recovery_applies_terminal_gate_before_wait_or_rollover_mutation(self):
        wait_row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 1, "continue"
        )
        self.assertTrue(
            self.adapter._ledger.mark_pending_process_wait(wait_row["decision_id"])
        )
        self.adapter._linear = FakeLinear(state_type="completed")
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 1
        FakeGoalManager.waiting = True

        await self.adapter._recover_turn_decisions()

        self.assertEqual(self.admitted, [])
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(wait_row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.assertEqual(FakeGoalManager.resume_calls, 0)
        self.assertEqual(
            self.adapter._ledger.count_budget_rollovers("linear-session", 123000000),
            0,
        )

    async def test_parked_goal_reuses_current_ordinal_without_consuming_a_turn(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 7
        FakeGoalManager.waiting = True
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "wait",
            "reason": "session proc-123",
            "message": "native goal parked",
        }
        event = turn_event()

        await self.adapter._prepare_native_owned_turn_delivery(
            event, "still waiting", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["ordinal"], 7)
        goal_state = FakeGoalManager.instances[-1].state
        assert goal_state is not None
        self.assertEqual(goal_state.turns_used, 7)
        self.assertEqual(row["dispatch_state"], "completed")
        self.assertIsNone(row["error"])

    async def test_zero_turn_parked_goal_is_not_recovered_by_plugin(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 0
        FakeGoalManager.waiting = True
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "wait",
            "reason": "session proc-123",
            "message": "native goal parked",
        }
        event = turn_event()
        await self.adapter._prepare_native_owned_turn_delivery(
            event, "waiting", event._gateway_turn_result
        )
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["ordinal"], 0)

        await self.adapter._recover_turn_decisions()

        self.assertEqual(row["dispatch_state"], "completed")
        self.assertEqual(self.admitted, [])

    async def test_restart_fences_running_decision_after_visible_error(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 9, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "enqueued", "running")

        await self.adapter._recover_turn_decisions()

        recovered = self.adapter._ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(recovered["dispatch_state"], "fenced")
        self.assertEqual(recovered["error"], "blocked")

    async def test_completed_issue_fences_turn_instead_of_delivering_success(self):
        self.adapter._linear = FakeLinear(state_type="completed")
        event = turn_event()

        result = await self.adapter._prepare_native_owned_turn_delivery(
            event, "must not be delivered", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "stopped")

    async def test_duplicate_prepare_callback_admits_once(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "done"
        event = turn_event()
        await self.adapter._prepare_native_owned_turn_delivery(event, "unfinished", event._gateway_turn_result)
        await self.adapter._prepare_native_owned_turn_delivery(event, "unfinished", event._gateway_turn_result)
        self.assertEqual(len(self.admitted), 1)

    async def test_historical_decision_failure_does_not_degrade_health(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "blocked"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "completed")
        response = await self.adapter._health(None)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text.count('"status": "ok"'), 1)

    async def test_stop_webhook_fences_enqueued_decision_before_dispatch(self):
        self.adapter._signing_secrets = ("s" * 32,)
        self.adapter._data_change_events_enabled = False
        self.adapter._planned_activation_enabled = False
        self.adapter._dependency_wait_enabled = False
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._activation_allowed_team_ids = set()
        self.adapter._planned_owner_ids = set()
        self.adapter.handle_message = mock.AsyncMock()
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        payload = {
            "type": "AgentSessionEvent",
            "action": "prompted",
            "webhookId": "webhook-stop-ops164",
            "webhookTimestamp": int(__import__("time").time() * 1000),
            "organizationId": "org",
            "actor": {"id": "human-1", "name": "Human"},
            "agentActivity": {
                "id": "activity-stop-ops164",
                "signal": "stop",
                "body": "stop",
            },
            "agentSession": {
                "id": "linear-session",
                "issue": {
                    "id": "issue-164",
                    "identifier": "OPS-164",
                    "title": "Native continuation",
                },
            },
        }
        response = await self.adapter._handle_webhook(FakeRequest(payload))
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter.handle_message.assert_awaited_once()
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_new_human_prompt_fences_stale_continuation_before_dispatch(self):
        self.adapter._signing_secrets = ("s" * 32,)
        self.adapter._data_change_events_enabled = False
        self.adapter._planned_activation_enabled = False
        self.adapter._dependency_wait_enabled = False
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._activation_allowed_team_ids = set()
        self.adapter._planned_owner_ids = set()
        self.adapter.handle_message = mock.AsyncMock()
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        payload = {
            "type": "AgentSessionEvent",
            "action": "prompted",
            "webhookId": "webhook-human-preempt-ops164",
            "webhookTimestamp": int(__import__("time").time() * 1000),
            "organizationId": "org",
            "actor": {"id": "human-1", "name": "Human"},
            "agentActivity": {
                "id": "activity-human-preempt-ops164",
                "signal": "prompt",
                "body": "new instruction",
            },
            "agentSession": {
                "id": "linear-session",
                "status": "active",
                "issue": {
                    "id": "issue-164",
                    "identifier": "OPS-164",
                    "title": "Native continuation",
                },
            },
        }

        response = await self.adapter._handle_webhook(FakeRequest(payload))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )
        self.adapter.handle_message.assert_awaited_once()

    async def test_cancel_interrupts_runner_before_releasing_adapter_lane(self):
        self.adapter.cancel_session_processing = mock.AsyncMock()

        await self.adapter._cancel_linear_session_processing("linear-session")

        self.adapter.gateway_runner.interrupt_session_processing.assert_awaited_once()
        interrupt_source = self.adapter.gateway_runner.interrupt_session_processing.call_args.args[0]
        interrupt_kwargs = self.adapter.gateway_runner.interrupt_session_processing.call_args.kwargs
        self.assertEqual(interrupt_source.chat_id, "linear-session")
        self.assertEqual(interrupt_kwargs["reason"], "linear_authoritative_stop")
        self.assertIsNone(interrupt_kwargs["expected_session_id"])
        session_key = self.adapter.cancel_session_processing.await_args.args[0]
        self.adapter.cancel_session_processing.assert_awaited_once_with(session_key)

    async def test_cancel_pins_active_hermes_session_identity(self):
        self.adapter.cancel_session_processing = mock.AsyncMock()
        event = turn_event()
        event.metadata["gateway_session_id"] = "hermes-session"
        self.adapter._active_turn_events["linear-session"] = event

        await self.adapter._cancel_linear_session_processing("linear-session")

        call = self.adapter.gateway_runner.interrupt_session_processing.await_args
        self.assertIs(call.args[0], event.source)
        self.assertEqual(call.kwargs["expected_session_id"], "hermes-session")

    async def test_bound_blocker_fences_and_cancels_without_dependency_wait_feature(self):
        self.adapter._dependency_wait_enabled = False
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        self.adapter._linear.get_open_blockers = mock.AsyncMock(
            return_value=[{"id": "blocker-1"}]
        )
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()

        stopped = await self.adapter._stop_bound_turns_if_blocked("issue-164")

        self.assertTrue(stopped)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_bound_blocker_cancels_initial_turn_without_decision_row(self):
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.adapter._linear.get_open_blockers = mock.AsyncMock(
            return_value=[{"id": "blocker-1"}]
        )
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()

        stopped = await self.adapter._stop_bound_turns_if_blocked("issue-164")

        self.assertTrue(stopped)
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_rejected_strict_session_fences_running_decision_with_visible_error(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "enqueued", "running"
        )
        event = turn_event(internal=True, decision_id=row["decision_id"])
        event.metadata["gateway_session_rejected"] = "strict_session_mismatch"

        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

        fenced = self.adapter._ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(fenced["dispatch_state"], "fenced")
        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        self.assertEqual(activity["payload"]["activity_type"], "error")

    async def test_failure_completion_keeps_turn_fence_until_generic_error_send(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)

        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
        self.assertIn("linear-session", self.adapter._active_turn_events)
        result = await self.adapter.send(
            "linear-session", "Hermes encountered an unexpected processing error."
        )

        self.assertFalse(result.success)
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox "
            "WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)


class BlockerPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_blockers_exhausts_pages_and_checks_identity_consistency(self):
        client = LinearClient("/unused")
        calls: list[dict] = []

        async def graphql(_query, variables=None):
            calls.append(dict(variables or {}))
            if variables.get("after") is None:
                return {
                    "issue": {
                        "id": "issue-164",
                        "inverseRelations": {
                            "nodes": [{
                                "type": "blocks",
                                "issue": {"id": "b1", "identifier": "OPS-1", "title": "one", "state": {"name": "Todo", "type": "unstarted"}},
                            }],
                            "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                        },
                    }
                }
            return {
                "issue": {
                    "id": "issue-164",
                    "inverseRelations": {
                        "nodes": [{
                            "type": "blocks",
                            "issue": {"id": "b2", "identifier": "OPS-2", "title": "two", "state": {"name": "Todo", "type": "started"}},
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }

        client.graphql = graphql
        blockers = await client.get_open_blockers("issue-164")
        self.assertEqual([item["id"] for item in blockers], ["b1", "b2"])
        self.assertEqual(calls, [{"id": "issue-164", "after": None}, {"id": "issue-164", "after": "page-2"}])

    async def test_open_blockers_fails_closed_on_identity_change(self):
        client = LinearClient("/unused")
        client.graphql = mock.AsyncMock(side_effect=[
            {"issue": {"id": "issue-164", "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"issue": {"id": "other", "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ])
        with self.assertRaisesRegex(LinearAPIError, "identity changed"):
            await client.get_open_blockers("issue-164")


if __name__ == "__main__":
    unittest.main()
