import pytest

from portable.molmo_public import cache_identity, copy_cache


@pytest.fixture
def cache(tmp_path):
    source = tmp_path / 'input'
    for name in ('assets', 'cache', 'nltk_data'):
        (source / name).mkdir(parents=True)
    (source / 'assets/scene.xml').write_text('<mujoco/>')
    (source / 'cache/scene.xml').symlink_to('../assets/scene.xml')
    return source


def test_portable_data_cache_copies_with_verified_relative_links(cache, tmp_path):
    expected = cache_identity(cache)
    target = tmp_path / 'output'
    files = copy_cache(None, cache, target, expected)
    assert files == [target / 'assets/scene.xml']
    assert (target / 'cache/scene.xml').read_text() == '<mujoco/>'
    assert (target / 'cache/scene.xml').is_symlink()
    assert cache_identity(target) == expected


@pytest.mark.parametrize('target', ['/etc/passwd', '../../outside'])
def test_data_cache_rejects_links_outside_its_root(cache, target):
    (cache / 'assets/unsafe').symlink_to(target)
    with pytest.raises((ValueError, FileNotFoundError)):
        cache_identity(cache)


def test_data_cache_rejects_a_software_tree(cache):
    (cache / 'venv').mkdir()
    with pytest.raises(ValueError, match='only assets, cache, nltk_data'):
        cache_identity(cache)


def test_data_cache_rejects_changed_input_before_copy(cache, tmp_path):
    expected = cache_identity(cache)
    (cache / 'assets/scene.xml').write_text('changed')
    with pytest.raises(ValueError, match='changed before copying'):
        copy_cache(None, cache, tmp_path / 'output', expected)
    assert not (tmp_path / 'output').exists()


def test_data_cache_rejects_an_unverified_archive(tmp_path):
    (tmp_path / 'molmospaces-assets.tar.zst').write_bytes(b'not the declared archive')
    with pytest.raises(ValueError, match='size or SHA256 differs'):
        cache_identity(tmp_path)
