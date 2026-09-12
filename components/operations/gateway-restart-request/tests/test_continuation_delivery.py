import hashlib
from pathlib import Path
import tempfile
import unittest
from continuation_store import ContinuationStore
from continuation_delivery import schedule_bound


class NativeShape:
    def __init__(self):
        self.calls = []

    def inject_message(self, content, *, session_key, expected_session_id, dispatch_guard):
        self.calls.append((session_key, expected_session_id, dispatch_guard))
        return True


class DeliveryTests(unittest.TestCase):
    def test_failed_fence_disables_already_queued_and_future_delivery(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from continuation_delivery import fence_authorized_inbound
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = ContinuationStore(home / 'state.sqlite3')
            route = hashlib.sha256(b'route-1').hexdigest()
            args = ('op-1', 'session-1', route, 'b' * 64)
            store.record(*args)
            store.claim(*args, owner_id='owner-1')
            ctx = NativeShape()
            self.assertTrue(schedule_bound(store, ctx, 'op-1', 'owner-1', 'route-1',
                                           authority_guard=lambda: True))
            sessions = SimpleNamespace(lookup_by_session_key=Mock(
                return_value=SimpleNamespace(session_id='session-1')))
            gateway = SimpleNamespace(session_store=sessions,
                _resolve_profile_home_for_source=Mock(return_value=home),
                _is_user_authorized_for_source=Mock(return_value=True),
                _session_key_for_source=Mock(return_value='route-1'))
            event = SimpleNamespace(source=SimpleNamespace(profile=None), internal=False)
            with patch.object(store, 'fence', side_effect=OSError('test write failure')):
                with self.assertRaises(OSError):
                    fence_authorized_inbound(store, owner_home=home, event=event,
                                             gateway=gateway, session_store=sessions)
            self.assertFalse(ctx.calls[0][2](), 'queued work survived a failed cancellation')
            # Another handle in the SAME process must not bypass the failure latch.
            reopened = ContinuationStore(store.path)
            args2 = ('op-2', 'session-1', route, 'c' * 64)
            reopened.record(*args2)
            reopened.claim(*args2, owner_id='owner-2')
            other = NativeShape()
            self.assertFalse(schedule_bound(reopened, other, 'op-2', 'owner-2', 'route-1',
                                            authority_guard=lambda: True))
            self.assertEqual(other.calls, [])
            raw_inject = Mock(return_value=True)
            self.assertFalse(reopened.submit('op-2', 'owner-2', raw_inject))
            raw_inject.assert_not_called()

    def test_no_fresh_authority_means_no_scheduling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "state.sqlite3")
            route = hashlib.sha256(b"route-1").hexdigest()
            store.record("op-1", "session-1", route, "b" * 64)
            store.claim("op-1", "session-1", route, "b" * 64, owner_id="owner-1")
            for value in (False, None, 1):
                ctx = NativeShape()
                self.assertFalse(schedule_bound(store, ctx, "op-1", "owner-1", "route-1",
                                                 authority_guard=lambda: value))
                self.assertEqual(ctx.calls, [])
            self.assertEqual(store.get("op-1")["state"], "claimed")

    def test_old_native_api_cannot_be_silently_used(self):
        class Legacy:
            def inject_message(self, content, *, session_key):
                raise AssertionError("legacy injection must never run")
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "state.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "native_guarded_injection_unavailable"):
                schedule_bound(store, Legacy(), "op-1", "owner-1", "route-1", authority_guard=lambda: True)
