import json
import shutil
from pathlib import Path

import pytest

from portable.prepare_runtime import derive_release, release_inventory
from portable.release import ReleaseInput


@pytest.fixture
def accepted(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases/robodojo'
    inputs = tmp_path / 'inputs'
    shutil.copytree(source, inputs)
    return inputs


def test_official_handoff_preserves_accepted_case_and_binds_every_native_path(accepted, tmp_path):
    release = derive_release(accepted, tmp_path / 'derived', 'a' * 64)
    original = ReleaseInput.load(accepted / 'release.json')
    assert release.case == original.case
    for name in ('step_limit', 'sdk', 'roles', 'environment', 'task'):
        assert release.config.get(name) == original.config.get(name)
    assert release.config['robodojo'] == '/opt/envsdk/native/payload/RoboDojo'
    assert release.config['assets'] == release.config['robodojo']
    assert release.config['home_robot_config'].endswith('/Assets/Robots/x5/curobo_tmp.yml')
    assert release.config['simulator_command'] == original.config['simulator_command']
    assert '/osmo/' not in json.dumps(release.config)
    assert release.native['kind'] == 'installed_software'
    assert release.native['prefix'] == release.native['payload_path']


def test_handoff_refuses_changed_accepted_config(accepted, tmp_path):
    (accepted / 'config.json').write_text('{}')
    with pytest.raises(ValueError, match='source hash mismatch'):
        derive_release(accepted, tmp_path / 'derived', 'a' * 64)
    assert not (tmp_path / 'derived').exists()


def test_handoff_refuses_changed_accepted_release(accepted, tmp_path):
    (accepted / 'release.json').write_text('{}')
    with pytest.raises(ValueError, match='accepted RoboDojo release differs'):
        derive_release(accepted, tmp_path / 'derived', 'a' * 64)


def test_handoff_rejects_reused_output(accepted, tmp_path):
    target = tmp_path / 'derived'
    target.mkdir()
    marker = target / 'keep'
    marker.write_text('preserve')
    with pytest.raises(FileExistsError):
        derive_release(accepted, target, 'a' * 64)
    assert marker.read_text() == 'preserve'


def test_release_inventory_excludes_dynamic_metadata_and_includes_original_inputs(tmp_path):
    for name in ('code/a', 'runtime/a', 'bootstrap/lib/control.py', 'gym/a', 'inputs/robodojo/config.json',
                 'deployment/runtime.json', 'deployment/release-files.json', 'deployment/tool',
                 'common-inputs.json'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (tmp_path / 'code/link').symlink_to('a')
    records = release_inventory(tmp_path)
    assert 'deployment/runtime.json' not in records
    assert 'deployment/release-files.json' not in records
    assert 'inputs/robodojo/config.json' in records
    assert records['code/link'] == {'link': 'a'}
    assert 'deployment/tool' in records and 'common-inputs.json' in records
    assert 'bootstrap/lib/control.py' in records


@pytest.mark.parametrize('environment,kind', [('exemplar', 'fixture'), ('robodojo', 'expanded_seed')])
def test_preparation_does_not_relabel_existing_private_or_fixture_runtime(tmp_path, monkeypatch, environment, kind):
    from portable import prepare_runtime

    deployment = tmp_path / 'deployment'
    deployment.mkdir()
    (deployment / 'runtime.json').write_text(json.dumps({
        'environment': environment, 'provenance': {'release_input': {'native_input': {'kind': kind}}}}))
    monkeypatch.setattr(prepare_runtime, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='not official-source RoboDojo'):
        prepare_runtime.prepare()
