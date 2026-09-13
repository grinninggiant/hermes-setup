"""Structural contracts only; not a model behavior acceptance test."""
from pathlib import Path
import tempfile
import os
import unittest

ROOT = Path(__file__).resolve().parent
LIVE = Path(os.environ['SKILL_LIVE_ROOT'])
NAMES = ('test-driven-development', 'systematic-debugging', 'simplify-code', 'plan', 'requesting-code-review')

class RevisionContracts(unittest.TestCase):
    def test_sources_and_live_match(self):
        for name in NAMES:
            self.assertEqual((ROOT/name/'candidate.md').read_bytes(), (LIVE/name/'SKILL.md').read_bytes())

    def test_metadata_and_trigger(self):
        for name in NAMES:
            text = (ROOT/name/'candidate.md').read_text()
            front = text.split('---', 2)[1]
            self.assertIn('name: '+name, front)
            self.assertIn('description: "Use when ', front)
            self.assertIn('license: MIT', front)
            self.assertLess(len(next(x for x in front.splitlines() if x.startswith('description:'))), 120)

    def test_test_first_behavior(self):
        text = (ROOT/NAMES[0]/'candidate.md').read_text()
        for phrase in ('RED', 'GREEN', 'REFACTOR', 'old implementation', 'intended missing behavior', 'characterization', 'idempotency', 'external-system behavior'):
            self.assertIn(phrase, text)
        self.assertNotIn('Delete it. Start over.', text)

    def test_debugging_depth_and_race_lesson(self):
        text = (ROOT/NAMES[1]/'candidate.md').read_text()
        for phrase in ('exact symptom', 'one variable', 'contested store lock', 'finally', 'async facade', 'metadata-only', 'actual test results'):
            self.assertIn(phrase, text)
        for phrase in ('Generate 3–5', '95% vs 40%', 'Run the trigger 100x'):
            self.assertNotIn(phrase, text)

    def test_cleanup_scope(self):
        text = (ROOT/NAMES[2]/'candidate.md').read_text()
        for phrase in ('Reuse', 'Quality', 'Efficiency', 'Design depth', 'dry-run', 'RISKY', 'human review', 'current session', 'actual tool schema', 'unrelated user work'):
            self.assertIn(phrase, text)
        self.assertNotIn('Four narrow reviewers beat', text)

    def test_plan_only_boundary(self):
        text = (ROOT/'plan'/'candidate.md').read_text()
        self.assertEqual(text, (LIVE/'plan'/'SKILL.md').read_text())
        for phrase in ('explicit plan-only request', 'read-only tools', 'Do not implement code', 'latest request', 'not permission', 'canonical plan'):
            self.assertIn(phrase, text)

    def test_review_gate_approved_candidate_active(self):
        self.assertEqual((ROOT/'requesting-code-review'/'candidate.md').read_bytes(), (LIVE/'requesting-code-review'/'SKILL.md').read_bytes())
        candidate = (ROOT/'requesting-code-review'/'candidate.md').read_text()
        for phrase in ('gate remains unmet', 'self-review, not independent review', 'block a passing result', 'Never stage unrelated work'):
            self.assertIn(phrase, candidate)

    def test_isolated_byte_exact_rollback(self):
        for name in NAMES:
            baseline = (ROOT/name/'baseline.md').read_bytes()
            candidate = (ROOT/name/'candidate.md').read_bytes()
            with tempfile.TemporaryDirectory() as directory:
                p = Path(directory)/'SKILL.md'
                p.write_bytes(baseline)
                p.write_bytes(candidate)
                self.assertEqual(p.read_bytes(), candidate)
                p.write_bytes(baseline)
                self.assertEqual(p.read_bytes(), baseline)
            self.assertLess(len(candidate), len(baseline))

if __name__ == '__main__':
    unittest.main(verbosity=2)
