"""Credential-free launcher contract tests, not model behavior tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TARGET = Path(os.environ.get('WRAPPER_TARGET', str(Path(__file__).with_name('hermes-gateway-keychain.sh'))))
RELEASE = '593baf8a705cf100b2671c6d70c8b454151644ae'
PROFILES = ('general', 'assistant', 'researcher', 'coder', 'writer', 'producer', 'marketing', 'health', 'finance')
LOOKUP = 'token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)'
ROOT = 'bootstrap_root="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap"'

def isolated(text, root):
    for exact in (LOOKUP, ROOT):
        if text.count(exact) != 1:
            raise ValueError('Unsafe source drift')
    text = text.replace(LOOKUP, 'token="synthetic-test-value"').replace(ROOT, f'bootstrap_root="{root}"')
    if '/usr/bin/security' in text or '/.hermes/runtime/hermes-gateway-sdk-bootstrap' in text:
        raise ValueError('Live resolver survived')
    return text

def invoke(text, profile):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        safe = isolated(text, root)
        (root/'venv/bin').mkdir(parents=True)
        (root/'venv/bin/python').symlink_to(sys.executable)
        fake = root/'hermes_gateway_sdk_bootstrap.py'
        fake.write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
        fake.chmod(0o700)
        wrapper = root/'wrapper.sh'
        wrapper.write_text(safe)
        return subprocess.run(['/bin/zsh', str(wrapper), profile], capture_output=True, text=True)

class WrapperContract(unittest.TestCase):
    def test_all_profiles(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                result = invoke(TARGET.read_text(), profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                executable = f'/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-{RELEASE}/venv/bin/hermes' if profile == 'general' else '/Users/mutlupolatcan/.local/bin/hermes'
                self.assertEqual(json.loads(result.stdout), [profile, '--hermes-executable', executable])
    def test_unsupported_profile(self):
        self.assertEqual(invoke(TARGET.read_text(), 'not-a-profile').returncode, 64)
    def test_drift_never_executes(self):
        text = TARGET.read_text()
        for drift in (text.replace(LOOKUP, ''), text+'\n'+LOOKUP, text.replace(LOOKUP, LOOKUP.replace('token=', 'token =')), text.replace(ROOT, '')):
            with patch('subprocess.run') as run:
                with self.assertRaises(ValueError):
                    invoke(drift, 'general')
                run.assert_not_called()

if __name__ == '__main__':
    unittest.main(verbosity=2)
