import hashlib
import json
import os
from pathlib import Path

import pytest

from portable.build_context import dockerfile
from portable.native_admission import ensure_prefix, verify_seed
from portable.release import ReleaseInput, bind_paths, digest


def release_input(tmp_path, environment='robotwin'):
    config = {'integration': environment, 'control': 'direct', 'native': {'seed': 100000},
              'sdk': {'execution': 'ttc', 'python': '/old/sdk/bin/python'},
              'step_limit': 4500, 'roles': {'actor': {'limits': {'model_calls': 40}}}}
    case = {'case_id': 'accepted', 'environment': environment, 'python': '/native/venv/bin/python',
            'config_path': '/original/accepted.json', 'native_seconds': 1200, 'model_calls': 40,
            'evidence': {'sdk_summary_path': 'native/seed-100000/sdk/summary.json'}}
    sources = {}
    for name, value in (('config', config), ('registry', {'cases': [case]})):
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value, indent=2) + '\n')
        sources[name + '_source'] = {'path': path.name, 'sha256': digest(path)}
    document = {'schema': 'nvgym.release-input.v1', 'environment': environment, 'episode_seconds': 1800,
                'case_id': 'accepted', **sources,
                'bindings': [{'document': 'config', 'pointer': '/sdk/python',
                              'expected': '/old/sdk/bin/python', 'destination': '/opt/nvgym/sdk/bin/python'}],
                'native': {'kind': 'expanded_seed', 'image': 'example/seed@sha256:' + 'a' * 64,
                           'descriptor_path': '/opt/envsdk/native/seed.json', 'descriptor_sha256': 'b' * 64,
                           'payload_path': '/opt/envsdk/native/payload', 'prefix': '/native',
                           'python': '/native/venv/bin/python'},
                'artifacts': [], 'qualification': {'status': 'test input only', 'native_pair': {'commit': 'test'}}}
    path = tmp_path / 'release.json'
    path.write_text(json.dumps(document))
    return path, document


def test_compiles_exact_case_without_inventing_policies_or_controls(tmp_path):
    path, _ = release_input(tmp_path)
    release = ReleaseInput.load(path)
    assert release.config['control'] == 'direct'
    assert 'policies' not in release.config
    assert release.config['step_limit'] == 4500
    assert release.config['roles']['actor']['limits']['model_calls'] == 40
    assert release.config['sdk']['python'] == '/opt/nvgym/sdk/bin/python'
    assert release.case['evidence']['sdk_summary_path'] == 'native/seed-100000/sdk/summary.json'
    assert release.case['native_seconds'] == 1200
    assert 'config_path' not in release.case
    assert release.provenance['config_sha256'] == digest(tmp_path / 'config.json')


def test_binding_refuses_stale_source_value():
    config = {'native': {'root': '/changed'}}
    with pytest.raises(ValueError, match='expected value differs'):
        bind_paths(config, {}, [{'document': 'config', 'pointer': '/native/root',
                                 'expected': '/old', 'destination': '/new'}])
    assert config['native']['root'] == '/changed'


def test_binding_refuses_to_create_missing_fields():
    with pytest.raises(ValueError, match='target absent'):
        bind_paths({}, {}, [{'document': 'config', 'pointer': '/policies',
                             'expected': '/old', 'destination': '/new'}])


def test_release_rejects_unsupported_environment(tmp_path):
    path, document = release_input(tmp_path)
    document['environment'] = 'unknown'
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='Unsupported SDK environment'):
        ReleaseInput.load(path)


def test_release_rejects_mutated_source_bytes(tmp_path):
    path, _ = release_input(tmp_path)
    (tmp_path / 'config.json').write_text('{}')
    with pytest.raises(ValueError, match='source hash mismatch'):
        ReleaseInput.load(path)


def test_release_rejects_unpinned_seed_image(tmp_path):
    path, document = release_input(tmp_path)
    document['native']['image'] = 'example/seed:latest'
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='pinned by digest'):
        ReleaseInput.load(path)


