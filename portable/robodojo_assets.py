"""Recover an interrupted official RoboDojo asset installation through Git LFS."""

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from portable.native_inventory import digest

REPOSITORY = 'https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo'
REVISION = '43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab'
SOURCE_REVISION = '726e9aabfaa642203722eb126f5eaf0f37f3e1ad'
POINTER = re.compile(rb'version https://git-lfs.github.com/spec/v1\n'
                     rb'oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n')


def git(repository, *arguments, capture=False, timeout=180):
    environment = {name: os.environ[name] for name in ('PATH', 'LANG', 'SSL_CERT_FILE',
                   'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY') if name in os.environ}
    environment.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    if arguments and arguments[0] == 'checkout':
        environment['GIT_LFS_SKIP_SMUDGE'] = '1'
    result = subprocess.run(['git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
                             '-C', str(repository), *arguments], env=environment, check=True,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout


def manifest(repository):
    if git(repository, 'rev-parse', 'HEAD', capture=True).decode().strip() != REVISION:
        raise ValueError('RoboDojo asset repository revision differs')
    tree = git(repository, 'ls-tree', '-rz', 'HEAD', '--', 'Assets', capture=True)
    entries = []
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, blob = metadata.decode().split()
        path = PurePosixPath(name.decode())
        if (kind != 'blob' or mode not in ('100644', '100755') or path.is_absolute()
                or '..' in path.parts or path.parts[0] != 'Assets'):
            raise ValueError('Unsupported official asset tree entry')
        entries.append((str(path), int(mode, 8) & 0o777, blob))
    if not entries or len({name for name, _, _ in entries}) != len(entries):
        raise ValueError('Official asset tree is empty or contains duplicate paths')
    blobs = list(dict.fromkeys(blob for _, _, blob in entries))
    result = subprocess.run(['git', '-C', str(repository), 'cat-file', '--batch'],
                            input=('\n'.join(blobs) + '\n').encode(), stdout=subprocess.PIPE,
                            check=True, timeout=180)
    content, offset = {}, 0
    for expected in blobs:
        end = result.stdout.index(b'\n', offset)
        actual, kind, length = result.stdout[offset:end].decode().split()
        offset = end + 1
        data = result.stdout[offset:offset + int(length)]
        offset += int(length)
        if (actual != expected or kind != 'blob' or result.stdout[offset:offset + 1] != b'\n'
                or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != expected):
            raise ValueError('Official Git object identity differs')
        offset += 1
        pointer = POINTER.fullmatch(data)
        if data.startswith(b'version https://git-lfs.github.com/spec/v1') and pointer is None:
            raise ValueError('Unsupported Git LFS pointer')
        content[expected] = ({'sha256': pointer[1].decode(), 'bytes': int(pointer[2]),
                              'etag': pointer[1].decode(), 'lfs': True} if pointer else
                             {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                              'etag': expected, 'lfs': False})
    if offset != len(result.stdout):
        raise ValueError('Unexpected trailing Git object data')
    return {'repository': REPOSITORY, 'revision': REVISION,
            'git_tree_sha256': hashlib.sha256(tree).hexdigest(),
            'files': {name: dict(content[blob], git_blob=blob, mode=mode)
                      for name, mode, blob in entries}}


def verify_file(path, record):
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes']
            or digest(path) != record['sha256']):
        raise ValueError('Official asset size or SHA256 differs: ' + str(path))


def cached_recipe(recipe, cache, metadata):
    """Replace only asset transport with verified copies from an operator cache."""
    cache = Path(cache).resolve(strict=True)
    metadata = Path(metadata)
    if not cache.is_dir() or not (cache / 'Assets').is_dir():
        raise ValueError('Asset cache must contain the public RoboDojo Assets directory')
    if recipe.SPEC['source_commit'] != SOURCE_REVISION or recipe.SPEC['assets_commit'] != REVISION:
        raise ValueError('Cached asset transport differs from the registered source pins')
    transport = {}

    def assets(ctx):
        if (ctx.state.get('phase') != 'assets'
                or ctx.state.get('completed') != ['python', 'source', 'dependencies']):
            raise ValueError('Cached assets require the native owner assets phase')
        if cache == ctx.source or cache.is_relative_to(ctx.source) or ctx.source.is_relative_to(cache):
            raise ValueError('Asset cache must be separate from the native installation')
        if git(ctx.source, 'rev-parse', 'HEAD', capture=True).decode().strip() != SOURCE_REVISION:
            raise ValueError('Native RoboDojo source revision differs')
        metadata.mkdir(parents=True, exist_ok=False)
        git(metadata, 'init', '--bare', '--quiet')
        git(metadata, 'remote', 'add', 'origin', REPOSITORY)
        git(metadata, 'fetch', '--depth', '1', 'origin', REVISION)
        git(metadata, 'update-ref', 'HEAD', 'FETCH_HEAD')
        record = manifest(metadata)
        for name, item in record['files'].items():
            source, target = cache / name, ctx.source / name
            if not source.parent.resolve(strict=True).is_relative_to(cache):
                raise ValueError('Cached asset source escapes its directory')
            verify_file(source, item)
            if target.is_symlink() or not target.parent.resolve().is_relative_to(ctx.source):
                raise ValueError('Cached asset destination escapes the installation')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(item['mode'])
            verify_file(target, item)
        manifest_path = metadata / 'official-assets.json'
        manifest_path.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
        (ctx.root / 'asset-paths.json').write_text(json.dumps(list(record['files'])))
        generated = ctx.render_asset_configs(ctx.source / 'Assets/Robots')
        ctx.record_assets([*(ctx.source / name for name in record['files']), *generated])
        transport.update(kind='operator-downloaded public assets', repository=REPOSITORY,
                         revision=REVISION, files_verified=len(record['files']),
                         bytes_verified=sum(item['bytes'] for item in record['files'].values()),
                         manifest_sha256=digest(manifest_path))

    wrapped = SimpleNamespace(**vars(recipe))
    wrapped.assets = assets
    return wrapped, transport


