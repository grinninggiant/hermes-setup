"""Linear on an upstream Hermes core: one model turn per admitted prompt.

Upstream cores expose no gateway goal operations, no owner-bound clarify
waiters and no owner-fenced session cancel. The adapter must stay safe and
goal-free there while keeping the retired fork core behaviour unchanged.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_native_platform as native

from gateway.config import Platform, PlatformConfig

package = native.package
adapter_mod = native.adapter_mod
LinearPlatformAdapter = adapter_mod.LinearPlatformAdapter


class _UpstreamRunner:
    """A runner with no goal operations, like the upstream GatewayRunner."""


class _ForkRunner:
    async def goal_state_for_source(self, source, *, session_id):
        return SimpleNamespace(status="active", source=source, session_id=session_id)

    async def ensure_goal_for_source(self, source, goal, *, contract, session_id):
        return SimpleNamespace(status="active", goal=goal, session_id=session_id)


def _adapter(extra: dict | None = None) -> LinearPlatformAdapter:
    temp = tempfile.mkdtemp()
    config = PlatformConfig(
        enabled=True,
        extra={"database_path": str(Path(temp) / "ledger.sqlite3"), **(extra or {})},
    )
    return LinearPlatformAdapter(config, Platform.WEBHOOK)


class GoalFreeCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_goal_features_are_forced_off_without_core_goal_operations(self):
        adapter = _adapter(
            {"native_goal_continuation_enabled": True, "dependency_wait_enabled": True}
        )
        adapter.gateway_runner = _UpstreamRunner()

        with self.assertLogs(adapter_mod.logger, "WARNING"):
            adapter._disable_goal_features_without_core_goals()

        self.assertFalse(adapter._native_goal_continuation_enabled)
        self.assertFalse(adapter._dependency_wait_enabled)

    def test_goal_features_stay_configured_on_a_core_with_goal_operations(self):
        adapter = _adapter(
            {"native_goal_continuation_enabled": True, "dependency_wait_enabled": True}
        )
        adapter.gateway_runner = _ForkRunner()

        adapter._disable_goal_features_without_core_goals()

        self.assertTrue(adapter._native_goal_continuation_enabled)
        self.assertTrue(adapter._dependency_wait_enabled)

    async def test_goal_operations_are_neutral_without_core_goals(self):
        adapter = _adapter()
        adapter.gateway_runner = _UpstreamRunner()
        source = SimpleNamespace(chat_id="session-1")

        self.assertIsNone(await adapter._goal_state_for_source(source, "hermes-1"))
        self.assertIsNone(
            await adapter._ensure_goal_for_source(source, "hermes-1", "goal", mock.Mock())
        )
        self.assertEqual(
            await adapter._resume_goal_for_source(source, "hermes-1", reset_budget=False),
            (None, None),
        )
        self.assertIsNone(await adapter._next_goal_prompt_for_source(source, "hermes-1"))

    async def test_goal_operations_still_reach_a_core_that_has_them(self):
        adapter = _adapter()
        adapter.gateway_runner = _ForkRunner()
        source = SimpleNamespace(chat_id="session-1")

        state = await adapter._goal_state_for_source(source, "hermes-1")

        self.assertEqual(state.session_id, "hermes-1")


class OwnerFencedCancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = _adapter()
        self.cancelled: list[str] = []

        async def cancel(session_key, *, release_guard=True, discard_pending=True):
            self.cancelled.append(session_key)

        # Upstream signature: no expected_task / expected_guard.
        self.adapter.cancel_session_processing = cancel

    async def _stop(self, owner_task, owner_guard):
        await self.adapter._cancel_owned_session_processing("key-1", owner_task, owner_guard)

    async def test_fenced_core_cancel_receives_the_owner(self):
        received = {}

        async def fenced_cancel(session_key, *, expected_task=None, expected_guard=None):
            received.update(key=session_key, task=expected_task, guard=expected_guard)

        self.adapter.cancel_session_processing = fenced_cancel
        owner, guard = object(), object()

        await self.adapter._cancel_owned_session_processing("key-1", owner, guard)

        self.assertEqual(received, {"key": "key-1", "task": owner, "guard": guard})

    async def test_owner_turn_is_cancelled(self):
        owner = asyncio.ensure_future(asyncio.sleep(0))
        guard = asyncio.Event()
        self.adapter._session_tasks["key-1"] = owner
        self.adapter._active_sessions["key-1"] = guard

        await self._stop(owner, guard)

        self.assertEqual(self.cancelled, ["key-1"])
        await owner

    async def test_newer_turn_is_never_cancelled(self):
        owner = asyncio.ensure_future(asyncio.sleep(0))
        newer = asyncio.ensure_future(asyncio.sleep(0))
        self.adapter._session_tasks["key-1"] = newer

        await self._stop(owner, None)

        self.assertEqual(self.cancelled, [])
        await asyncio.gather(owner, newer)


class LinearClarifyTests(unittest.TestCase):
    def _hook(self, tool_name: str, platform: str):
        env = {"HERMES_SESSION_PLATFORM": platform}
        with mock.patch(
            "gateway.session_context.get_session_env",
            side_effect=lambda name, default="": env.get(name, default),
        ):
            return package._block_unbound_linear_clarify(tool_name=tool_name)

    def test_clarify_is_blocked_on_linear_without_owner_bound_waiters(self):
        with mock.patch.object(package, "_core_binds_clarify_owner", return_value=False):
            directive = self._hook("clarify", "linear")

        self.assertEqual(directive["action"], "block")
        self.assertIn("final response", directive["message"])

    def test_clarify_stays_available_when_the_core_binds_owners(self):
        with mock.patch.object(package, "_core_binds_clarify_owner", return_value=True):
            self.assertIsNone(self._hook("clarify", "linear"))

    def test_other_platforms_and_tools_are_untouched(self):
        with mock.patch.object(package, "_core_binds_clarify_owner", return_value=False):
            self.assertIsNone(self._hook("clarify", "telegram"))
            self.assertIsNone(self._hook("read_file", "linear"))


class BackgroundReviewProgressTests(unittest.TestCase):
    def test_bg_review_thread_never_publishes_linear_progress(self):
        results = []

        def run():
            with mock.patch.object(package, "_progress_adapter") as progress_adapter:
                results.append(package._pre_tool_progress(tool_name="read_file"))
                results.append(progress_adapter.called)

        thread = threading.Thread(target=run, name="bg-review")
        thread.start()
        thread.join(timeout=5)

        self.assertEqual(results, [None, False])


if __name__ == "__main__":
    unittest.main()
