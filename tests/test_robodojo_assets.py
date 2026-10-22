import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from portable import robodojo_assets as assets


def commit(repository, files):
    repository.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
    for name, data in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=asset-test',
                    '-c', 'user.email=asset-test@localhost', 'commit', '--quiet', '-m', 'fixture'],
                   check=True)
    return subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                                   text=True).strip()


@pytest.fixture
def official_tree(tmp_path, monkeypatch):
    data = b'real asset bytes\x00\xff'
    pointer = (b'version https://git-lfs.github.com/spec/v1\noid sha256:'
               + hashlib.sha256(data).hexdigest().encode() + b'\nsize '
               + str(len(data)).encode() + b'\n')
    repository = tmp_path / 'download'
    revision = commit(repository, {'Assets/Robots/robot.usd': pointer,
                                    'Assets/Material/license.txt': b'asset license\n'})
    monkeypatch.setattr(assets, 'REVISION', revision)
    record = assets.manifest(repository)
    (repository / 'Assets/Robots/robot.usd').write_bytes(data)
    destination = tmp_path / 'native'
    source_revision = commit(destination, {'source.py': b'# source\n'})
    monkeypatch.setattr(assets, 'SOURCE_REVISION', source_revision)
    return repository, destination, record


def test_manifest_uses_git_objects_after_lfs_checkout(official_tree):
    repository, _, record = official_tree
    assert assets.manifest(repository) == record
    assert record['files']['Assets/Robots/robot.usd']['lfs'] is True
    assert record['files']['Assets/Material/license.txt']['lfs'] is False
    assert len(record['files']) == 2


@pytest.fixture
def cache_installation(official_tree, tmp_path, monkeypatch):
    cache, source, _ = official_tree
    monkeypatch.setattr(assets, 'REPOSITORY', str(cache))
    root = tmp_path / 'installation'
    root.mkdir()
    recorded = []
    ctx = SimpleNamespace(root=root, source=source,
                          state={'phase': 'assets', 'completed': ['python', 'source', 'dependencies']},
                          render_asset_configs=lambda folder: [],
                          record_assets=lambda paths: recorded.extend(paths))
    def original(ctx):
        pytest.fail('Cached transport called the network asset downloader')
    recipe = SimpleNamespace(SPEC={'source_commit': assets.SOURCE_REVISION, 'assets_commit': assets.REVISION},
                             assets=original)
    wrapped, transport = assets.cached_recipe(recipe, cache, tmp_path / 'metadata')
    return cache, ctx, wrapped, transport, recorded


def test_operator_cache_is_copied_verified_and_left_unchanged(cache_installation):
    cache, ctx, recipe, transport, recorded = cache_installation
    before = {str(path.relative_to(cache)): path.read_bytes()
              for path in (cache / 'Assets').rglob('*') if path.is_file()}
    recipe.assets(ctx)
    assert {name: (cache / name).read_bytes() for name in before} == before
    assert {str(path.relative_to(ctx.source)): path.read_bytes() for path in recorded} == before
    assert json.loads((ctx.root / 'asset-paths.json').read_text()) == sorted(before)
    assert transport['revision'] == assets.REVISION
    assert transport['files_verified'] == len(before)
    assert transport['bytes_verified'] == sum(map(len, before.values()))


@pytest.mark.parametrize('corruption', ['same_size', 'missing', 'symlink'])
def test_operator_cache_rejects_incomplete_or_changed_assets(cache_installation, tmp_path, corruption):
    cache, ctx, recipe, transport, recorded = cache_installation
    path = cache / 'Assets/Robots/robot.usd'
    if corruption == 'same_size':
        path.write_bytes(b'X' * path.stat().st_size)
    elif corruption == 'missing':
        path.unlink()
    else:
        outside = tmp_path / 'outside-data'
        path.rename(outside)
        path.symlink_to(outside)
    with pytest.raises(ValueError, match='size or SHA256'):
        recipe.assets(ctx)
    assert not transport and not recorded
    assert not (ctx.source / 'Assets/Robots/robot.usd').exists()