def prefill(repository, destination, python, record):
    if git(destination, 'rev-parse', 'HEAD', capture=True).decode().strip() != SOURCE_REVISION:
        raise ValueError('Native RoboDojo source revision differs')
    if record != manifest(repository):
        raise ValueError('Asset manifest differs from the pinned official Git objects')
    for name, item in record['files'].items():
        source, target = repository / name, destination / name
        if not source.parent.resolve(strict=True).is_relative_to(repository.resolve(strict=True)):
            raise ValueError('Asset source escapes the download directory')
        verify_file(source, item)
        if target.is_symlink() or not target.parent.resolve().is_relative_to(destination.resolve()):
            raise ValueError('Asset destination escapes the installation')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.move(str(source), str(target))
        target.chmod(item['mode'])
        verify_file(target, item)
    # Use the installed Hub's metadata writer after checking every pinned byte.
    # The unchanged native recipe then records and validates its own asset inventory.
    program = '''import json,sys
from pathlib import Path
from huggingface_hub._local_folder import write_download_metadata
record=json.load(sys.stdin)
for name,item in record['files'].items():
    write_download_metadata(Path(sys.argv[1]), name, record['revision'], item['etag'])
'''
    subprocess.run([str(python), '-B', '-I', '-c', program, str(destination)],
                   input=json.dumps(record), text=True, check=True, timeout=120,
                   env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                        'PYTHONDONTWRITEBYTECODE': '1'})


def recover(root, cache):
    from nanoverse.catalog import load_catalog
    from nanoverse.install import Installation

    root, cache = Path(root).resolve(strict=True), Path(cache).absolute()
    if ((root.parent / 'preparation/installed-software.json').exists()
            or (root.parent / 'preparation/sealed').exists()
            or Path('/opt/nvgym/deployment/native-admission.json').exists()):
        raise ValueError('Cannot alter admitted native software')
    recipe = importlib.import_module(load_catalog()['robodojo'].installer)
    if (recipe.SPEC['source_commit'] != SOURCE_REVISION or recipe.SPEC['assets_commit'] != REVISION):
        raise ValueError('Official transport pins differ from the registered installer')
    installation = Installation(root, recipe)
    with installation.owned():
        if (installation.state['completed'] != ['python', 'source', 'dependencies']
                or installation.state.get('phase') != 'assets'):
            raise ValueError('Recovery requires the original incomplete assets phase')
        if cache.is_symlink() or cache.exists():
            raise ValueError('Asset recovery cache must be a new directory')
        cache.mkdir(parents=True)
        git(cache, 'init', '--quiet')
        git(cache, 'remote', 'add', 'origin', REPOSITORY)
        git(cache, 'sparse-checkout', 'set', 'Assets')
        git(cache, 'fetch', '--depth', '1', 'origin', REVISION)
        git(cache, 'checkout', '--detach', 'FETCH_HEAD')
        record = manifest(cache)
        (cache / 'official-assets.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
        git(cache, '-c', 'lfs.concurrenttransfers=4', 'lfs', 'pull', '-I', 'Assets/**', timeout=3600)
        prefill(cache, installation.source, installation.python, record)
        result = {'repository': REPOSITORY, 'revision': REVISION,
                  'files_verified': len(record['files']),
                  'bytes_verified': sum(item['bytes'] for item in record['files'].values()),
                  'manifest_sha256': digest(cache / 'official-assets.json'),
                  'native_installation_complete': False,
                  'next': 'Run the unchanged native install command with --resume'}
        (cache / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        # All installed copies have been verified; keep only the small provenance.
        shutil.rmtree(cache / 'Assets')
        shutil.rmtree(cache / '.git/lfs/objects')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(recover(args.root, args.cache), indent=2))


if __name__ == '__main__':
    main()
