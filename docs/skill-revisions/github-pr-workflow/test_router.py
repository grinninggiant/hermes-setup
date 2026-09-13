"""Content/rollback checks; not model-behavior or fleet acceptance."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def validate(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    for rel, digest in manifest['files_sha256'].items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest, rel
    reconstructed = manifest['frontmatter'] + ''.join(
        (root / rel).read_text() for rel in manifest['references_in_order']
    )
    assert reconstructed == (root / 'baseline.md').read_text()
    assert hashlib.sha256(reconstructed.encode()).hexdigest() == manifest['original_sha256']
    router = (root / 'SKILL.md').read_text()
    assert router.startswith(manifest['frontmatter'])
    assert len(router.encode()) < manifest['original_bytes']
    for rel in manifest['references_in_order']:
        assert f'`{rel}`' in router
    for rel in ['SKILL.md', *manifest['references_in_order']]:
        fence = None
        for line in (root / rel).read_text().splitlines():
            value = line.lstrip()
            if value.startswith(('```', '~~~')):
                mark = value[:3]
                if fence is None:
                    fence = mark
                elif fence == mark:
                    fence = None
        assert fence is None, rel
    return manifest


class RouterTests(unittest.TestCase):
    def test_exact_reconstruction_and_links(self):
        validate(ROOT)

    def test_frontmatter_identity_unchanged(self):
        original = (ROOT / 'baseline.md').read_text()
        candidate = (ROOT / 'SKILL.md').read_text()
        self.assertEqual(original.split('\n---\n', 1)[0], candidate.split('\n---\n', 1)[0])

    def test_lossless_guard_rejects_omission(self):
        manifest = validate(ROOT)
        reconstructed = manifest['frontmatter'] + ''.join(
            (ROOT / p).read_text() for p in manifest['references_in_order'][1:]
        )
        self.assertNotEqual(hashlib.sha256(reconstructed.encode()).hexdigest(), manifest['original_sha256'])

    def test_code_blocks_stay_in_their_section(self):
        text = (ROOT / 'references/workflow-open-pr.md').read_text()
        self.assertIn('## Summary', text)
        self.assertIn('gh pr create', text)
        self.assertIn('Closes #42', text)

    def test_existing_support_references_not_replaced(self):
        manifest = validate(ROOT)
        self.assertNotIn('references/ci-troubleshooting.md', manifest['files_sha256'])
        self.assertNotIn('references/conventional-commits.md', manifest['files_sha256'])

    def test_isolated_rollback(self):
        manifest = validate(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'SKILL.md'
            target.write_bytes((ROOT / 'SKILL.md').read_bytes())
            self.assertNotEqual(hashlib.sha256(target.read_bytes()).hexdigest(), manifest['original_sha256'])
            target.write_bytes((ROOT / 'baseline.md').read_bytes())
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), manifest['original_sha256'])

    def test_no_false_behavior_acceptance(self):
        manifest = validate(ROOT)
        self.assertEqual(manifest['acceptance'], 'OPEN')
        self.assertIn('content-preserving', manifest['scope'])


if __name__ == '__main__':
    unittest.main()
