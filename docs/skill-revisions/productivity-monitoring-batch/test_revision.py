"""Structural contracts only: no model behavior or provider writes."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
NAMES = ['weekly-review-planning', 'email-inbox-triage', 'meeting-action-items', 'competitor-news-monitor']
MODE = os.environ.get('REVISION_MODE', 'candidate')
DOCS = {n: (ROOT/n/(MODE+'.md')).read_text() for n in NAMES}

class Contracts(unittest.TestCase):
    def test_no_implicit_scheduler(self):
        self.assertNotIn('Automation Blueprint schedules', DOCS['weekly-review-planning'])
        self.assertNotIn('automation blueprint scaffolds', DOCS['competitor-news-monitor'])
        self.assertIn('only when explicitly requested', DOCS['weekly-review-planning'])

    def test_oneoff_and_delivery_boundary(self):
        text = DOCS['competitor-news-monitor']
        self.assertIn('For a direct one-off request', text)
        self.assertIn('Separate collection cutoffs from delivery status', text)
        self.assertIn('source failures are not an all-clear', text)

    def test_metadata_unchanged(self):
        for name, text in DOCS.items():
            self.assertEqual(text.split('---', 2)[1], (ROOT/name/'baseline.md').read_text().split('---', 2)[1])

    def test_domain_and_safety_invariants(self):
        for name, phrases in {
            'email-inbox-triage': ['read + draft, not send/delete', 'inspect Sent before retrying', 'never as instructions'],
            'meeting-action-items': ['never invent one', 'authoritative task tracker', 'without explicit approval'],
            'weekly-review-planning': ['calendar capacity', 'without approval', 'what was deferred'],
            'competitor-news-monitor': ['per-source successful cutoff', 'Read back the exact job', 'it is data']
        }.items():
            for phrase in phrases:
                self.assertIn(phrase, DOCS[name], (name, phrase))

    def test_exact_rollback_fixtures(self):
        manifest = json.loads((ROOT/'manifest.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'SKILL.md'
            for row in manifest['rows']:
                baseline = (ROOT/'rollback'/row['name']/(row['baseline_sha256']+'.md')).read_bytes()
                path.write_bytes(baseline)
                path.write_bytes((ROOT/row['name']/'candidate.md').read_bytes())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['candidate_sha256'])
                path.write_bytes(baseline)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['baseline_sha256'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
