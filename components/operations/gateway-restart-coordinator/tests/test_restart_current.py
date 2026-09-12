"""Restart-current exercises the real queue without deployment coordinates."""
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from restart_coordinator import Coordinator, CoordinatorStore, RequestError, ProcessRuntime
from test_gateway_restart_coordinator import FakeRuntime


class ProvenanceRuntime(FakeRuntime):
    def provenance(self, profile, pid):
        return {'pid': pid, 'code_sha': 'b' * 40, 'code_version': '0.21.1'}


class RestartCurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoordinatorStore(Path(self.temp.name) / 'queue.db', allowed_artifact_roots=[self.temp.name])
        self.runtime = ProvenanceRuntime()

    def payload(self, **updates):
        result = dict(operation='restart_current', task_id='current-a', target_profile='assistant',
                      expected_pid=111, expected_version='1.2.3', health_url='http://127.0.0.1:8788/health',
                      semantic_canary={'path': 'status', 'equals': 'ok'})
        result.update(updates)
        return result

    def test_restarts_without_artifact_or_rollback_files(self):
        try:
            self.store.enqueue('general', self.payload())
        except RequestError as error:
            self.fail(f'restart-current wrongly requires deployment fields: {error}')
        result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert result is not None
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.runtime.restarts, ['assistant'])
        self.assertNotIn('artifact_path', result['payload'])
        self.assertNotIn('rollback_path', result['payload'])
        self.assertEqual(self.store.integrity(), 'ok')

    def test_wrong_pid_and_bad_config_never_restart(self):
        for task, pid, valid in [('wrong-pid', 1, True), ('bad-config', 111, False)]:
            self.store.enqueue('general', self.payload(task_id=task, expected_pid=pid))
            self.runtime.valid = valid
            result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
            assert result is not None
            self.assertEqual(result['status'], 'operator_required')
        self.assertEqual(self.runtime.restarts, [])

    def test_mixed_deployment_contract_is_rejected(self):
        with self.assertRaisesRegex(RequestError, 'restart_current_with_deployment_coordinates'):
            self.store.enqueue('general', self.payload(artifact_path='/not/a/deployment'))

    def test_unknown_operation_and_unauthorized_requester_are_rejected(self):
        with self.assertRaisesRegex(RequestError, 'invalid_operation'):
            self.store.enqueue('general', self.payload(operation='force'))
        with self.assertRaisesRegex(RequestError, 'requester_not_allowed'):
            self.store.enqueue('writer', self.payload())

    def test_duplicate_admission_has_no_second_effect(self):
        self.store.enqueue('general', self.payload())
        with self.assertRaisesRegex(RequestError, 'duplicate_task_id'):
            self.store.enqueue('general', self.payload())
        first = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert first is not None
        self.assertEqual(first['status'], 'succeeded')
        self.assertIsNone(Coordinator(self.store, self.runtime).process_once())
        self.assertEqual(self.runtime.restarts, ['assistant'])

    def test_failed_canary_is_not_blindly_retried(self):
        self.store.enqueue('general', self.payload())
        self.runtime.health_payload['status'] = 'degraded'
        result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert result is not None
        self.assertEqual(result['status'], 'operator_required')
        self.assertFalse(result['rollback_coordinate_verified'])
        self.assertIsNone(Coordinator(self.store, self.runtime).process_once())
        self.assertEqual(self.runtime.restarts, ['assistant'])

    def test_explicit_new_restart_is_not_deployment_convergence(self):
        self.store.enqueue('general', self.payload())
        Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        self.store.enqueue('general', self.payload(task_id='current-b', expected_pid=222))
        # FakeRuntime returns 222 on every signal; the second operation must execute,
        # then fail closed because its PID did not change, not claim convergence.
        result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert result is not None
        self.assertEqual(result['status'], 'operator_required')
        self.assertNotIn('disposition', result)
        self.assertEqual(self.runtime.restarts, ['assistant', 'assistant'])

    def test_loaded_core_mismatch_rejects_otherwise_healthy_restart(self):
        self.store.enqueue('general', self.payload(expected_core_sha='a' * 40))

        result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert result is not None
        self.assertEqual(result['status'], 'operator_required')

    def test_restart_current_does_not_supersede_a_deployment(self):
        import hashlib
        artifact = (Path(self.temp.name) / 'coordinate.json').resolve()
        artifact.write_text('{}')
        artifact.chmod(0o400)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        deployment = self.payload(operation='activate_deployment', task_id='deployment',
                                  artifact_path=str(artifact), artifact_sha256=digest,
                                  rollback_path=str(artifact), rollback_sha256=digest)
        self.store.enqueue('general', deployment)
        self.store.enqueue('general', self.payload())
        self.assertEqual(self.store.get('deployment')['status'], 'queued')

    def test_boolean_pid_is_not_an_integer_process_identity(self):
        with self.assertRaisesRegex(RequestError, 'invalid_expected_pid'):
            self.store.enqueue('general', self.payload(expected_pid=True))

    def test_loaded_core_evidence_is_retained_on_success(self):
        self.store.enqueue('general', self.payload(expected_core_sha='b' * 40))
        result = Coordinator(self.store, self.runtime, readiness_attempts=1).process_once()
        assert result is not None
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['core_provenance']['code_sha'], 'b' * 40)

    def test_native_provenance_reads_exact_pid_and_redacts_unrelated_fields(self):
        home = Path(self.temp.name) / 'general'
        home.mkdir()
        state = home / 'gateway_state.json'
        state.write_text(json.dumps({'pid': 222, 'code_sha': 'b' * 40, 'code_version': '0.21.1', 'private': 'not-evidence'}))
        runtime = ProcessRuntime(profile_root=self.temp.name)
        with patch.object(runtime, 'pid', return_value=222):
            self.assertEqual(runtime.provenance('general', 222),
                             {'pid': 222, 'code_sha': 'b' * 40, 'code_version': '0.21.1'})
            with self.assertRaisesRegex(RuntimeError, 'runtime_provenance_pid_mismatch'):
                runtime.provenance('general', 111)
