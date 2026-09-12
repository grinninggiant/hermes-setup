import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / '__init__.py'
spec = importlib.util.spec_from_file_location('general_document_guard', SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class GeneralDocumentGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.skills = self.root / 'skills'
        self.artifacts = self.root / 'artifacts'
        self.skills.mkdir()
        self.artifacts.mkdir()
        for name, value in [('_GENERAL_SKILL_DOC_ROOT', self.skills), ('_GENERAL_AUDIT_DOC_ROOT', self.artifacts)]:
            context = patch.object(module, name, value, create=True)
            context.start()
            self.addCleanup(context.stop)
        env = patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general')
        env.start()
        self.addCleanup(env.stop)
        self.example = 'gateway' + ' restart'

    def blocked(self, path, tool='write_file', **kwargs):
        args = {'path': str(path), 'content': self.example, **kwargs}
        return module._pre_tool_call(tool, args) is not None

    def test_only_scoped_document_writes_are_allowed(self):
        for path in [self.skills/'ops/demo/SKILL.md', self.skills/'ops/demo/references/runbook.md', self.artifacts/'inventory.json', self.artifacts/'notes.md']:
            with self.subTest(path=path):
                self.assertFalse(self.blocked(path))
        args = {'mode':'replace', 'path':str(self.skills/'ops/demo/SKILL.md'), 'old_string':'old', 'new_string':self.example}
        self.assertIsNone(module._pre_tool_call('patch', args))

    def test_execution_profile_path_and_alias_boundaries_remain_closed(self):
        for path in [self.skills/'ops/demo/script.py', self.skills/'ops/demo/misc.md', self.skills/'ops/demo/config.json', self.artifacts/'script.sh', self.root/'plugins/demo/__init__.py', self.root/'config.yaml', self.root/'shared/SKILL.md', self.artifacts/'..'/'outside.md']:
            with self.subTest(path=path):
                self.assertTrue(self.blocked(path))
        for tool in ['terminal', 'execute_code', 'code_exec']:
            self.assertTrue(self.blocked(self.artifacts/'notes.md', tool=tool))
        self.assertTrue(self.blocked(self.artifacts/'notes.md', command=self.example))
        with patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/coder'):
            self.assertTrue(self.blocked(self.artifacts/'notes.md'))
        target = self.artifacts/'live.md'
        target.write_text('baseline')
        target.chmod(0o700)
        self.assertTrue(self.blocked(target))
        target.chmod(0o600)
        link = self.artifacts/'alias.md'
        link.symlink_to(target)
        self.assertTrue(self.blocked(link))
        nested = self.artifacts/'nested'
        nested.symlink_to(self.root, target_is_directory=True)
        self.assertTrue(self.blocked(nested/'new.md'))
        hard = self.artifacts/'hard.md'
        os.link(target, hard)
        self.assertTrue(self.blocked(hard))
        self.assertTrue(self.blocked(target))

if __name__ == '__main__':
    unittest.main()
