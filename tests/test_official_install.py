import json

import pytest

from portable import official_install as owner
from portable.native_inventory import digest, inventory


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    root = tmp_path / 'payload'
    root.mkdir()
    (root / 'software').write_bytes(b'installed native bytes')
    output = tmp_path / 'preparation'
    output.mkdir()
    monkeypatch.setattr(owner, 'ROOT', root)
    monkeypatch.setattr(owner, 'OUTPUT', output)
    files, identity = inventory(root, directory_modes=True)
    owner.write_json(output / 'installed-files.json', files)
    record = {'schema': owner.SCHEMA, 'environment': 'robodojo', 'root': str(root),
              'root_mode': root.stat().st_mode & 0o777, 'paired_revision': owner.PAIR,
              'public_base': owner.PUBLIC_BASE, 'owners': owner.OWNERS,
              'python': str(root / 'venv/bin/python'), 'wheel': {'revision': owner.PAIR},
              'recipe': {'source_commit': owner.SOURCE_REVISION, 'assets_commit': owner.REVISION},
              'preparation': {'native_closure': {'self_contained': True, 'dependency_roots': [str(root)]}},
              'preparation_owner_sha256': digest(owner.__file__), 'inventory': identity,
              'files_sha256': digest(output / 'installed-files.json')}
    descriptor = output / 'installed-software.json'
    owner.write_json(descriptor, record)
    return root, descriptor, record


def test_sealed_official_software_is_verified_read_only(sealed):
    root, descriptor, _ = sealed
    before = {p: p.read_bytes() for p in descriptor.parent.rglob('*') if p.is_file()}
    result = owner.verify(descriptor, digest(descriptor))
    assert result['mode'] == 'installed_software'
    assert result['original_prefix'] == str(root)
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('change', ['bytes', 'new_file', 'mode', 'root_mode'])
def test_verify_rejects_changed_native_software(sealed, change):
    root, descriptor, _ = sealed
    expected = digest(descriptor)
    if change == 'bytes':
        (root / 'software').write_bytes(b'changed native bytes!!')
    elif change == 'new_file':
        (root / 'unexpected').write_bytes(b'new')
    elif change == 'mode':
        (root / 'software').chmod(0o600)
    else:
        root.chmod(0o700)
    with pytest.raises(ValueError, match='inventory differs'):
        owner.verify(descriptor, expected)


def test_verify_rejects_rewritten_descriptor(sealed):
    _, descriptor, record = sealed
    expected = digest(descriptor)
    record['inventory']['sha256'] = 'a' * 64
    descriptor.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='descriptor SHA256'):
        owner.verify(descriptor, expected)


@pytest.mark.parametrize('field,value', [
    ('paired_revision', 'other'), ('public_base', 'other'), ('environment', 'exemplar'),
    ('owners', {}), ('python', '/other/python'), ('preparation_owner_sha256', '0' * 64),
    ('preparation', {}),
])
def test_verify_rejects_wrong_provenance(sealed, field, value):
    _, descriptor, record = sealed
    record[field] = value
    descriptor.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='provenance differs'):
        owner.verify(descriptor, digest(descriptor))


def test_verify_rejects_changed_inventory_file(sealed):
    _, descriptor, _ = sealed
    (descriptor.parent / 'installed-files.json').write_text('{}')
    with pytest.raises(ValueError, match='file records differ'):
        owner.verify(descriptor, digest(descriptor))


def test_install_does_not_proceed_without_operator_terms_acceptance(tmp_path):
    with pytest.raises(ValueError, match='accept-isaac-terms'):
        owner.install(tmp_path / 'not-read.whl')


def test_initial_preparation_cleans_only_owned_installer_caches(tmp_path):
    root = tmp_path / 'payload'
    for name in ('cache/uv', 'cache/pip', 'cache/huggingface', 'cache/torch', 'python'):
        path = root / name
        path.mkdir(parents=True)
        (path / 'keep-or-remove').write_bytes(b'content')
    (root / 'python/.lock').write_bytes(b'')
    scripts = root / owner.KIT_SCRIPTS
    (scripts / 'new_stage').mkdir(parents=True)
    scripts.chmod(0o777)
    (scripts / 'new_stage').chmod(0o777)
    result = owner.prepare_inventory(root)
    assert set(result['removed_installer_caches']) == {
        'cache/uv', 'cache/pip', 'cache/huggingface', 'python/.lock'}
    assert (root / 'cache/torch/keep-or-remove').read_bytes() == b'content'
    assert (root / 'python/keep-or-remove').read_bytes() == b'content'
    assert scripts.stat().st_mode & 0o777 == 0o755
    assert (scripts / 'new_stage').stat().st_mode & 0o777 == 0o755
    assert not owner.prepare_inventory(root)['removed_installer_caches']


def test_initial_preparation_refuses_external_cache_link(tmp_path):
    root = tmp_path / 'payload'
    (root / 'cache').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_bytes(b'preserve')
    (root / 'cache/uv').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='escaped its owned prefix'):
        owner.prepare_inventory(root)
    assert (outside / 'data').read_bytes() == b'preserve'


@pytest.fixture
def native_paths(tmp_path, monkeypatch):
    root = tmp_path / 'payload'
    python = root / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('owned interpreter fixture')
    python.chmod(0o755)
    base = root / 'python'
    base.mkdir()
    site = root / 'venv/lib/site-packages'
    site.mkdir(parents=True)
    identity = {'implementation': 'cpython', 'abi': 'cp311-test',
                'base_prefix': str(base), 'base_executable': str(python),
                'executable_target': str(python), 'prefix': str(root / 'venv'),
                'sites': [str(site)], 'startup_modules': {}, 'path': [str(base)],
                'editable_hooks': {}}
    monkeypatch.setattr(owner.subprocess, 'check_output', lambda *args, **kwargs: json.dumps(identity))
    return root, site, identity


def test_initial_seal_requires_python_dependencies_inside_payload(native_paths):
    root, _, _ = native_paths
    assert owner.native_closure(root)['dependency_roots'] == [str(root)]


@pytest.mark.parametrize('missing', [False, True])
def test_initial_seal_rejects_kit_extensions_in_external_home(native_paths, tmp_path, missing):
    root, site, _ = native_paths
    extension = tmp_path / 'temporary-home/extension'
    if not missing:
        extension.mkdir(parents=True)
    (site / 'kit-extension').symlink_to(extension, target_is_directory=True)
    with pytest.raises((ValueError, FileNotFoundError)):
        owner.native_closure(root)


def test_initial_seal_rejects_external_python_base(native_paths, tmp_path):
    root, _, identity = native_paths
    base = tmp_path / 'external-python'
    base.mkdir()
    identity['base_prefix'] = str(base)
    with pytest.raises(ValueError, match='Python base escapes'):
        owner.native_closure(root)
