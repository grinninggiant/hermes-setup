"""Semantic text contracts, metadata preservation and isolated rollback only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
ROWS = json.loads((ROOT/'manifest.json').read_text())['rows']
NAMES = sorted({r['name'] for r in ROWS})
MODE = os.environ.get('REVISION_MODE','candidate')
DOCS = {n:(ROOT/n/(MODE+'.md')).read_text() for n in NAMES}
POSITIVE = {
 'session-librarian':['worker/session limits','explicit confirmation'],
 'product-price-monitor':['current schema','Separate','Never buy, book or reserve'],
 'codex':['`codex exec` is noninteractive','Keep sandbox and approval controls intact'],
 'obsidian':['only a candidate','Do not switch to shell writes'],
 'github-issue-to-pr':['isolated fixture or worktree','absent or not-triggered checks'],
 'huggingface-hub':['command arguments','paid Jobs/Endpoints/Spaces'],
 'inspecting-hermes-desktop-dom':['invalid port','vision tool','not proof that no listener exists'],
 'codebase-inspection':['externally managed','`--duplicates`','lexer and version'],
 'blogwatcher':['never overwrite an existing destination','authorized profile-owned database'],
 'apple-reminders':['All literal IDs and dates below are illustrative','authoritative task tracker'],
 'findmy':['last-known position','not a continuous GPS tracker','not an unattended capture method']
}
NEGATIVE = {
 'session-librarian':['(Aug 2026)','Use `delegate_task` with one task per workstream'],
 'product-price-monitor':['~/.hermes/price-watches','browser_navigate','cronjob(action='],
 'codex':['Always use `pty=true`','--sandbox danger-full-access'],
 'obsidian':['If it is unset, use'],
 'github-issue-to-pr':['at least two keyword','CI is running.'],
 'huggingface-hub':['or the `--token` flag.'],
 'inspecting-hermes-desktop-dom':['Closed in exactly two cases','Empty → no port.'],
 'codebase-inspection':['--break-system-packages','all Markdown content as comments'],
 'blogwatcher':['mv ~/.blogwatcher/','Last scanned: 2026'],
 'apple-reminders':['use GitHub Issues, Notion, etc.'],
 'findmy':['while true; do','updates stop when minimized']
}
class Contracts(unittest.TestCase):
 def test_corrected_guidance(self):
  for name,phrases in POSITIVE.items():
   for phrase in phrases:
    with self.subTest(name=name,phrase=phrase):self.assertTrue(phrase in DOCS[name],(name,phrase))
 def test_removed_wrong_assumptions(self):
  for name,phrases in NEGATIVE.items():
   for phrase in phrases:
    with self.subTest(name=name,phrase=phrase):self.assertTrue(phrase not in DOCS[name],(name,phrase))
 def test_metadata(self):
  for name in NAMES:
   self.assertEqual(DOCS[name].split('---',2)[1],(ROOT/name/'baseline.md').read_text().split('---',2)[1])
 def test_complete_empty_support_trees(self):
  for row in ROWS:self.assertEqual(row['supporting_files'],[])
 def test_exact_candidate_and_rollback(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'SKILL.md'
   for row in ROWS:
    baseline=(ROOT/'rollback'/row['name']/(row['baseline_sha256']+'.md')).read_bytes()
    path.write_bytes((ROOT/row['name']/'candidate.md').read_bytes())
    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),row['candidate_sha256'])
    path.write_bytes(baseline)
    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),row['baseline_sha256'])
if __name__=='__main__':unittest.main(verbosity=2)
