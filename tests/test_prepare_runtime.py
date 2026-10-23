import json
import shutil
from pathlib import Path

import pytest

from portable.prepare_runtime import PUBLIC_BASE, derive_release, release_inventory
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


def test_libero_offline_validation_has_endpoint_without_changing_accepted_input(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases/libero'
    original = json.loads((source / 'release.json').read_text())
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.config['model']['base_url'] == 'http://127.0.0.1:9/v1'
    assert release.config['native']['env']['NUMBA_CACHE_DIR'] == '/tmp/nvgym-libero-numba'
    assert 'base_url' not in json.loads((source / 'config.json').read_text())['model']
    assert release.provenance['qualification']['accepted_config_sha256'] == original['config_source']['sha256']


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
    with pytest.raises(ValueError, match='does not match the selected official-source environment'):
        prepare_runtime.prepare()


@pytest.mark.parametrize('environment', ['libero', 'robolab', 'robocasa365'])
def test_official_handoff_relocates_all_native_paths(environment, tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases' / environment
    original = ReleaseInput.load(source / 'release.json')
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.environment == environment
    assert release.case == original.case
    assert release.config['native']['python'] == '/opt/nvgym/native-host/bin/python'
    assert release.config['native']['root'].startswith('/opt/envsdk/native/payload/')
    assert '/osmo/' not in json.dumps(release.config)
    assert release.config['native'].get('task') == original.config['native'].get('task')
    assert release.config['native'].get('cell') == original.config['native'].get('cell')
    assert release.native['kind'] == 'installed_software'
    assert release.native['prefix'] == release.native['payload_path']


def test_forge_handoff_retains_exact_authored_package(tmp_path):
    from portable.release import digest
    source = Path(__file__).resolve().parents[1] / 'releases/roboforge'
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.config['task_package'] == '/opt/nvgym/tasks/align_blocks.native'
    assert release.config['robodojo'] == '/opt/envsdk/native/payload/RoboDojo'
    assert digest(release.source.parent / release.artifacts[0]['path']) == release.artifacts[0]['sha256']
    assert release.config['task_package_sha256'] == release.artifacts[0]['manifest_sha256']


def test_exemplar_stays_an_explicit_cpu_fixture(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases/exemplar'
    release = derive_release(source, tmp_path / 'derived', None,
                             fixture_python='/opt/envsdk/fixture/python/bin/python3.11')
    assert release.native == {'kind': 'fixture', 'image': PUBLIC_BASE,
                              'python': '/opt/envsdk/fixture/python/bin/python3.11'}
    assert release.environment == 'exemplar'


def test_behavior_handoff_installs_software_with_external_data(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases/behavior1k'
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.native['kind'] == 'installed_software'
    assert release.config['native']['env']['PYTHONDONTWRITEBYTECODE'] == '1'
    assert release.config['native']['env']['NUMBA_CACHE_DIR'] == '/tmp/nvgym-behavior-numba'
    assert release.config['native']['data'] == '/data'
    assert release.config['native']['appdata'] == '/output/behavior-appdata'
    assert release.config['native']['task'] == 'turning_on_radio'


@pytest.mark.parametrize('name', ['datasets', 'venv/omnigibson.key', 'BEHAVIOR-1K/.behavior1k-datasets'])
def test_behavior_software_rejects_bundled_licensed_inputs(tmp_path, name):
    from portable.behavior_software import validate_layout
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'private input')
    with pytest.raises(ValueError, match='Licensed data cannot be bundled'):
        validate_layout(tmp_path)


def test_robotwin_preserves_required_cuda121_base(tmp_path):
    from portable.official_install import ROBOTWIN_BASE
    source = Path(__file__).resolve().parents[1] / 'releases/robotwin'
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.native['image'] == ROBOTWIN_BASE
    assert release.config['native']['task'] == 'stack_blocks_two'
    script = (source.parents[1] / 'portable/prepare.sh').read_text()
    assert 'PUBLIC_BASE=' + ROBOTWIN_BASE in script


def test_molmospaces_handoff_keeps_accepted_classic_case_without_unused_renderer(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'releases/molmospaces'
    release = derive_release(source, tmp_path / 'derived', 'a' * 64)
    assert release.native['kind'] == 'installed_software'
    assert release.config['native']['benchmark'] == 'close_v1'
    assert release.config['native']['index'] == 890
    assert 'filament_python' not in release.config['native']
    assert release.config['native']['data'] == '/opt/envsdk/native/payload/molmospaces/.molmospaces-data'
