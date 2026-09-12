import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / '__init__.py'
spec = importlib.util.spec_from_file_location('config_safety_guard', SOURCE)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class ConfigSafetyPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'SKILL.md'
        self.text = 'gateway' + ' restart'
        self.digest = hashlib.sha256(self.text.encode()).hexdigest()
        self.home = patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general')
        self.home.start()
        self.addCleanup(self.home.stop)
        self.targets = patch.object(guard, '_CONFIG_SAFETY_DOC_TARGETS', {str(self.target): {self.digest}}, create=True)
        self.targets.start()
        self.addCleanup(self.targets.stop)

    def check(self, allowed, tool='write_file', path=None, text=None):
        result = guard._pre_tool_call(tool, {'path': str(path or self.target), 'content': self.text if text is None else text})
        self.assertEqual(result is None, allowed, result)

    def test_exact_candidate_and_existing_rollback(self):
        self.check(True)
        self.target.write_text('baseline')
        self.check(True)

    def test_wrong_content_target_tool_and_profile(self):
        self.check(False, text=self.text + '\n')
        self.check(False, path=self.root / 'other.md')
        for tool in ('terminal', 'execute_code', 'code_exec', 'patch'):
            self.check(False, tool=tool)
        for profile in ('assistant', 'coder', 'writer', 'researcher', 'marketing', 'finance', 'health', 'producer'):
            with patch.dict(os.environ, HERMES_HOME=f'/Users/mutlupolatcan/.hermes/profiles/{profile}'):
                self.check(False)

    def test_symlink_hardlink_executable_and_directory(self):
        outside = self.root / 'outside.md'
        outside.write_text('baseline')
        self.target.symlink_to(outside)
        self.check(False)
        self.target.unlink()
        os.link(outside, self.target)
        self.check(False)
        self.target.unlink()
        self.target.write_text('baseline')
        self.target.chmod(0o700)
        self.check(False)
        self.target.unlink()
        self.target.mkdir()
        self.check(False)

    def test_symlink_parent_and_noncanonical_spelling(self):
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        target = alias / 'SKILL.md'
        with patch.object(guard, '_CONFIG_SAFETY_DOC_TARGETS', {str(target): {self.digest}}):
            self.check(False, path=target)
        self.check(False, path=self.root / 'real' / '..' / 'SKILL.md')

if __name__ == '__main__':
    unittest.main()
