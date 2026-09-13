"""Reconciliation must never restore execution authority."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ledger import DeliveryLedger


class ReconciliationTests(unittest.TestCase):
    def test_verified_create_retires_failure_without_granting_execution(self):
        spec = importlib.util.find_spec('direct_reconciliation')
        self.assertIsNotNone(spec, 'The evidence-only reconciliation capability is missing')
        import direct_reconciliation as recovery
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'general'
            state = home / 'state'
            state.mkdir(parents=True, mode=0o700)
            ledger = DeliveryLedger(str(state / 'linear-bridge.sqlite3'), startup_recovery=False)
            try:
                ledger.reserve_direct_activation_grant(
                    operation_key='test-create', source_platform='telegram',
                    source_user_id='test-user', source_message_id='test-message',
                    source_session_id='test-session', source_profile='general',
                    actor_id='test-actor', team_id='test-team',
                    issue_fingerprint=ledger.direct_issue_fingerprint('test-team', 'Test issue'),
                )
                ledger.fail_direct_activation_grant('test-create', 'unbound_reservation_expired')
                key = hashlib.sha256(b'test-create').hexdigest()
                with sqlite3.connect(state / 'linear-outbound-mcp.sqlite3') as db:
                    db.execute('CREATE TABLE linear_mcp_operations (operation_key TEXT PRIMARY KEY, payload_hash TEXT, tool_name TEXT, profile_id TEXT, actor_id TEXT, team_id TEXT, status TEXT, result_id TEXT)')
                    db.execute('INSERT INTO linear_mcp_operations VALUES (?,?,?,?,?,?,?,?)',
                               (key, 'a' * 64, 'save_issue', 'general', 'test-actor', 'test-team', 'success', 'OPS-1'))
                class Client:
                    actor_id = 'test-actor'
                    async def graphql(self, query, variables):
                        assert query.lstrip().startswith('query ')
                        assert variables == {'id': 'OPS-1'}
                        return {'issue': {'id': '00000000-0000-4000-8000-000000000001',
                            'identifier': 'OPS-1', 'title': 'Test issue', 'updatedAt': '2026-01-01T00:00:00Z',
                            'creator': {'id': 'test-actor'}, 'delegate': {'id': 'test-actor'},
                            'team': {'id': 'test-team'}, 'state': {'id': 'test-backlog', 'type': 'backlog'}}}
                class WrongOwner(Client):
                    async def graphql(self, query, variables):
                        response = await super().graphql(query, variables)
                        response['issue']['delegate']['id'] = 'different-actor'
                        return response
                with self.assertRaisesRegex(ValueError, 'ownership mismatch'):
                    asyncio.run(recovery.build_plan(home, WrongOwner(), [key]))
                plan = asyncio.run(recovery.build_plan(home, Client(), [key]))
                self.assertEqual(ledger.direct_activation_counts()['failed'], 1)
                # A stale approval cannot create even an audit row.
                with self.assertRaisesRegex(ValueError, 'evidence changed'):
                    asyncio.run(recovery.reconcile(home, Client(), [key], expected_sha256='b' * 64))
                self.assertIsNone(ledger._db.execute("SELECT name FROM sqlite_master WHERE name='direct_activation_reconciliations'").fetchone())
                result = asyncio.run(recovery.reconcile(home, Client(), [key], expected_sha256=plan['sha256']))
                self.assertEqual(result['status'], 'reconciled_no_activation')
                row = ledger._db.execute('SELECT state,issue_id,last_error FROM direct_activation_grants').fetchone()
                self.assertEqual(row, ('failed', None, 'unbound_reservation_expired'))
                self.assertFalse(ledger.bind_direct_activation_grant('test-create', '00000000-0000-4000-8000-000000000001'))
                counts = ledger.direct_activation_counts()
                self.assertEqual(counts['failed'], 0)
                self.assertEqual(counts['granted'], 0)
                self.assertEqual(counts['reconciled_no_activation'], 1)
                self.assertEqual(counts['failed_historical'], 1)
                self.assertEqual(counts['last_historical_error'], 'unbound_reservation_expired')
                self.assertIsNone(counts['last_error'])
                asyncio.run(recovery.reconcile(home, Client(), [key], expected_sha256=plan['sha256']))
                self.assertEqual(ledger._db.execute('SELECT COUNT(*) FROM direct_activation_reconciliations').fetchone()[0], 1)
                # A changed historical row invalidates the reconciliation receipt.
                ledger._db.execute('UPDATE direct_activation_grants SET updated_at=updated_at+1')
                ledger._db.commit()
                self.assertEqual(ledger.direct_activation_counts()['failed'], 1)
                self.assertEqual(ledger.direct_activation_counts()['reconciled_no_activation'], 0)
                # A terminal fence cannot become a reconciliation candidate.
                ledger._db.execute("UPDATE direct_activation_grants SET state='canceled'")
                ledger._db.commit()
                with self.assertRaisesRegex(ValueError, 'failed, unbound'):
                    asyncio.run(recovery.build_plan(home, Client(), [key]))
            finally:
                ledger.close()


if __name__ == '__main__':
    unittest.main()
