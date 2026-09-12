"""Real SQLite persistence tests; no model calls or gateway lifecycle execution."""
import tempfile
import unittest
from pathlib import Path
from continuation_store import ContinuationStore


def _crash_during_submission(path):
    import os
    ContinuationStore(Path(path)).submit("op-1", "owner-1", lambda: os._exit(19))


class ContinuationTests(unittest.TestCase):
    def test_fence_before_late_record_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'continuations.sqlite3'
            ContinuationStore(path).fence('session-1', authorized=True)
            reopened = ContinuationStore(path)
            with self.assertRaisesRegex(ValueError, 'stale_generation'):
                reopened.record('late-op', 'session-1', 'a' * 64, 'b' * 64)
            self.assertIsNone(reopened.get('late-op'))

    def test_fresh_authorization_uses_generation_without_reviving_old_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / 'state.sqlite3')
            old = ('old-op', 'session-1', 'a' * 64, 'b' * 64)
            store.record(*old)
            store.fence('session-1', authorized=True)
            generation = store.generation('session-1')
            self.assertEqual(generation, 1)
            store.record('new-op', 'session-1', 'a' * 64, 'c' * 64,
                         expected_generation=generation)
            self.assertEqual(store.get('new-op')['state'], 'pending')
            store.record(*old, expected_generation=generation)
            self.assertEqual(store.get('old-op')['state'], 'cancelled')
            store.fence('session-1', authorized=True)
            with self.assertRaisesRegex(ValueError, 'stale_generation'):
                store.record('late-op', 'session-1', 'a' * 64, 'd' * 64,
                             expected_generation=generation)
            self.assertEqual(store.generation('session-1'), 2)
            self.assertEqual(store.generation('other-session'), 0)
            store.record('other-op', 'other-session', 'a' * 64, 'b' * 64)

    def test_fence_metadata_and_generation_types_are_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / 'state.sqlite3')
            for invalid in ('private conversation body', '', None):
                with self.assertRaisesRegex(ValueError, 'invalid_binding'):
                    store.fence(invalid, authorized=True)
                with self.assertRaisesRegex(ValueError, 'invalid_binding'):
                    store.generation(invalid)
            with self.assertRaises(PermissionError):
                store.fence('session-1', authorized=False)
            self.assertEqual(store.generation('session-1'), 0)
            for invalid in (False, True, 0.0, -1, '0', None):
                with self.assertRaisesRegex(ValueError, 'invalid_generation'):
                    store.record('op-1', 'session-1', 'a' * 64, 'b' * 64,
                                 expected_generation=invalid)
            self.assertIsNone(store.get('op-1'))

    def test_concurrent_fences_do_not_lose_generation_updates(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            store = ContinuationStore(path)
            def fence(_):
                ContinuationStore(path).fence('session-1', authorized=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fence, range(8)))
            self.assertEqual(ContinuationStore(path).generation('session-1'), 8)
            with self.assertRaisesRegex(ValueError, 'stale_generation'):
                store.record('late-op', 'session-1', 'a' * 64, 'b' * 64,
                             expected_generation=7)

    def test_metadata_fields_reject_prompt_bodies_and_invalid_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "state.sqlite3")
            valid = ["op-1", "session-1", "a" * 64, "b" * 64]
            for index in range(4):
                args = valid.copy()
                args[index] = "raw private conversation body"
                with self.assertRaisesRegex(ValueError, "invalid_binding"):
                    store.record(*args)
            self.assertIsNone(store.get("op-1"))

    def test_process_death_during_injection_is_durable_and_not_replayed(self):
        import multiprocessing
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuations.sqlite3"
            store = ContinuationStore(path)
            args = ("op-1", "session-1", "a" * 64, "b" * 64)
            store.record(*args)
            store.claim(*args, owner_id="owner-1")
            child = multiprocessing.get_context("spawn").Process(target=_crash_during_submission, args=(str(path),))
            child.start()
            child.join(timeout=10)
            try:
                self.assertFalse(child.is_alive())
                self.assertEqual(child.exitcode, 19)
            finally:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
                child.close()
            recovered = ContinuationStore(path)
            self.assertEqual(recovered.get("op-1")["state"], "dispatching")
            inject = Mock(return_value=True)
            self.assertFalse(recovered.submit("op-1", "owner-1", inject))
            self.assertFalse(recovered.claim(*args, owner_id="new-process"))
            inject.assert_not_called()

    def test_submit_is_owner_bound_and_ack_is_not_completion(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "continuations.sqlite3")
            args = ("op-1", "session-1", "a" * 64, "b" * 64)
            store.record(*args)
            self.assertTrue(store.claim(*args, owner_id="owner-1"))
            inject = Mock(return_value=True)
            self.assertFalse(store.submit("op-1", "wrong-owner", inject))
            inject.assert_not_called()
            self.assertTrue(store.submit("op-1", "owner-1", inject))
            inject.assert_called_once_with()
            self.assertEqual(store.get("op-1")["state"], "submitted")
            self.assertFalse(store.submit("op-1", "owner-1", inject))
            self.assertEqual(inject.call_count, 1)

    def test_cancel_before_submission_prevents_injection(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "continuations.sqlite3")
            args = ("op-1", "session-1", "a" * 64, "b" * 64)
            store.record(*args)
            store.claim(*args, owner_id="owner-1")
            store.fence("session-1", authorized=True)
            inject = Mock(return_value=True)
            self.assertFalse(store.submit("op-1", "owner-1", inject))
            inject.assert_not_called()

    def test_only_one_concurrent_owner_can_claim_and_cancel_is_terminal(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuations.sqlite3"
            store = ContinuationStore(path)
            store.record("op-1", "session-1", "a" * 64, "b" * 64)
            def claim(_):
                return ContinuationStore(path).claim("op-1", "session-1", "a" * 64, "b" * 64)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(claim, range(8)))
            self.assertEqual(results.count(True), 1)
            self.assertEqual(store.get("op-1")["state"], "claimed")
            store.fence("session-1", authorized=True)
            self.assertFalse(claim(None))
            self.assertEqual(store.get("op-1")["state"], "cancelled")

    def test_wrong_checkpoint_cannot_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "continuations.sqlite3")
            store.record("op-1", "session-1", "a" * 64, "b" * 64)
            self.assertFalse(store.claim("op-1", "session-1", "a" * 64, "c" * 64))
            self.assertEqual(store.get("op-1")["state"], "pending")

    def test_duplicate_cannot_resurrect_cancelled_intent_or_change_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "continuations.sqlite3")
            args = ("op-1", "session-1", "a" * 64, "b" * 64)
            store.record(*args)
            store.fence("session-1", authorized=True)
            store.record(*args)
            self.assertEqual(store.get("op-1")["state"], "cancelled")
            with self.assertRaisesRegex(ValueError, "binding_conflict"):
                store.record("op-1", "other-session", "a" * 64, "b" * 64)

    def test_unverified_input_cannot_cancel_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / "continuations.sqlite3")
            store.record("op-1", "session-1", "a" * 64, "b" * 64)
            for authority in (False, None, "true", 1):
                with self.assertRaises(PermissionError):
                    store.fence("session-1", authorized=authority)
            self.assertEqual(store.get("op-1")["state"], "pending")

    def test_authorized_fence_is_durable_and_session_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuations.sqlite3"
            store = ContinuationStore(path)
            store.record("op-1", "session-1", "a" * 64, "b" * 64)
            store.record("op-2", "session-2", "c" * 64, "d" * 64)
            store.fence("session-1", authorized=True)
            recovered = ContinuationStore(path)
            self.assertEqual(recovered.get("op-1")["state"], "cancelled")
            self.assertEqual(recovered.get("op-2")["state"], "pending")

    def test_intent_survives_store_recreation_without_prompt_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuations.sqlite3"
            store = ContinuationStore(path)
            store.record("op-1", "session-1", "a" * 64, "b" * 64)
            row = ContinuationStore(path).get("op-1")
            self.assertEqual(row["session_id"], "session-1")
            self.assertEqual(row["state"], "pending")
            self.assertNotIn("prompt", row)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
