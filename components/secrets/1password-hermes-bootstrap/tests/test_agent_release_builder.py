import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import os
from unittest import mock

spec = importlib.util.spec_from_file_location("builder", Path(__file__).resolve().parents[1] / "deployment/agent_release_builder.py")
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

SCRIPT = Path(__file__).resolve().parents[1] / 'deployment/agent_release_builder.py'


class ReleaseTests(unittest.TestCase):
    def test_exact_release_and_failure_preserve_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / 'repo'
            repo.mkdir()
            def run(*args, cwd=repo):
                return subprocess.check_output(args, cwd=cwd, text=True).strip()
            run('git', 'init', '-q')
            run('git', 'config', 'user.name', 'Test')
            run('git', 'config', 'user.email', 'test@example.invalid')
            (repo / 'pyproject.toml').write_text('[project]\nname="release-fixture"\nversion="1.0"\nrequires-python=">=3.11"\ndependencies=[]\n[project.scripts]\nhermes="fixture:main"\n[build-system]\nrequires=["setuptools==83.0.0"]\nbuild-backend="setuptools.build_meta"\n[tool.setuptools]\npy-modules=["fixture"]\n')
            (repo / 'fixture.py').write_text('def main():\n print("fixture-ready")\n')
            run('uv', 'lock', '--python', sys.executable)
            run('git', 'add', '.')
            run('git', 'commit', '-qm', 'fixture')
            commit = run('git', 'rev-parse', 'HEAD')
            tree = run('git', 'rev-parse', 'HEAD^{tree}')
            rollback = root / 'rollback'
            rollback.write_text('unchanged')
            target = root / 'candidate'
            cmd = [sys.executable, str(SCRIPT), '--repo', str(repo), '--commit', commit,
                   '--target', str(target), '--python', sys.executable, '--import-module', 'fixture']
            result = subprocess.run(cmd, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((target / 'manifest.json').read_text())
            self.assertEqual(manifest['commit'], commit)
            self.assertEqual(manifest['tree'], tree)
            self.assertEqual((target / 'source/.hermes_build_sha').read_text(), commit + '\n')
            self.assertIn('.hermes_build_sha', manifest['generated_source_files'])
            self.assertEqual(run(str(target / 'venv/bin/python'), '-I', '-c', 'import fixture; print(fixture.__file__)'), str(target / 'source/fixture.py'))
            self.assertEqual(manifest['status'], 'ready')
            self.assertEqual(run(str(target / 'venv/bin/hermes')), 'fixture-ready')
            self.assertEqual(run(str(target / 'venv/bin/python'), '-c', 'import sys; print(sys.prefix)'), str(target / 'venv'))
            self.assertIn(str(target / 'venv/bin/python'), (target / 'venv/bin/hermes').read_text())
            self.assertEqual((target / 'manifest.json').stat().st_mode & 0o777, 0o400)
            before = (target / 'manifest.json').read_bytes()
            again = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual((target / 'manifest.json').read_bytes(), before)
            (repo / 'fixture.py').write_text('def main():\n print("different")\n')
            run('git', 'add', '.'); run('git', 'commit', '-qm', 'changed')
            other = run('git', 'rev-parse', 'HEAD')
            collision = [other if x == commit else x for x in cmd]
            self.assertNotEqual(subprocess.run(collision, capture_output=True).returncode, 0)
            self.assertEqual((target / 'manifest.json').read_bytes(), before)
            failed = root / 'failed'
            failure = [str(failed) if x == str(target) else x for x in cmd]
            failure[-1] = 'missing_release_fixture_module'
            self.assertNotEqual(subprocess.run(failure, capture_output=True).returncode, 0)
            self.assertTrue(failed.exists())
            self.assertFalse((failed / 'manifest.json').exists())
            self.assertEqual(rollback.read_text(), 'unchanged')
            for invalid in ('HEAD', commit[:8], 'a' * 40):
                bad = [invalid if x == commit else x for x in cmd]
                self.assertNotEqual(subprocess.run(bad, capture_output=True).returncode, 0)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.target = self.root / 'candidate'
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'pyproject.toml').write_text('[project]\nname="release-fixture"\nversion="1.0"\nrequires-python=">=3.11"\ndependencies=[]\n[project.scripts]\nhermes="fixture:main"\n[build-system]\nrequires=["setuptools==83.0.0"]\nbuild-backend="setuptools.build_meta"\n[tool.setuptools]\npy-modules=["fixture"]\n')
        (self.repo / 'fixture.py').write_text('def main():\n print("fixture-ready")\n')
        subprocess.check_call(['uv', 'lock', '--python', sys.executable], cwd=self.repo, stdout=subprocess.DEVNULL)
        git('add', '.'); git('commit', '-qm', 'fixture')
        self.commit = git('rev-parse', 'HEAD')

    def build(self):
        return builder.build(self.repo, self.commit, self.target, sys.executable, ['fixture'], [])

    def change(self, path, text):
        path.parent.chmod(0o700)
        if path.exists():
            path.chmod(0o600)
        path.write_text(text)

    def test_external_pth_rejected_before_execution(self):
        self.build()
        site = next((self.target / 'venv/lib').glob('python*/site-packages'))
        marker = self.root / 'EXECUTED'
        (self.repo / 'fixture.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\ndef main(): print("mutable-ready")\n')
        self.change(site / 'external.pth', 'import sys; sys.path.insert(0, ' + repr(str(self.repo)) + ')\n')
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse(marker.exists(), 'candidate executed before integrity validation')

    def test_source_symlink_and_special_entries_rejected(self):
        self.build()
        source = self.target / 'source'
        source.chmod(0o700)
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind):
                path = source / 'unexpected'
                if kind == 'symlink':
                    path.symlink_to(self.repo / 'fixture.py')
                else:
                    os.mkfifo(path)
                try:
                    with self.assertRaises(ValueError):
                        self.build()
                finally:
                    path.unlink()

    def test_build_backend_cannot_redefine_committed_inventory(self):
        original = builder.run
        def run(argv, **kw):
            result = original(argv, **kw)
            if 'sync' in argv:
                (self.target / 'source/fixture.py').write_text('def main():\n print("backend-mutated")\n')
            return result
        with mock.patch.object(builder, 'run', side_effect=run):
            with self.assertRaises(ValueError):
                self.build()
        self.assertTrue(self.target.exists())
        self.assertFalse((self.target / 'manifest.json').exists())

    def test_archive_and_lock_manifest_rechecked(self):
        self.build()
        path = self.target / 'manifest.json'
        data = json.loads(path.read_text())
        for key in ('archive_sha256', 'lock_sha256'):
            with self.subTest(field=key):
                changed = dict(data, **{key: '0' * 64})
                self.change(path, json.dumps(changed))
                with self.assertRaises(ValueError):
                    self.build()
        self.change(path, json.dumps(data))
        self.change(self.target / 'source/uv.lock', '# lock drift\n')
        with self.assertRaises(ValueError):
            self.build()

    def test_structural_symlinks_rejected_without_candidate_execution(self):
        self.build()
        for relative in ('source', 'manifest.json', 'venv', 'venv/bin', 'venv/lib', 'build-home'):
            with self.subTest(path=relative):
                path = self.target / relative
                path.parent.chmod(0o700)
                saved = path.with_name(path.name + '.saved')
                path.rename(saved)
                path.symlink_to(saved, target_is_directory=saved.is_dir())
                try:
                    with mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
                        with self.assertRaises(ValueError):
                            self.build()
                finally:
                    path.unlink(); saved.rename(path)

    def test_venv_mapping_executable_and_bootstrap_drift_rejected(self):
        self.build()
        site = next((self.target / 'venv/lib').glob('python*/site-packages'))
        paths = [self.target / 'venv/bin/hermes', self.target / 'venv/pyvenv.cfg',
                 next(site.glob('*.pth')), next(site.glob('__editable__*finder.py')),
                 next(site.glob('*.dist-info/direct_url.json'))]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_text()
                mode = path.stat().st_mode & 0o777
                self.change(path, original + '\n# drift\n')
                try:
                    with mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
                        with self.assertRaises(ValueError):
                            self.build()
                finally:
                    self.change(path, original)
                    path.chmod(mode)
        python = self.target / 'venv/bin/python'
        original = os.readlink(python)
        python.parent.chmod(0o700)
        python.unlink(); python.symlink_to('/bin/sh')
        try:
            with mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
                with self.assertRaises(ValueError):
                    self.build()
        finally:
            python.unlink(); python.symlink_to(original)

    def test_initial_external_editable_paths_rejected(self):
        original = builder.run
        for kind in ('pth', 'finder', 'direct_url'):
            with self.subTest(kind=kind):
                self.target = self.root / ('candidate-' + kind)
                def run(argv, **kw):
                    result = original(argv, **kw)
                    if 'sync' in argv:
                        site = next((self.target / 'venv/lib').glob('python*/site-packages'))
                        if kind == 'pth':
                            (site / 'external.pth').write_text(str(self.repo) + '\n')
                        elif kind == 'finder':
                            next(site.glob('__editable__*finder.py')).write_text('MAPPING = {"fixture": ' + repr(str(self.repo / 'fixture.py')) + '}\n')
                        else:
                            next(site.glob('*.dist-info/direct_url.json')).write_text(json.dumps({'url': self.repo.as_uri(), 'dir_info': {'editable': True}}))
                    return result
                with mock.patch.object(builder, 'run', side_effect=run), mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
                    with self.assertRaises(ValueError):
                        self.build()
                self.assertTrue(self.target.exists())
                self.assertFalse((self.target / 'manifest.json').exists())

    def test_named_generated_metadata_only_and_no_forged_source_baseline(self):
        manifest = self.build()
        self.assertIn('release_fixture.egg-info/PKG-INFO', manifest['generated_source_files'])
        unexpected = self.target / 'source/unrelated.egg-info'
        self.change(unexpected, 'not allowed')
        try:
            with self.assertRaises(ValueError):
                self.build()
        finally:
            unexpected.unlink()
        source = self.target / 'source/fixture.py'
        self.change(source, 'def main(): pass\n')
        manifest['source_files']['fixture.py']['sha256'] = builder.digest(source)
        self.change(self.target / 'manifest.json', json.dumps(manifest))
        with mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
            with self.assertRaises(ValueError):
                self.build()

    def test_build_marker_drift_rejected_before_candidate_execution(self):
        self.build()
        marker = self.target / 'source/.hermes_build_sha'
        self.assertTrue(marker.is_file())
        self.change(marker, '0' * 40 + '\n')
        with mock.patch.object(builder, 'verify', side_effect=AssertionError('executed candidate')):
            with self.assertRaises(ValueError):
                self.build()

    def test_legitimate_locked_build_site_bootstrap_is_preserved(self):
        original = builder.run
        def run(argv, **kw):
            result = original(argv, **kw)
            if 'sync' in argv:
                site = next((self.target / 'venv/lib').glob('python*/site-packages'))
                (site / 'legitimate.pth').write_text('import sys; sys._builder_bootstrap = True\n')
            return result
        with mock.patch.object(builder, 'run', side_effect=run):
            manifest = self.build()
        self.assertTrue(any(name.endswith('/legitimate.pth') for name in manifest['venv_files']))
        self.assertEqual(self.build()['status'], 'ready')

    def test_legacy_schema_rejected_without_repair(self):
        manifest = self.build()
        manifest['schema'] = 1
        path = self.target / 'manifest.json'
        self.change(path, json.dumps(manifest))
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(path.read_bytes(), before)

    def test_real_desktop_import_and_callable_are_required(self):
        # Real Git/venv package with all defaults; only the Desktop import is bad.
        project = self.repo / 'pyproject.toml'
        project.write_text(project.read_text().replace('py-modules=["fixture"]', 'py-modules=["fixture", "run_agent"]\npackages=["hermes_cli", "tui_gateway"]'))
        (self.repo / 'run_agent.py').write_text('')
        for package, module in (('hermes_cli', 'main'), ('tui_gateway', 'server')):
            directory = self.repo / package
            directory.mkdir()
            (directory / '__init__.py').write_text('')
            (directory / (module + '.py')).write_text('')
        web = self.repo / 'hermes_cli/web_server.py'
        for index, text in enumerate(('raise RuntimeError("desktop-import-exercised")\n', 'start_server = None\n', 'def start_server(): pass\n')):
            web.write_text(text)
            subprocess.check_call(['git', 'add', '.'], cwd=self.repo)
            subprocess.check_call(['git', 'commit', '-qm', 'desktop fixture'], cwd=self.repo)
            commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
            target = self.root / ('desktop-' + str(index))
            if index < 2:
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    builder.build(self.repo, commit, target, sys.executable, builder.DEFAULT_IMPORTS, [])
                self.assertIn(b'desktop-import-exercised' if index == 0 else b'AssertionError', caught.exception.output)
                self.assertFalse((target / 'manifest.json').exists())
            else:
                result = builder.build(self.repo, commit, target, sys.executable, builder.DEFAULT_IMPORTS, [])
                self.assertEqual(result['status'], 'ready')

    def test_default_checks_real_desktop_entrypoint(self):
        result = dict(target='unused', commit=self.commit, tree='unused', status='ready')
        with mock.patch.object(sys, 'argv', ['builder', '--repo', str(self.repo), '--commit', self.commit, '--target', str(self.target)]), mock.patch.object(builder, 'build', return_value=result) as build:
            self.assertEqual(builder.main(), 0)
        self.assertIn('hermes_cli.web_server:start_server', build.call_args.args[4])


if __name__ == '__main__':
    unittest.main()