def test_fixture_retains_original_python_separately_from_host_binding(tmp_path):
    path, document = release_input(tmp_path, environment='exemplar')
    document['native'] = {'kind': 'fixture', 'image': 'public/python@sha256:' + 'a' * 64,
                          'python': '/usr/local/bin/python3.11'}
    document['bindings'].append({'document': 'case', 'pointer': '/python',
                                 'expected': '/native/venv/bin/python',
                                 'destination': '/opt/nvgym/native-host/bin/python'})
    path.write_text(json.dumps(document))
    release = ReleaseInput.load(path)
    assert release.case['python'] == '/opt/nvgym/native-host/bin/python'
    assert release.native['python'] == '/usr/local/bin/python3.11'
    recipe = dockerfile(release)
    venv = next(json.loads(line[4:]) for line in recipe.splitlines() if line.startswith('RUN [')
                and '"venv"' in line)
    assert venv[0] == release.native['python'] and venv[-1] == '/opt/nvgym/sdk'
    assert recipe.index('"pip", "install"') < recipe.index('"portable.native_admission", "prepare"')
    assert '/opt/nvgym/native-host/bin/python' not in recipe


@pytest.mark.parametrize('python', (None, 'relative/python'))
def test_fixture_requires_an_explicit_absolute_source_python(tmp_path, python):
    path, document = release_input(tmp_path, environment='exemplar')
    document['native'] = {'kind': 'fixture', 'image': 'public/python@sha256:' + 'a' * 64}
    if python is not None:
        document['native']['python'] = python
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        ReleaseInput.load(path)


@pytest.mark.skipif(os.getuid() != 0, reason='Native seed admission requires image-owned root files')
def test_seed_checks_real_inventory_not_only_descriptor(tmp_path):
    payload = tmp_path / 'payload'
    payload.mkdir(mode=0o755)
    entry = payload / 'native.py'
    entry.write_text('original')
    entry.chmod(0o644)
    (payload / 'unused-training-data').symlink_to(tmp_path / 'absent-private-dataset')
    records = {'native.py': {'sha256': digest(entry), 'mode': 0o644},
               'unused-training-data': {'link': str(tmp_path / 'absent-private-dataset')}}
    identity = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    descriptor = tmp_path / 'seed.json'
    descriptor.write_text(json.dumps({'schema': 'nanoverse.native-seed.v1',
                                    'payload': {'sha256': identity, 'bytes': entry.stat().st_size, 'files': 1}}))
    descriptor.chmod(0o644)
    native = {'descriptor_path': str(descriptor), 'descriptor_sha256': digest(descriptor),
              'payload_path': str(payload), 'prefix': str(tmp_path / 'prefix')}
    receipt = verify_seed(native)
    assert receipt['payload']['sha256'] == identity
    assert receipt['missing_absolute_symlinks'] == ['unused-training-data']
    assert Path(native['prefix']).resolve() == payload
    entry.write_text('mutated')
    with pytest.raises(ValueError, match='full inventory differs'):
        verify_seed(native)


def test_seed_rejects_wrong_descriptor_identity_before_reading_payload(tmp_path):
    descriptor = tmp_path / 'seed.json'
    descriptor.write_text('{}')
    with pytest.raises(ValueError, match='descriptor hash mismatch'):
        verify_seed({'descriptor_path': str(descriptor), 'descriptor_sha256': '0' * 64,
                     'payload_path': str(tmp_path / 'missing')})


def test_native_prefix_bootstrap_is_repeatable_and_rejects_existing_installations(tmp_path):
    payload = tmp_path / 'baked'
    payload.mkdir()
    prefix = tmp_path / 'ephemeral/original'
    native = {'payload_path': str(payload), 'prefix': str(prefix)}
    ensure_prefix(native)
    ensure_prefix(native)
    assert prefix.is_symlink() and prefix.resolve() == payload
    prefix.unlink()
    prefix.mkdir()
    with pytest.raises(ValueError, match='different installation'):
        ensure_prefix(native)
    assert prefix.is_dir() and not prefix.is_symlink()



