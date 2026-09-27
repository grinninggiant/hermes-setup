import contextlib
import io
import json
import tempfile
import unittest
from gateway_restartctl import main

class StatusContractTests(unittest.TestCase):
    def test_status_is_explicit_about_delivery_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--state-dir',tmp,'status']),0)
            result=json.loads(output.getvalue())
            self.assertEqual(result['outbox_delivery_sink'],'coordinator_stdout_log')
            self.assertFalse(result['user_channel_delivery_verified'])
            self.assertFalse(result['automatic_artifact_rollback'])
    def test_missing_task_fails_without_fabricated_status(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--state-dir',tmp,'status','--task-id','missing']),2)
            self.assertEqual(json.loads(output.getvalue())['reason'],'request_not_found')

    def test_recent_lists_metadata_only(self):
        from restart_coordinator import CoordinatorStore
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinatorStore(f"{tmp}/queue.sqlite3")
            with store._connect() as conn:
                for n in (1, 2):
                    conn.execute(
                        "INSERT INTO requests(task_id,requester,target_profile,artifact_sha256,expected_version,"
                        "contract_sha256,payload_json,status) VALUES (?,?,?,?,?,?,?,?)",
                        (f"t{n}", "general", "coder", "a", "v", "c", '{"secret":"x"}', "succeeded"))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--state-dir', tmp, 'status', '--recent', '1']), 0)
            result = json.loads(output.getvalue())
            self.assertEqual([r['task_id'] for r in result['recent']], ['t2'])
            self.assertNotIn('secret', output.getvalue())

if __name__=='__main__':unittest.main()
