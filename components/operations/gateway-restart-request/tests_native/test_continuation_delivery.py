"""Real native scheduling/ingress with real SQLite; transport and source lookup are doubles.
No model call, real gateway restart, or external platform delivery is exercised here.
"""
import asyncio
from datetime import datetime
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
import yaml
from continuation_store import ContinuationStore
from continuation_delivery import schedule_bound
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_sqlite_to_native_ingress(tmp_path, monkeypatch, cancel):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {
        "restart-test": {"allow_gateway_injection": True}
    }}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm")
    now = datetime.now()
    entry = SessionEntry(session_key="agent:main:telegram:dm:42", session_id="session-42",
                         created_at=now, updated_at=now, origin=source, platform=Platform.TELEGRAM)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
        lookup_by_session_key=AsyncMock(return_value=entry))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._running, runner._draining = True, False
    runner._background_tasks = set()
    runner._gateway_loop = asyncio.get_running_loop()
    runner._is_user_authorized = Mock(return_value=True)
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name="restart-test", key="restart-test", source="user"), manager)
    store = ContinuationStore(home / "continuations.sqlite3")
    args = ("op-1", entry.session_id, hashlib.sha256(entry.session_key.encode()).hexdigest(), "b" * 64)
    store.record(*args)
    store.claim(*args, owner_id="owner-1")
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        runner._install_plugin_message_injector()
        try:
            assert schedule_bound(store, ctx, "op-1", "owner-1", entry.session_key,
                                  authority_guard=lambda: True) is True
            assert store.get("op-1")["state"] == "submitted"
            await asyncio.gather(*list(runner._background_tasks))
            event = adapter.handle_message.await_args.args[0]
            assert event.allow_gateway_control is False
            assert event.metadata["gateway_session_id"] == entry.session_id
            if cancel:
                store.fence(entry.session_id, authorized=True)
            admitted = await runner._hm_admit_event(event)
            assert (admitted is None) is cancel
            assert store.get("op-1")["state"] != "completed"
        finally:
            runner._clear_plugin_message_injector()
