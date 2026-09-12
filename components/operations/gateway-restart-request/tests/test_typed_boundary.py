"""Document prose is not a lifecycle command; execution remains guarded."""
from test_doc_guard import guard
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class TypedBoundaryTests(unittest.TestCase):
    def test_documents_do_not_need_path_or_content_registration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general'):
            root=Path(tmp).resolve()
            for name in ('new-guide.md','different-guide.rst','notes.txt'):
                for text in ('gateway'+' restart','launchctl'+' kickstart ai.hermes.gateway-general'):
                    args={'path':str(root/name),'content':text,'cross_profile':False}
                    self.assertIsNone(guard._pre_tool_call('write_file',args))
                    self.assertIsNone(guard._pre_tool_call('patch',{'path':str(root/name),'old_string':'old','new_string':text,'patch':None,'cross_profile':False}))
                    self.assertIsNotNone(guard._pre_tool_call('terminal',{'command':text}))

    def test_disguised_scripts_and_non_document_config_remain_scanned(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,HERMES_HOME='/Users/mutlupolatcan/.hermes/profiles/general'):
            root=Path(tmp).resolve(); text='gateway'+' restart'
            for name in ('config.yaml','plugin.json','script.py','script.sh'):
                self.assertIsNotNone(guard._pre_tool_call('write_file',{'path':str(root/name),'content':text}))
            target=root/'disguised.md'
            self.assertIsNotNone(guard._pre_tool_call('write_file',{'path':str(target),'content':'#!/bin/sh\n'+text}))
            target.write_text('#!/bin/sh\n')
            self.assertIsNotNone(guard._pre_tool_call('write_file',{'path':str(target),'content':text}))

    def test_no_document_hash_registry_remains(self):
        for symbol in ('_SECURITY_DOC_HASHES','_PROMOTION_MANIFEST_SHA','_CONFIG_SAFETY_DOC_TARGETS'):
            self.assertFalse(hasattr(guard,symbol))

if __name__=='__main__':unittest.main()
