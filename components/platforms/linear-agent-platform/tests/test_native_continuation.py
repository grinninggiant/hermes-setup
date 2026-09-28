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
    MessageEvent,
    MessageType,
)

try:
    from gateway.platforms.base import GoalStatusNotice, GoalStatusNoticeKind
except ImportError:  # Upstream cores carry no platform goal-status seam.
    GoalStatusNotice = GoalStatusNoticeKind = None
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

    async def verify_response_receipt(self, activity_id, session_id, body):
        # This fixture's vendor create is an AsyncMock installed by each test;
        # strict vendor shape/ownership validation has its own client tests.
        create = getattr(self, "create_activity", None)
        return any(
            call.args == (session_id, "response", body)
            and call.kwargs.get("activity_id") == activity_id
            for call in getattr(create, "await_args_list", ())
        )

    async def get_agent_session_delivery_context(self, session_id: str) -> dict:
        context = await self.get_agent_turn_context(session_id)
        issue = context["issue"]
        return {
            "id": session_id, "app_user_id": context["app_user_id"],
            "issue_id": issue["id"], "updated_at": issue["updatedAt"],
            "description": issue["description"], "delegate_id": issue["delegate"]["id"],
        }

    async def get_agent_turn_context(self, session_id: str) -> dict:
        return {
            "id": session_id,
            "status": self.status,
            "app_user_id": self.actor_id,
            "issue": {
                "id": "issue-164",
                "identifier": "OPS-164",
                "title": "Native continuation",
                "updatedAt": "2026-08-30T20:00:00.000Z",
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
                return_value=True,
            ),
            _profile_name_for_source=lambda _source, **_kw: None,
    )
    return attach_fake_goal_api(runner)


def record_acceptance_fixture(adapter, description=None):
    """Offline PASS metadata for the verified-delivery fixtures, never live evidence."""
    description = description or "## Acceptance\n- [x] tests pass\n- [x] restart is safe"
    for criterion in adapter_mod.acceptance_criteria(description):
        adapter._ledger.record_acceptance_evidence(
            issue_id="issue-164", actor_id="app-user", criterion_hash=criterion.criterion_hash,
            test_class="integration", evidence_digest="b" * 64,
            evidence_pointer="artifact://offline-test-fixture",
            observed_revision="2026-08-30T20:00:00.000Z",
            accepted_revision="2026-08-30T20:00:00.000Z",
            timestamp="2026-08-30T20:00:00.000Z", result="PASS",
        )


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
        record_acceptance_fixture(self.adapter)
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

    async def test_checked_native_success_requires_current_delegate_evidence(self):
        self.adapter._ledger._db.execute("DELETE FROM acceptance_evidence")
        self.adapter._ledger._db.commit()
        self.adapter._linear.description = "## Acceptance\n- [x] tests pass\n- [x] restart is safe"
        event = turn_event()
        result = {**dict(event._gateway_turn_result), "completed": True}
        context = await self.adapter._linear.get_agent_turn_context("linear-session")
        self.assertNotEqual(self.adapter._classify_turn_outcome(event, result, context), "success")











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

    def test_linear_policy_owned_delivery_disables_response_streaming(self):
        self.assertIs(self.adapter.supports_response_streaming, False)







































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
