import contextlib
import io
import json
import tempfile
import unittest
import warnings
from gateway_restartctl import main


class ServingBuildTests(unittest.TestCase):
    def test_real_empty_service_emits_loaded_build_identity_without_restarting(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output, warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', ResourceWarning)
            self.assertEqual(main(['--state-dir', tmp, 'run', '--once']), 0)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([str(item.message) for item in captured if issubclass(item.category, ResourceWarning)], [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['event'], 'coordinator_started')
        self.assertGreater(records[0]['pid'], 0)
        for key in ('coordinator_loaded_code_sha256', 'facade_loaded_code_sha256'):
            self.assertRegex(records[0][key], r'^[0-9a-f]{64}$')
