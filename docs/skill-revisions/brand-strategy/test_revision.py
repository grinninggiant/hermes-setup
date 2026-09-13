"""Brand instruction regression contracts; not model behavior acceptance."""
import argparse
from pathlib import Path
import re
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
args, rest = parser.parse_known_args()
ROOT = args.root

class BrandContracts(unittest.TestCase):
    def test_reference_resolution(self):
        for path in ROOT.rglob('*.md'):
            for ref in re.findall(r'`(references/[^`]+\.md)`', path.read_text()):
                self.assertTrue((ROOT / ref).is_file(), (path, ref))

    def test_clear_brief_does_not_force_calibration(self):
        body = (ROOT / 'SKILL.md').read_text()
        self.assertIn('zaten açıksa yeniden seçim isteme', body)
        self.assertNotIn('Tek karar sorusuyla', body)

    def test_domain_constraint_precedes_presentation(self):
        body = (ROOT / 'SKILL.md').read_text()
        self.assertIn('Domain uygunluğu teslim koşuluysa', body)
        self.assertNotIn('Mülkiyet taramasını en sonda', body)
        product = (ROOT / 'references/product-vs-umbrella-company-name.md').read_text()
        self.assertIn('before presenting candidates', product)

    def test_one_domain_evidence_owner(self):
        canonical = (ROOT / 'references/domain-constrained-brand-longlists.md').read_text()
        self.assertIn('## Registration-evidence procedure', canonical)
        self.assertIn('Timeout, 403, 429', canonical)
        self.assertIn('means **unknown**', canonical)
        for name in ['domain-constrained-bulk-naming', 'brand-identity-infrastructure-sequencing', 'story-led-multi-brand-domain-preflight']:
            body = (ROOT / f'references/{name}.md').read_text()
            self.assertIn('references/domain-constrained-brand-longlists.md', body)
            self.assertNotIn('require all three signals', body)
            self.assertNotIn('use three independent read-backs', body)

    def test_no_fixed_finalist_quota(self):
        body = (ROOT / 'references/software-product-name-preflight.md').read_text()
        self.assertNotIn('Present only 2–4', body)
        self.assertIn('requested number', body)

    def test_knowledge_and_task_boundary(self):
        body = (ROOT / 'SKILL.md').read_text()
        self.assertIn('notion-knowledge-ops', body)
        self.assertIn("kabul kriterleri Linear'da kalır", body)
        self.assertIn('hedef belirsizse yazma', body)
        self.assertIn('tescil yetkisi değildir', body)

if __name__ == '__main__':
    unittest.main(argv=['test_revision.py'] + rest, verbosity=2)
