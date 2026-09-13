"""Source package integrity checks; not model or live deployment acceptance."""
import hashlib
import json
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT/'manifest.json').read_text())

class LibraryContracts(unittest.TestCase):
    def test_unique_names_and_safe_paths(self):
        names = [r['name'] for r in MANIFEST['skills']]
        self.assertEqual(len(names), len(set(names)))
        for row in MANIFEST['skills']:
            path = ROOT/row['path']
            self.assertTrue(path.resolve().is_relative_to(ROOT.resolve()))
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.name, 'SKILL.md')
            self.assertEqual(path.parent.name, row['name'])

    def test_hashes_and_metadata(self):
        for row in MANIFEST['skills']:
            raw = (ROOT/row['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['sha256'])
            text = raw.decode()
            lines = text.splitlines()
            self.assertEqual(lines[0], '---')
            end = lines.index('---', 1)
            front = yaml.safe_load('\n'.join(lines[1:end]))
            self.assertIsInstance(front, dict)
            self.assertEqual(front['name'], row['name'])
            self.assertEqual(front['license'], row['license'])
            for key in ('author', 'description'):
                self.assertIsInstance(front[key], str)
                self.assertTrue(front[key].strip())

    def test_manifest_covers_all_entrypoints(self):
        expected = {row['path'] for row in MANIFEST['skills']}
        actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob('SKILL.md')}
        self.assertEqual(actual, expected)

    def test_no_runtime_or_secret_payloads(self):
        forbidden = {'.env', 'auth.json', 'state.db', 'jobs.json', 'config.yaml', 'honcho.json'}
        for p in ROOT.rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts:
                continue
            self.assertNotIn(p.name, forbidden)
            if p.suffix not in {'.md', '.json'}:
                continue
            text = p.read_text()
            self.assertNotRegex(text, r'gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
            self.assertNotIn('/Users/', text)

    def test_deployment_not_overclaimed(self):
        for row in MANIFEST['skills']:
            if 'verification_evidence' in row:
                evidence = json.loads((ROOT/row['verification_evidence']).read_text())
                checks = [c for c in evidence['loader_checks'] if c['name'] == row['name']]
                self.assertEqual(len(checks), len(row['verified_profiles']))
                self.assertEqual(sorted(c['profile'] for c in checks), sorted(row['verified_profiles']))
                for check in checks:
                    self.assertEqual(check['sha256'], row['sha256'])
            else:
                self.assertEqual(row['verified_profiles'], ['general'])
            self.assertIn('No implicit installation', row['rollout'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
