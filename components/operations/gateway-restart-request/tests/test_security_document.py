import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('security_doc_guard', Path(__file__).resolve().parents[1] / '__init__.py')
assert spec and spec.loader
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

class SecurityDocumentTests(unittest.TestCase):
    def test_exact_scope_and_negative_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / 'SKILL.md'
            text = 'gateway' + ' restart'
            target.write_text(text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            with patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general'):
                args = {'path':str(target),'content':text,'cross_profile':False}
                self.assertTrue(g._is_inert_document('write_file',args))
                self.assertIsNone(g._pre_tool_call('write_file',args))
                for extra in ({'cross_profile':True},{'cross_profile':0},{'cross_profile':None},{'command':text}):
                    with self.subTest(extra=extra):
                        self.assertFalse(g._is_inert_document('write_file',args | extra))
                        self.assertIsNotNone(g._pre_tool_call('write_file',args | extra))
                for tool in ('terminal','execute_code','code_exec','patch'):
                    self.assertIsNotNone(g._pre_tool_call(tool,args))
                with patch.dict(os.environ,HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/coder'):
                    self.assertIsNotNone(g._pre_tool_call('write_file',args))
                target.chmod(0o700)
                self.assertFalse(g._is_inert_document('write_file',args))
                target.chmod(0o600)
                hard=root/'hard.md'
                os.link(target,hard)
                self.assertFalse(g._is_inert_document('write_file',args))
                hard.unlink()
                target.unlink()
                self.assertTrue(g._is_inert_document('write_file',args))
                other=root/'real.md'
                other.write_text(text)
                target.symlink_to(other)
                self.assertFalse(g._is_inert_document('write_file',args))

if __name__ == '__main__':
    unittest.main()
