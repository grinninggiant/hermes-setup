"""Text-revision checks only; never starts nano-pdf or a model."""
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.before = (ROOT / 'baseline.md').read_text()
        self.after = (ROOT / 'SKILL.md').read_text()
        self.changes = json.loads((ROOT / 'changes.json').read_text())

    def test_exact_two_changes(self):
        self.assertEqual(len(self.changes), 2)
        expected = self.before
        for change in self.changes:
            self.assertEqual(expected.count(change['old']), 1)
            expected = expected.replace(change['old'], change['new'], 1)
        self.assertEqual(expected, self.after)

    def test_identity_and_domain_information_preserved(self):
        self.assertEqual(self.before.split('## Notes')[0], self.after.split('## Notes')[0])
        self.assertEqual(self.before.split('- The tool uses an LLM')[1], self.after.split('- The tool uses an LLM')[1])

    def test_unsafe_shortcuts_removed(self):
        self.assertNotIn('if the edit hits the wrong page, retry with ±1', self.after)
        self.assertNotIn('use `read_file` to check file size', self.after)
        for phrase in ['Work on a copy', 'extracted text and rendered appearance', 'unrelated pages stayed unchanged']:
            self.assertIn(phrase, self.after)

    def test_isolated_rollback(self):
        restored = self.after
        for change in reversed(self.changes):
            restored = restored.replace(change['new'], change['old'], 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SKILL.md'
            path.write_text(self.after)
            path.write_text(restored)
            self.assertEqual(path.read_bytes(), self.before.encode())


if __name__ == '__main__':
    unittest.main()
