import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / '__init__.py'
spec = importlib.util.spec_from_file_location('doc_guard', SOURCE)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class DocGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'candidate'
        self.root.mkdir()
        self.patcher = patch.object(guard, '_DOC_CANDIDATE_ROOT', self.root, create=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.home = patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general')
        self.home.start()
        self.addCleanup(self.home.stop)
        self.text = 'gateway' + ' restart'

    def check(self, tool, path, allowed=False, **extra):
        args = {'path': str(path), 'content': self.text, **extra}
        result = guard._pre_tool_call(tool, args)
        self.assertEqual(result is None, allowed, (tool, path, result))

    def test_candidate_markdown_allowed(self):
        self.check('write_file', self.root / 'reference.md', True)
        self.check('patch', self.root / 'reference.md', True, mode='replace', old_string=self.text, new_string=self.text)

    def test_execution_and_other_targets_stay_blocked(self):
        for tool in ('terminal', 'execute_code', 'code_exec'):
            self.check(tool, self.root / 'reference.md')
        for path in (self.root / 'script.py', self.root / 'script.sh', self.root.parent / 'outside.md'):
            self.check('write_file', path)
        self.check('patch', self.root / 'reference.md', mode='patch', patch=self.text)

    def test_executable_symlink_and_hardlink_stay_blocked(self):
        executable = self.root / 'executable.md'
        executable.write_text('text')
        executable.chmod(0o700)
        self.check('write_file', executable)
        target = self.root.parent / 'outside.md'
        target.write_text('text')
        link = self.root / 'link.md'
        link.symlink_to(target)
        self.check('write_file', link)
        nested = self.root / 'nested'
        nested.symlink_to(self.root.parent, target_is_directory=True)
        self.check('write_file', nested / 'new.md')
        hard = self.root / 'hard.md'
        os.link(target, hard)
        self.check('write_file', hard)

    def test_other_profiles_and_traversal_stay_blocked(self):
        with patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/coder'):
            self.check('write_file', self.root / 'reference.md')
        self.check('write_file', self.root / '..' / 'outside.md')

if __name__ == '__main__':
    unittest.main()
