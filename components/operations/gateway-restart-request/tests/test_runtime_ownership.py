"""A fresh interpreter cannot inherit continuation authority from SQLite metadata."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from continuation_store import ContinuationStore


class RuntimeOwnershipTests(unittest.TestCase):
    def seed_previous_runtime(self, path, claimed=False):
        source = Path(__file__).resolve().parents[1]
        script = (
            'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
            'from continuation_store import ContinuationStore; '
            's=ContinuationStore(Path(sys.argv[2])); '
            'a=("op-1","session-1","a"*64,"b"*64); s.record(*a); '
            's.claim(*a,owner_id="previous-owner") if sys.argv[3]=="yes" else None'
        )
        subprocess.run([sys.executable, '-I', '-c', script, str(source), str(path),
                        'yes' if claimed else 'no'], check=True, timeout=20,
                       capture_output=True, text=True)

    def test_module_reload_invalidates_already_queued_dispatch_guard(self):
        import hashlib
        import importlib
        import continuation_store
        from continuation_delivery import schedule_bound
        class Native:
            guard = None
            def inject_message(self, content, *, session_key, expected_session_id, dispatch_guard):
                self.guard = dispatch_guard
                return True
        with tempfile.TemporaryDirectory() as tmp:
            store = ContinuationStore(Path(tmp) / 'state.sqlite3')
            route = hashlib.sha256(b'route-1').hexdigest()
            args = ('op-1', 'session-1', route, 'b' * 64)
            store.record(*args)
            store.claim(*args, owner_id='owner-1')
            ctx = Native()
            self.assertTrue(schedule_bound(store, ctx, 'op-1', 'owner-1', 'route-1',
                                           authority_guard=lambda: True))
            guard = ctx.guard
            assert callable(guard)
            self.assertTrue(guard())
            importlib.reload(continuation_store)
            self.assertFalse(guard())

    def test_legacy_rows_are_not_implicitly_authorized_by_migration(self):
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE intents (operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, '
                           'route_digest TEXT NOT NULL, checkpoint_digest TEXT NOT NULL, '
                           "state TEXT NOT NULL DEFAULT 'pending', owner_id TEXT)")
                db.execute('INSERT INTO intents (operation_id,session_id,route_digest,checkpoint_digest) '
                           'VALUES (?,?,?,?)', ('op-1','session-1','a'*64,'b'*64))
            store = ContinuationStore(path)
            row = store.get('op-1')
            assert row is not None
            self.assertEqual(row['runtime_id'], '')
            self.assertFalse(store.runtime_owns('op-1'))
            store.record('op-1', 'session-1', 'a'*64, 'b'*64)
            self.assertFalse(store.claim('op-1', 'session-1', 'a'*64, 'b'*64))
            persisted = ContinuationStore(path).get('op-1')
            assert persisted is not None
            self.assertEqual(persisted['state'], 'pending')

    def test_forked_child_cannot_inherit_runtime_nonce(self):
        import os
        if not hasattr(os, 'fork'):
            self.skipTest('fork is unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(__file__).resolve().parents[1]
            script = (
                'import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'from continuation_store import ContinuationStore; '
                's=ContinuationStore(Path(sys.argv[2])); '
                'a=("op-1","session-1","a"*64,"b"*64); s.record(*a); pid=os.fork(); '
                'os._exit(7 if s.claim(*a) else 0) if pid==0 else None; '
                '_,status=os.waitpid(pid,0); assert os.waitstatus_to_exitcode(status)==0'
            )
            subprocess.run([sys.executable, '-I', '-c', script, str(source),
                            str(Path(tmp) / 'state.sqlite3')], check=True, timeout=20,
                           capture_output=True, text=True)

    def test_new_runtime_cannot_claim_old_pending_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            self.seed_previous_runtime(path)
            store = ContinuationStore(path)
            # Duplicate metadata is not fresh authority and must not rebind runtime.
            store.record('op-1', 'session-1', 'a' * 64, 'b' * 64)
            self.assertFalse(store.claim('op-1', 'session-1', 'a' * 64, 'b' * 64))
            row = store.get('op-1')
            assert row is not None
            self.assertEqual(row['state'], 'pending')

    def test_new_runtime_cannot_submit_using_persisted_owner_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            self.seed_previous_runtime(path, claimed=True)
            store = ContinuationStore(path)
            inject = Mock(return_value=True)
            self.assertFalse(store.submit('op-1', 'previous-owner', inject))
            inject.assert_not_called()
            row = store.get('op-1')
            assert row is not None
            self.assertEqual(row['state'], 'claimed')
