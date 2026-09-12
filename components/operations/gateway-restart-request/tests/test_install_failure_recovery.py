"""A failed final promotion must leave the existing plugin discoverable."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('install_failure', Path(__file__).resolve().parents[1] / 'install_gateway_restart_request.py')
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallRecoveryTests(unittest.TestCase):
    def test_failed_promotion_restores_live_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'
            source.mkdir()
            for name in ('plugin.yaml','__init__.py','continuation_store.py','continuation_delivery.py'):
                (source/name).write_text('candidate')
            destination = root/'profiles/general/plugins/gateway-restart-request'
            destination.mkdir(parents=True)
            (destination/'__init__.py').write_text('original')
            rename = Path.rename
            def fail_promotion(path, target):
                if path.parent == destination.parent and path != destination and Path(target) == destination:
                    raise OSError('injected promotion failure')
                return rename(path,target)
            with patch.object(Path,'rename',fail_promotion):
                with self.assertRaisesRegex(OSError,'injected promotion failure'):
                    installer.install_plugin(source,destination)
            self.assertTrue(destination.is_dir(), 'old plugin disappeared after failed promotion')
            self.assertEqual((destination/'__init__.py').read_text(),'original')

if __name__ == '__main__':
    unittest.main()
