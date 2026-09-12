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
import importlib.util
import subprocess
import sys
from pathlib import Path
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


@pytest.fixture
def installed_continuation(tmp_path):
    """Exercise packaged modules through the real scoped installer, not source imports."""
    component = Path(__file__).resolve().parents[1]
    home = tmp_path / 'installed-home'
    profile = home / 'profiles/general'
    profile.mkdir(parents=True)
    config = profile / 'config.yaml'
    original = 'plugins:\n  enabled: []\n'
    config.write_text(original)
    subprocess.run([sys.executable, str(component / 'install_gateway_restart_request.py'),
        '--apply', '--hermes-home', str(home), '--profile', 'general', '--plugin-only'],
        check=True, capture_output=True, text=True, timeout=20)
    assert config.read_text() == original
    installed = profile / 'plugins/gateway-restart-request'
    modules = []
    for name in ('continuation_store', 'continuation_delivery'):
        spec = importlib.util.spec_from_file_location('installed_' + name, installed / (name + '.py'))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.__file__ is not None
        assert Path(module.__file__).parent == installed
        modules.append(module)
    return modules[0].ContinuationStore, modules[1].schedule_bound, modules[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_sqlite_to_native_ingress(tmp_path, monkeypatch, cancel, installed_continuation):
    ContinuationStore, schedule_bound, _ = installed_continuation
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


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['/stop', 'new authorized instruction'])
@pytest.mark.parametrize('authorized,internal,profile,write_failure,expected', [
    (True, False, None, False, 1), (False, False, None, False, 0),
    (True, True, None, False, 0), (True, False, 'coder', False, 0),
    (True, False, None, True, 0),
])
async def test_native_inbound_fence_authority(tmp_path, monkeypatch, installed_continuation,
                                             authorized, internal, profile, write_failure, expected, text):
    from gateway.platforms.event import MessageEvent
    Store, _, delivery = installed_continuation
    home = tmp_path / 'general'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42',
                           chat_type='dm', profile=profile)
    event = MessageEvent(text=text, source=source, internal=internal)
    store = Store(home / 'continuations.sqlite3')
    store.record('op-1', 'session-42', 'a' * 64, 'b' * 64)
    if write_failure:
        # Characterize an OPEN safety gap, not successful cancellation acceptance:
        # native observer failures are suppressed and user ingress still proceeds.
        monkeypatch.setattr(store, 'fence', Mock(side_effect=OSError('test-only write failure')))
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = Mock()
    runner._is_user_authorized_for_source = Mock(return_value=authorized)
    runner._get_unauthorized_dm_behavior = Mock(return_value='ignore')
    runner._resolve_profile_home_for_source = Mock(return_value=home)
    runner._session_key_for_source = Mock(return_value='route-42')
    runner.session_store = SimpleNamespace(lookup_by_session_key=Mock(
        return_value=SimpleNamespace(session_id='session-42')))
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='fence-test', key='fence-test', source='user'), manager)
    ctx.register_hook('pre_gateway_dispatch', lambda **kwargs: delivery.fence_authorized_inbound(
        store, owner_home=home, **kwargs))
    with patch('hermes_cli.plugins.get_plugin_manager', return_value=manager):
        admitted = await runner._hm_admit_event(event)
    assert store.generation('session-42') == expected
    assert store.dispatch_safe() is (not write_failure)
    assert store.get('op-1')['state'] == ('cancelled' if expected else 'pending')
    if not internal:
        assert (admitted is not None) is authorized
