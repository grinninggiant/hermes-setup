import unittest
import jsonschema
from test_doc_guard import guard


class OperationSchemaTests(unittest.TestCase):
    def test_restart_current_is_admitted_without_deployment_fields(self):
        payload = dict(operation='restart_current', task_id='schema-only', target_profile='general',
                       expected_pid=123, expected_version='1.2.3', expected_core_sha='b' * 40, health_url='http://127.0.0.1:8787/health',
                       semantic_canary={'path': 'adapter', 'equals': 'linear-native'})
        errors = list(jsonschema.Draft202012Validator(guard.SCHEMA['parameters']).iter_errors(payload))
        self.assertEqual([error.message for error in errors], [])

    def test_deployment_fields_remain_required_by_default(self):
        payload = dict(task_id='schema-only', target_profile='general', expected_pid=123,
                       expected_version='1.2.3', health_url='http://127.0.0.1:8787/health',
                       semantic_canary={'path': 'adapter', 'equals': 'linear-native'})
        self.assertFalse(jsonschema.Draft202012Validator(guard.SCHEMA['parameters']).is_valid(payload))
