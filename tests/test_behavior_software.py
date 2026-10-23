from types import SimpleNamespace

import pytest

from portable.behavior_software import public_recipe


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / 'installed'
    source = root / 'BEHAVIOR-1K'
    for name in ('OmniGibson/omnigibson/omnigibson_5_1_0.kit', 'docs/assets/OmniGibson_logo.png'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'official software resource')
    package = root / 'venv/lib/python3.11/site-packages/isaacsim'
    (package / 'apps').mkdir(parents=True)
    (package / 'kit').mkdir()
    ctx = SimpleNamespace(root=root, source=source, python=root / 'venv/bin/python', pip=lambda *args: None)
    recipe = SimpleNamespace(SPEC={}, source=lambda ctx: None, dependencies=lambda ctx: None,
                             native_config=lambda ctx: {})
    return ctx, public_recipe(recipe), package


def test_software_prepares_runtime_required_extension_roots_before_sealing(installation, monkeypatch):
    ctx, recipe, package = installation
    monkeypatch.setenv('PIP_CONSTRAINT', 'operator-constraints')
    recipe.dependencies(ctx)
    for name in ('exts', 'extscache', 'extsPhysics', 'extsUser', 'extsDeprecated',
                 'kit/exts', 'kit/extsPhysics'):
        directory = package / name
        assert directory.is_dir() and not directory.is_symlink()
        assert directory.stat().st_mode & 0o777 == 0o755
        assert not list(directory.iterdir())
    assert (package / 'apps/omnigibson_5_1_0.kit').read_bytes() == b'official software resource'
    import os
    assert os.environ['PIP_CONSTRAINT'] == 'operator-constraints'


@pytest.mark.parametrize('name', ('exts', 'extscache', 'extsPhysics', 'extsUser', 'extsDeprecated',
                                  'kit/exts', 'kit/extsPhysics'))
def test_software_refuses_an_extension_root_symlink(installation, tmp_path, name):
    ctx, recipe, package = installation
    outside = tmp_path / 'preserve'
    outside.mkdir()
    marker = outside / 'marker'
    marker.write_text('keep')
    (package / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='search-root symlink'):
        recipe.dependencies(ctx)
    assert marker.read_text() == 'keep'
