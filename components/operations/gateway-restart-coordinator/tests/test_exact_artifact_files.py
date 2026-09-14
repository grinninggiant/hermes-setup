import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from restart_coordinator import CoordinatorStore, RequestError


class ExactArtifactFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.allowed = self.root / 'artifacts'
        self.allowed.mkdir()
        self.wrapper = self.root / 'scripts' / 'gateway.sh'
        self.wrapper.parent.mkdir()
        self.wrapper.write_bytes(b'candidate')
        self.wrapper.chmod(0o444)
        self.rollback = self.allowed / 'rollback.sh'
        self.rollback.write_bytes(b'baseline')
        self.rollback.chmod(0o444)
        self.store = CoordinatorStore(
            self.root / 'state' / 'queue.db',
            allowed_artifact_roots=[self.allowed],
            allowed_artifact_files=[self.wrapper],
        )

    def payload(self, path=None):
        path = path or self.wrapper
        return {
            'task_id': 'exact-file-test', 'target_profile': 'general',
            'artifact_path': str(path),
            'artifact_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'rollback_path': str(self.rollback),
            'rollback_sha256': hashlib.sha256(self.rollback.read_bytes()).hexdigest(),
            'expected_version': 'test', 'expected_pid': 111,
            'health_url': 'http://127.0.0.1:9999/health',
            'semantic_canary': {'path': 'status', 'equals': 'ok'},
        }

    def test_exact_immutable_file_is_accepted_with_identity(self):
        result = self.store.enqueue('general', self.payload())
        self.assertEqual(result['status'], 'queued')
        saved = self.store.get(result['task_id'])['payload']
        self.assertEqual(saved['artifact_path'], str(self.wrapper))
        self.assertEqual(saved['artifact_identity']['inode'], self.wrapper.stat().st_ino)

    def test_sibling_is_not_authorized(self):
        sibling = self.wrapper.with_name('other.sh')
        sibling.write_bytes(b'other')
        sibling.chmod(0o444)
        with self.assertRaisesRegex(RequestError, 'outside_allowed_roots'):
            self.store.enqueue('general', self.payload(sibling))

    def test_exact_filename_never_becomes_a_directory_grant(self):
        self.wrapper.unlink()
        self.wrapper.mkdir()
        child = self.wrapper / 'child.sh'
        child.write_bytes(b'child')
        child.chmod(0o444)
        with self.assertRaisesRegex(RequestError, 'outside_allowed_roots'):
            self.store.enqueue('general', self.payload(child))

    def test_symlink_is_rejected(self):
        alias = self.wrapper.with_name('alias.sh')
        alias.symlink_to(self.wrapper)
        with self.assertRaisesRegex(RequestError, 'invalid_artifact_path'):
            self.store.enqueue('general', self.payload(alias))

    def test_parent_symlink_is_rejected(self):
        alias = self.root / 'alias-directory'
        alias.symlink_to(self.wrapper.parent, target_is_directory=True)
        with self.assertRaisesRegex(RequestError, 'contains_symlink'):
            self.store.enqueue('general', self.payload(alias / self.wrapper.name))

    def test_mutable_exact_file_is_rejected(self):
        self.wrapper.chmod(0o644)
        with self.assertRaisesRegex(RequestError, 'not_immutable'):
            self.store.enqueue('general', self.payload())

    def test_hardlinked_exact_file_is_rejected(self):
        os.link(self.wrapper, self.wrapper.with_name('hardlink.sh'))
        with self.assertRaisesRegex(RequestError, 'not_unique_regular_file'):
            self.store.enqueue('general', self.payload())

    def test_requester_permissions_are_unchanged(self):
        with self.assertRaisesRegex(RequestError, 'requester_not_allowed'):
            self.store.enqueue('writer', self.payload())


class ProductionGrantTests(unittest.TestCase):
    def test_facade_keeps_roots_and_adds_only_the_approved_file(self):
        import contextlib
        import io
        from unittest import mock
        import gateway_restartctl

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(gateway_restartctl, 'CoordinatorStore', wraps=CoordinatorStore) as constructor:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gateway_restartctl.main(['--state-dir', state, 'status']), 0)
            self.assertEqual(constructor.call_args.kwargs, {
                'allowed_artifact_roots': [
                    '/Users/mutlupolatcan/.hermes/profiles',
                    '/Users/mutlupolatcan/.hermes/runtime',
                    '/Users/mutlupolatcan/.hermes/backups',
                ],
                'allowed_artifact_files': [
                    '/Users/mutlupolatcan/.hermes/scripts/hermes-gateway-keychain.sh',
                ],
            })


if __name__ == '__main__':
    unittest.main()
