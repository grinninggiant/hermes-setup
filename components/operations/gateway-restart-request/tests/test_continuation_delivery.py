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
