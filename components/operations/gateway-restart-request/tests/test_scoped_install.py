import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'install_gateway_restart_request.py'

class ScopedInstallTests(unittest.TestCase):
    def test_general_only_preserves_config_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for profile in ('general', 'coder'):
                root = home / 'profiles' / profile
                root.mkdir(parents=True)
                (root / 'config.yaml').write_text('plugins:\n  enabled: []\n')
            result = subprocess.run([sys.executable, str(SCRIPT), '--apply', '--hermes-home', str(home), '--profile', 'general', '--plugin-only'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            plugin = home / 'profiles/general/plugins/gateway-restart-request'
            self.assertEqual((plugin / 'ops239-promotion.json').read_bytes(), (SCRIPT.parent / 'ops239-promotion.json').read_bytes())
            self.assertEqual((home / 'profiles/general/config.yaml').read_text(), 'plugins:\n  enabled: []\n')
            self.assertFalse((home / 'profiles/coder/plugins').exists())

    def test_reinstall_keeps_rollback_outside_discovery_root(self):
        spec = importlib.util.spec_from_file_location('scoped_installer', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'general'
            target = profile / 'plugins' / 'gateway-restart-request'
            target.mkdir(parents=True)
            (target / 'plugin.yaml').write_text('name: gateway-restart-request\n')
            (target / '__init__.py').write_text('baseline')
            module.install_plugin(SCRIPT.parent, target)
            manifests = list((profile / 'plugins').rglob('plugin.yaml'))
            self.assertEqual(manifests, [target / 'plugin.yaml'])
            backups = list((profile / 'plugin-backups').rglob('__init__.py'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'baseline')

    def test_missing_manifest_fails_before_replacing_destination(self):
        spec = importlib.util.spec_from_file_location('scoped_installer', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            for name in ('plugin.yaml', '__init__.py'):
                (source / name).write_text('candidate')
            target = root / 'target'
            target.mkdir()
            (target / '__init__.py').write_text('baseline')
            with self.assertRaises(FileNotFoundError):
                module.install_plugin(source, target)
            self.assertEqual((target / '__init__.py').read_text(), 'baseline')

if __name__ == '__main__':
    unittest.main()