def test_operator_cache_cannot_replace_assets_after_installation(cache_installation):
    _, ctx, recipe, _, _ = cache_installation
    ctx.state['completed'].append('assets')
    with pytest.raises(ValueError, match='owner assets phase'):
        recipe.assets(ctx)


def test_prefill_verifies_and_populates_actual_hub_cache(official_tree):
    repository, destination, record = official_tree
    assets.prefill(repository, destination, Path(sys.executable), record)
    for name, item in record['files'].items():
        path = destination / name
        assert path.stat().st_size == item['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        assert not (repository / name).exists()
        from huggingface_hub._local_folder import read_download_metadata
        metadata = read_download_metadata(destination, name)
        assert metadata.commit_hash == assets.REVISION and metadata.etag == item['etag']


@pytest.mark.parametrize('corruption', ['same_size', 'short', 'symlink'])
def test_prefill_rejects_changed_download_bytes(official_tree, tmp_path, corruption):
    repository, destination, record = official_tree
    source = repository / 'Assets/Robots/robot.usd'
    if corruption == 'same_size':
        source.write_bytes(b'X' * source.stat().st_size)
    elif corruption == 'short':
        source.write_bytes(b'X')
    else:
        outside = tmp_path / 'outside'
        source.rename(outside)
        source.symlink_to(outside)
    with pytest.raises(ValueError, match='size or SHA256'):
        assets.prefill(repository, destination, Path(sys.executable), record)
    assert not (destination / 'Assets/Robots/robot.usd').exists()
    assert not (destination / '.cache/huggingface/download/Assets/Robots/robot.usd.metadata').exists()


def test_prefill_rejects_manifest_changed_after_git_authentication(official_tree):
    repository, destination, record = official_tree
    record['files']['Assets/Robots/robot.usd']['sha256'] = 'a' * 64
    with pytest.raises(ValueError, match='manifest differs'):
        assets.prefill(repository, destination, Path(sys.executable), record)
    assert not (destination / 'Assets').exists()


def test_prefill_refuses_destination_symlink(official_tree, tmp_path):
    repository, destination, record = official_tree
    outside = tmp_path / 'outside'
    outside.mkdir()
    (destination / 'Assets').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='destination escapes'):
        assets.prefill(repository, destination, Path(sys.executable), record)
    assert not list(outside.rglob('*.usd'))


def test_manifest_rejects_wrong_revision(official_tree, monkeypatch):
    repository, _, _ = official_tree
    monkeypatch.setattr(assets, 'REVISION', '0' * 40)
    with pytest.raises(ValueError, match='revision differs'):
        assets.manifest(repository)


def test_manifest_rejects_git_tracked_symlink(tmp_path, monkeypatch):
    repository = tmp_path / 'download'
    repository.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
    (repository / 'Assets').mkdir()
    (repository / 'Assets/escape').symlink_to('/outside')
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=asset-test',
                    '-c', 'user.email=asset-test@localhost', 'commit', '--quiet', '-m', 'link'],
                   check=True)
    monkeypatch.setattr(assets, 'REVISION', subprocess.check_output(
        ['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip())
    with pytest.raises(ValueError, match='tree entry'):
        assets.manifest(repository)


@pytest.mark.parametrize('sealed_path', ['installed-software.json', 'sealed'])
def test_recovery_refuses_sealed_native_software(tmp_path, sealed_path):
    root = tmp_path / 'payload'
    root.mkdir()
    evidence = tmp_path / 'preparation'
    evidence.mkdir()
    if sealed_path == 'sealed':
        (evidence / sealed_path).mkdir()
    else:
        (evidence / sealed_path).write_text(json.dumps({'sealed': True}))
    with pytest.raises(ValueError, match='admitted native'):
        assets.recover(root, tmp_path / 'download')
    assert not (tmp_path / 'download').exists()
