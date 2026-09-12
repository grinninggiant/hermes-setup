from test_doc_guard import guard
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class ExactPromotionTest(unittest.TestCase):
    def test_exact_only_and_boundary_negatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / 'SKILL.md'
            text = 'gateway' + ' restart'
            manifest = root / 'allow.json'
            manifest.write_text(json.dumps({'target_root':str(root),'files':{'SKILL.md':[hashlib.sha256(text.encode()).hexdigest()]}}))
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            with patch.dict(os.environ, HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general'), patch.object(guard, '_PROMOTION_MANIFEST', manifest, create=True), patch.object(guard, '_PROMOTION_MANIFEST_SHA', digest, create=True):
                args={'path':str(target),'content':text}
                self.assertIsNone(guard._pre_tool_call('write_file',args))
                for tool in ('patch','terminal','execute_code'):
                    self.assertIsNotNone(guard._pre_tool_call(tool,args))
                self.assertIsNotNone(guard._pre_tool_call('write_file',{**args,'content':text+' altered'}))
                self.assertIsNotNone(guard._pre_tool_call('write_file',{**args,'path':str(root/'other.md')}))
                target.write_text('old');target.chmod(0o700)
                self.assertIsNotNone(guard._pre_tool_call('write_file',args))
                target.chmod(0o600)
                with patch.dict(os.environ,HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/coder'):
                    self.assertIsNotNone(guard._pre_tool_call('write_file',args))
                target.unlink();target.symlink_to(manifest)
                self.assertIsNotNone(guard._pre_tool_call('write_file',args))
                target.unlink();os.link(manifest,target)
                self.assertIsNotNone(guard._pre_tool_call('write_file',args))
                manifest.write_text('{}')
                self.assertIsNotNone(guard._pre_tool_call('write_file',args))

if __name__=='__main__':unittest.main()
