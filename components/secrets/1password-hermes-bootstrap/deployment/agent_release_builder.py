#!/usr/bin/env python3
"""Build an exact Git release; never activate, replace, or delete a runtime.

The trusted inputs are the operator-selected Git object, interpreter and locked
build dependencies. The seal detects drift, not an owner rewriting the seal.
No candidate interpreter (including site bootstrap) runs before static checks.
"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from urllib.parse import unquote, urlparse

DEFAULT_IMPORTS = ['hermes_cli.main', 'run_agent', 'tui_gateway.server',
                   'hermes_cli.web_server:start_server']
EGG_FILES = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt',
             'requires.txt', 'top_level.txt', 'not-zip-safe', 'zip-safe'}


def run(argv, *, cwd=None, env=None):
    return subprocess.check_output([str(x) for x in argv], cwd=cwd, env=env, stderr=subprocess.STDOUT)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_type(path, directory=False):
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError(f'unsupported structural path: {path}')


def inventory(root, interpreter=None):
    """lstat every entry, without following directory links or ignoring caches."""
    require_type(root, directory=True)
    result = {}
    def visit(directory):
        for p in sorted(directory.iterdir()):
            name = p.relative_to(root).as_posix()
            mode = p.lstat().st_mode
            if stat.S_ISDIR(mode):
                result[name] = {'directory': True}
                visit(p)
            elif stat.S_ISREG(mode):
                result[name] = {'sha256': digest(p), 'executable': bool(mode & 0o111)}
            elif stat.S_ISLNK(mode) and interpreter is not None and re.fullmatch(r'bin/python(?:\d+(?:\.\d+)?)?', name):
                resolved = p.resolve(strict=True)
                if resolved != interpreter:
                    raise ValueError(f'interpreter link escapes requested interpreter: {p}')
                require_type(resolved)
                result[name] = {'link': os.readlink(p), 'resolved': str(resolved), 'sha256': digest(resolved)}
            else:
                raise ValueError(f'unsupported link or special file: {p}')
    visit(root)
    return result


def archive_record(archive):
    files, directories = {}, set()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            name = member.name.rstrip('/')
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or name != str(path):
                raise ValueError('unsafe source archive path')
            if not (member.isfile() or member.isdir()):
                raise ValueError('source archive contains unsupported link or special file')
            directories.update(str(p) for p in path.parents if str(p) != '.')
            if member.isdir():
                directories.add(name)
            else:
                files[name] = {'sha256': hashlib.sha256(tar.extractfile(member).read()).hexdigest(),
                               'executable': bool(member.mode & 0o111)}
        project = tomllib.loads(tar.extractfile('pyproject.toml').read().decode())
    if 'uv.lock' not in files:
        raise ValueError('commit has no uv.lock')
    if '.hermes_build_sha' in files:
        raise ValueError('build marker must be generated from the verified commit')
    egg = re.sub(r'[-_.]+', '_', project['project']['name']) + '.egg-info'
    record = dict(files)
    record.update({name: {'directory': True} for name in directories})
    return record, egg


def source_record(source, committed, egg, commit):
    actual = inventory(source)
    if any(actual.get(name) != value for name, value in committed.items()):
        raise ValueError('candidate committed source integrity failure')
    generated = {name: value for name, value in actual.items() if name not in committed}
    for name, value in generated.items():
        if name == '.hermes_build_sha' and value == {
            'sha256': hashlib.sha256((commit + '\n').encode()).hexdigest(),
            'executable': False,
        }:
            continue
        path = PurePosixPath(name)
        if name == egg and value == {'directory': True}:
            continue
        if len(path.parts) != 2 or path.parts[0] != egg or path.name not in EGG_FILES or 'sha256' not in value:
            raise ValueError(f'unexpected generated source entry: {name}')
    return generated


def validate_mappings(target):
    """Reject external editable/data paths; retain legitimate installed bootstrap.

    Executable .pth code belongs to trusted locked build dependencies and is
    inventoried byte-for-byte. Do not attempt to sandbox Python with a denylist.
    """
    source = target / 'source'
    venv = target / 'venv'
    for p in venv.rglob('*.pth'):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith(('import ', 'import\t')):
                continue
            resolved = (p.parent / line).resolve()
            if not (resolved.is_relative_to(source) or resolved.is_relative_to(venv)):
                raise ValueError(f'external .pth source path: {p}')
    for p in venv.rglob('__editable__*finder.py'):
        for node in ast.walk(ast.parse(p.read_text())):
            names = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if not any(isinstance(n, ast.Name) and n.id in ('MAPPING', 'NAMESPACES') for n in names):
                continue
            mapping = ast.literal_eval(node.value)
            for value in mapping.values():
                for entry in value if isinstance(value, list) else [value]:
                    if not Path(entry).resolve().is_relative_to(source):
                        raise ValueError(f'external editable finder mapping: {p}')
    for p in venv.rglob('direct_url.json'):
        data = json.loads(p.read_text())
        url = urlparse(data.get('url', ''))
        if url.scheme == 'file' and (url.netloc not in ('', 'localhost') or Path(unquote(url.path)).resolve() != source):
            raise ValueError(f'external direct_url source: {p}')


def static_verify(target, manifest, committed, egg, interpreter):
    for name in ('source', 'venv', 'venv/bin', 'venv/lib', 'build-home'):
        require_type(target / name, directory=True)
    generated = source_record(target / 'source', committed, egg, manifest['commit'])
    if generated != manifest['generated_source_files']:
        raise ValueError('generated source metadata drift')
    if inventory(target / 'venv', interpreter) != manifest['venv_files']:
        raise ValueError('candidate venv integrity failure')
    validate_mappings(target)
    console = target / 'venv/bin/hermes'
    require_type(console)
    if str(target / 'venv/bin/python') not in console.read_text(encoding='utf-8')[:1024]:
        raise ValueError('console script does not reference final interpreter')


def build(repo, commit, target, python, modules, extras):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise ValueError('commit must be exactly 40 hexadecimal characters')
    for module in modules:
        if not re.fullmatch(r'[A-Za-z_]\w*(\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*)?', module):
            raise ValueError('invalid import module or entrypoint')
    repo = Path(repo).resolve(strict=True)
    target = Path(target)
    if not target.is_absolute() or target.is_symlink() or target != target.resolve():
        raise ValueError('target must be an absolute canonical path, not a symlink')
    python = str(Path(python).absolute())
    interpreter = Path(python).resolve(strict=True)
    require_type(interpreter)
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('uv is required')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'en_US.UTF-8',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'}
    resolved = run(['git', '-C', repo, 'rev-parse', '--verify', commit + '^{commit}'], env=env).decode().strip()
    if resolved.lower() != commit.lower():
        raise ValueError('object is not the requested commit')
    tree = run(['git', '-C', repo, 'rev-parse', resolved + '^{tree}'], env=env).decode().strip()
    # Derive source truth BEFORE any candidate/build backend execution, on every run.
    archive = run(['git', '-C', repo, 'archive', '--format=tar', resolved], env=env)
    committed, egg = archive_record(archive)
    identity = {'schema': 3, 'repo': str(repo), 'commit': resolved, 'tree': tree,
                'python_requested': python, 'python_resolved': str(interpreter),
                'python_sha256': digest(interpreter), 'imports': modules, 'extras': extras,
                'target': str(target), 'archive_sha256': hashlib.sha256(archive).hexdigest(),
                'lock_sha256': committed['uv.lock']['sha256'], 'source_files': committed,
                'generated_metadata_directory': egg}
    if target.exists():
        require_type(target, directory=True)
        require_type(target / 'manifest.json')
        manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        if any(manifest.get(k) != v for k, v in identity.items()) or manifest.get('status') != 'ready':
            raise ValueError('candidate collision: identity/provenance differs or incomplete/old schema')
        static_verify(target, manifest, committed, egg, interpreter)
        verify(target, uv, modules, env)
        static_verify(target, manifest, committed, egg, interpreter)
        return manifest
    target.mkdir(mode=0o700)
    home = target / 'build-home'
    home.mkdir(mode=0o700)
    env.update(HOME=str(home), HERMES_HOME=str(home / 'hermes'),
               UV_PROJECT_ENVIRONMENT=str(target / 'venv'), PYTHONDONTWRITEBYTECODE='1',
               UV_KEYRING_PROVIDER='disabled', UV_PYTHON_DOWNLOADS='never')
    source = target / 'source'
    source.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter='data')
    (source / '.hermes_build_sha').write_text(resolved + '\n', encoding='utf-8')
    source_record(source, committed, egg, resolved)
    run([uv, 'venv', '--python', python, target / 'venv'], env=env)
    args = [uv, 'sync', '--frozen', '--no-dev', '--no-default-groups',
            '--no-python-downloads', '--python', python, '--project', source]
    for extra in extras:
        args.extend(['--extra', extra])
    run(args, cwd=source, env=env)
    manifest = dict(identity, status='ready', uv_version=run([uv, '--version'], env=env).decode().strip(),
                    generated_source_files=source_record(source, committed, egg, resolved),
                    venv_files=inventory(target / 'venv', interpreter))
    static_verify(target, manifest, committed, egg, interpreter)
    verify(target, uv, modules, env)
    static_verify(target, manifest, committed, egg, interpreter)
    with (target / 'manifest.json').open('x', encoding='utf-8') as out:
        json.dump(manifest, out, indent=2, sort_keys=True)
        out.write('\n')
    for path in sorted(target.rglob('*'), reverse=True):
        if not path.is_symlink():
            path.chmod(0o500 if path.is_dir() or path.stat().st_mode & 0o111 else 0o400)
    target.chmod(0o500)
    return manifest


def verify(target, uv, modules, env):
    """Execute readiness only after the caller has validated the static seal."""
    env = dict(env, HOME=str(target / 'build-home'), HERMES_HOME=str(target / 'build-home/hermes'),
               PYTHONDONTWRITEBYTECODE='1', UV_KEYRING_PROVIDER='disabled')
    interpreter = target / 'venv/bin/python'
    prefix = run([interpreter, '-I', '-B', '-c', 'import sys; print(sys.prefix)'], env=env).decode().strip()
    if prefix != str(target / 'venv'):
        raise ValueError('interpreter is not at final candidate path')
    run([uv, '--no-cache', 'pip', 'check', '--python', interpreter], env=env)
    for entry in modules:
        module, _, attribute = entry.partition(':')
        code = 'import importlib; module = importlib.import_module(' + repr(module) + ')'
        if attribute:
            code += '; assert callable(getattr(module, ' + repr(attribute) + '))'
        run([interpreter, '-I', '-B', '-c', code], cwd=target, env=env)
    run([target / 'venv/bin/hermes', '--help'], cwd=target, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='operator-verified trusted local Git repository')
    parser.add_argument('--commit', required=True)
    parser.add_argument('--target', required=True, help='new final absolute candidate path; parent must exist')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--import-module', action='append', dest='modules')
    parser.add_argument('--extra', action='append', default=[])
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    try:
        result = build(args.repo, args.commit, args.target, args.python,
                       args.modules or DEFAULT_IMPORTS, sorted(set(args.extra)))
        print(json.dumps({k: result[k] for k in ('target', 'commit', 'tree', 'status')}))
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f'build failed; candidate retained: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.output.decode(errors='replace'), file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