@pytest.mark.parametrize('mutation', ('source', 'receipt'))
def test_runtime_checks_retained_authorities_after_other_final_verification_fails(tmp_path, monkeypatch, mutation):
    import hashlib
    import sys
    from types import SimpleNamespace

    from portable import runtime

    deployment = tmp_path / 'image/deployment'
    deployment.mkdir(parents=True)
    admission = deployment / 'native-admission.json'
    admission.write_text(json.dumps({'trusted_before_collection': True}))
    metadata = {'environment': 'exemplar', 'episode_seconds': 1, 'config': {},
                'case': {'case_id': 'controlled-case'},
                'provenance': {'native_admission_sha256': hashlib.sha256(admission.read_bytes()).hexdigest(),
                               'release_input': {'native_input': {'kind': 'fixture'}}}}
    (deployment / 'runtime.json').write_text(json.dumps(metadata))
    (deployment / 'ttc-manifest.json').write_text(json.dumps({'unsafe_no_host_isolation': True}))
    output = tmp_path / 'output'
    output.mkdir()
    credential = tmp_path / 'unused.env'
    credential.write_text('local-test-no-provider-request')
    monkeypatch.setattr(runtime, 'ROOT', deployment.parent)
    monkeypatch.setattr(sys, 'argv', ['runtime', 'exemplar', '--output-dir', str(output),
                                     '--credentials-file', str(credential)])
    calls = []

    def release_check(provenance):
        calls.append('release')
        if len(calls) > 1 and mutation == 'source':
            raise ValueError('controlled source mutation')

    def admission_check(metadata, retained, **kwargs):
        assert retained == {'trusted_before_collection': True}
        calls.append('admission')
        return {'all_authorities_measured': True}

    def collector(*args, **kwargs):
        episode = output / 'episode'
        actor = episode / 'n/attempt/n/actor'
        actor.mkdir(parents=True)
        (actor / 'ttc-runtime.json').write_text(json.dumps({'state': 'closed'}))
        (episode / 'n/attempt/cleanup.json').write_text(json.dumps({'complete': True}))
        (episode / 'rollouts.jsonl').write_text(json.dumps({'kind': 'native', 'reward': 0.75}) + '\n')
        if mutation == 'receipt':
            admission.write_text('{}')
        return SimpleNamespace(wait=lambda **kwargs: 0, poll=lambda: 0)

    monkeypatch.setattr(runtime, 'verify_release', release_check)
    monkeypatch.setattr(runtime, 'verify_admission', admission_check)
    monkeypatch.setattr(runtime.CaseRegistry, 'load', lambda _: SimpleNamespace(validate_deployment=lambda *args: None))
    monkeypatch.setattr(runtime.subprocess, 'Popen', collector)
    handlers = {sig: runtime.signal.getsignal(sig) for sig in (runtime.signal.SIGINT, runtime.signal.SIGTERM)}
    try:
        with pytest.raises(RuntimeError, match='Post-cleanup immutable'):
            runtime.main()
    finally:
        for sig, handler in handlers.items():
            runtime.signal.signal(sig, handler)
    assert calls == ['release', 'admission', 'release', 'admission']
    report = json.loads((output / 'summary.json').read_text())
    assert report['status'] == 'failed'
    assert report['final_identities'] == {'all_authorities_measured': True}
    assert 'reward' not in report

def test_official_release_retains_canonical_prefix(tmp_path):
    path, document = release_input(tmp_path, environment='robodojo')
    native = document['native']
    native.update(kind='installed_software', prefix='/opt/envsdk/native/payload',
                  python='/opt/envsdk/native/payload/venv/bin/python',
                  descriptor_path='/opt/envsdk/native/preparation/sealed/installed-software.json')
    path.write_text(json.dumps(document))
    release = ReleaseInput.load(path)
    assert release.native == native
    assert release.provenance['native_input']['kind'] == 'installed_software'


@pytest.mark.parametrize('field,value', [('prefix', '/old'), ('python', '/old/python'),
                                       ('descriptor_path', '/old/seed.json')])
def test_official_release_refuses_historical_paths(tmp_path, field, value):
    path, document = release_input(tmp_path, environment='robodojo')
    document['native'].update(kind='installed_software', prefix='/opt/envsdk/native/payload',
                              python='/opt/envsdk/native/payload/venv/bin/python',
                              descriptor_path='/opt/envsdk/native/preparation/sealed/installed-software.json')
    document['native'][field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='canonical installation paths'):
        ReleaseInput.load(path)
