import json
import hashlib
import marshal
import importlib.util
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_doc_guard import guard


class LoadedBuildTests(unittest.TestCase):
    def test_disk_replacement_does_not_relabel_loaded_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / '__init__.py'
            assert guard.__file__ is not None
            artifact = Path(guard.__file__).read_bytes()
            target.write_bytes(artifact)
            def load(name):
                spec = importlib.util.spec_from_file_location(name, target)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
            old = load('old_build')
            fingerprint = old.LOADED_CODE_SHA256
            expected = hashlib.sha256(marshal.dumps(compile(artifact, str(target), 'exec', dont_inherit=True), 0)).hexdigest()
            self.assertEqual(fingerprint, expected)
            target.write_bytes(artifact + b'\nBUILD_CANARY = 1\n')
            new = load('new_build')
            self.assertEqual(old.LOADED_CODE_SHA256, fingerprint)
            self.assertNotEqual(new.LOADED_CODE_SHA256, fingerprint)

    def test_handler_identifies_loaded_plugin_even_on_rejection(self):
        with patch.object(guard, '_requirements_available', return_value=False):
            result = json.loads(guard._handler({}))
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('request_plugin', result)
        self.assertRegex(result['request_plugin']['loaded_code_sha256'], r'^[0-9a-f]{64}$')
        self.assertTrue(result['request_plugin']['python_cache_tag'])
