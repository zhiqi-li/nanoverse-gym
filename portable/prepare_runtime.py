"""Join official native preparation to the existing immutable Gym runtime."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from portable.native_inventory import digest
from portable.official_install import INSTALLERS, OUTPUT, PAIR, PUBLIC_BASE, public_base
from portable.official_install import ROOT as NATIVE_ROOT
from portable.release import ReleaseInput, verified_source
from portable.gym_version import GYM_VERSION

ROOT = Path('/opt/nvgym')
ACCEPTED_RELEASE_SHA256 = '87c4339072b93d3420f062c373069b3070e45880012beb72df2db786f157fae3'
ACCEPTED_RELEASES = {
    'molmospaces': 'd0f268c15539dd68c92cf988bf435aa4920aa96a142dba2894252abfcd21a5cf',
    'robotwin': 'a7e24074bfe85a6bf3215397894e0cd2f2a4b035014b3b810bf909f1621735fd',
    'behavior1k': '299557216ad3adfa615bb61227d5b52ab9cd006fe992b19c91ef5ffec435e33e',
    'exemplar': 'c63e282c5a63b4747d9fad858f56f53c24e7242a14b924dacd74af955c6f225e',
    'robodojo': ACCEPTED_RELEASE_SHA256,
    'roboforge': '027db4e51f7fac45018d547756284d7cd13b968ee5e0ea9d2b7681fa86f80665',
    'libero': 'bff1cd9f95e6428c184c8c3298136203acbe11b5a55848c85aa4de3395d85859',
    'robolab': '738db31bf21322c192f53ffc396322058a2e879388d1d7c3b6c2c88c408f84d5',
    'robocasa365': '145cf545c4d42ec4a70b97509738562b75fb66a9beb834a240bef197b321d6f6',
}
COMMON_INPUTS_SHA256 = '3d4be4420be335c73ad59a34525138e8d2ce1d14e55e8a9f8b89cb41511a02f9'


def derive_release(inputs, destination, descriptor_sha256, *, fixture_python=None):
    original = inputs / 'release.json'
    document = json.loads(original.read_text())
    environment = document.get('environment')
    if digest(original) != ACCEPTED_RELEASES.get(environment):
        raise ValueError('Original accepted RoboDojo release differs' if environment == 'robodojo' or environment is None
                         else 'Original accepted release differs for ' + str(environment))
    config = json.loads(verified_source(inputs, document['config_source']).read_text())
    verified_source(inputs, document['registry_source'])
    if document['qualification']['native_pair'] != {'commit': PAIR}:
        raise ValueError('Accepted paired source identity differs')
    prefix = document['native'].get('prefix')
    bound = {(item['document'], item['pointer']) for item in document['bindings']}

    def relocate(value, pointer=''):
        if isinstance(value, dict):
            for name, item in value.items():
                relocate(item, pointer + '/' + name.replace('~', '~0').replace('/', '~1'))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                relocate(item, pointer + '/' + str(index))
        elif (prefix is not None and isinstance(value, str) and (value == prefix or value.startswith(prefix + '/'))
              and ('config', pointer) not in bound):
            document['bindings'].append({'document': 'config', 'pointer': pointer,
                                         'expected': value,
                                         'destination': str(NATIVE_ROOT) + value[len(prefix):]})

    relocate(config)
    for binding in document['bindings']:
        target = binding['destination']
        if prefix is not None and (target == prefix or target.startswith(prefix + '/')):
            binding['destination'] = str(NATIVE_ROOT) + target[len(prefix):]
    if environment in ('robodojo', 'roboforge'):
        source = NATIVE_ROOT / 'RoboDojo'
        for name, target in (('robodojo', source), ('assets', source),
                             ('home_robot_config', source / 'Assets/Robots/x5/curobo_tmp.yml')):
            matching = [item for item in document['bindings']
                        if item['document'] == 'config' and item['pointer'] == '/' + name]
            if matching:
                matching[0]['destination'] = str(target)
            else:
                document['bindings'].append({'document': 'config', 'pointer': '/' + name,
                                             'expected': config[name], 'destination': str(target)})
    python = NATIVE_ROOT / ('native-venv/bin/python' if environment == 'robolab' else 'venv/bin/python')
    document['native'] = {'kind': 'installed_software', 'image': public_base(environment),
                          'descriptor_path': str(OUTPUT / 'sealed/installed-software.json'),
                          'descriptor_sha256': descriptor_sha256,
                          'payload_path': str(NATIVE_ROOT), 'prefix': str(NATIVE_ROOT),
                          'python': str(python)}
    if environment == 'exemplar':
        if not fixture_python:
            raise ValueError('Exemplar requires its prepared CPU fixture interpreter')
        document['native'] = {'kind': 'fixture', 'image': PUBLIC_BASE, 'python': str(fixture_python)}
    document['qualification'].update(kind='official-origin preparation of the accepted case',
                                     accepted_release_sha256=digest(original))
    if environment == 'behavior1k':
        document['qualification']['kind'] = 'official-source software with recorded SDK dependency profile; native evaluation requires external data'
    destination.mkdir(parents=True, exist_ok=False)
    for field in ('config_source', 'registry_source'):
        target = destination / (field + '.json')
        shutil.copyfile(verified_source(inputs, document[field]), target)
        document[field]['path'] = target.name
    original_config = json.loads(json.dumps(config))
    if 'base_url' not in config['model']:
        config['model']['base_url'] = 'http://127.0.0.1:9/v1'
    if environment == 'libero':
        config['native']['env'] = {**config['native'].get('env', {}),
                                   'NUMBA_CACHE_DIR': '/tmp/nvgym-libero-numba'}
    if environment == 'behavior1k':
        config['native']['env'] = {**config['native'].get('env', {}),
                                   'PYTHONDONTWRITEBYTECODE': '1',
                                   'NUMBA_CACHE_DIR': '/tmp/nvgym-behavior-numba'}
    if environment == 'molmospaces':
        config['native'].pop('filament_python', None)
        document['bindings'] = [binding for binding in document['bindings']
                                if (binding['document'], binding['pointer']) != ('config', '/native/filament_python')]
        document['qualification']['prepared_scope'] = 'accepted close_v1-890 case with classic renderer'
    if config != original_config:
        document['qualification']['accepted_config_sha256'] = document['config_source']['sha256']
        target = destination / document['config_source']['path']
        target.write_text(json.dumps(config, indent=2) + '\n')
        document['config_source']['sha256'] = digest(target)
    for artifact in document['artifacts']:
        target = destination / Path(artifact['path']).name
        shutil.copyfile(verified_source(inputs, artifact), target)
        artifact['path'] = target.name
    path = destination / 'release.json'
    path.write_text(json.dumps(document, indent=2) + '\n')
    return ReleaseInput.load(path)


def prepare_fixture():
    root = Path('/opt/envsdk/fixture')
    root.mkdir(parents=True, exist_ok=False)
    env = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8',
           'UV_PYTHON_INSTALL_DIR': str(root / 'python'), 'UV_CACHE_DIR': str(root / 'cache'),
           'UV_PYTHON_PREFERENCE': 'only-managed'}
    subprocess.run(['uv', 'python', 'install', '3.11.15'], env=env, check=True)
    python = Path(subprocess.check_output(
        ['uv', 'python', 'find', '3.11.15'], env=env, text=True).strip())
    if not python.resolve(strict=True).is_relative_to(root / 'python'):
        raise ValueError('Fixture interpreter escaped its owned installation')
    venv = python.parent.parent / 'fixture-venv'
    subprocess.run(['uv', 'venv', '--python', str(python), '--seed', str(venv)], env=env, check=True)
    python = venv / 'bin/python'
    subprocess.run(['uv', 'pip', 'install', '--python', str(python), '--no-compile',
                    'numpy==1.26.0', 'pillow==11.3.0', 'scipy==1.15.3', 'msgpack==1.2.2',
                    'pyyaml==6.0.2', 'httpx==0.28.1', 'python-fcl==0.7.0.11'], env=env, check=True)
    shutil.rmtree(root / 'cache')
    (venv / '.lock').unlink(missing_ok=True)
    (root / 'python/.lock').unlink(missing_ok=True)
    return {'fixture_python': str(python), 'scope': 'CPU contract fixture; not a benchmark result'}


def stage_forge_task(inputs):
    original = inputs / 'release.json'
    if digest(original) != ACCEPTED_RELEASES['roboforge']:
        raise ValueError('Original accepted RoboForge release differs')
    release = ReleaseInput.load(original)
    if len(release.artifacts) != 1:
        raise ValueError('RoboForge requires one exact authored task package')
    artifact, = release.artifacts
    if set(artifact) != {'path', 'sha256', 'archive_root', 'destination', 'manifest_sha256'}:
        raise ValueError('Invalid authored task artifact')
    target = Path(artifact['destination'])
    tasks = ROOT / 'tasks'
    if not target.is_relative_to(tasks) or target == tasks:
        raise ValueError('Authored task must be below the image tasks directory')
    relative = Path(artifact['archive_root'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid task archive root')
    if (release.config['task_package'] != str(target)
            or release.config['task_package_sha256'] != artifact['manifest_sha256']):
        raise ValueError('Authored task differs from the accepted case')
    archive = verified_source(inputs, artifact)
    with tempfile.TemporaryDirectory(prefix='nvgym-forge-task-') as temporary:
        with tarfile.open(archive) as stream:
            stream.extractall(temporary, filter='data')
        source = Path(temporary) / relative
        if digest(source / 'native_package.json') != artifact['manifest_sha256']:
            raise ValueError('Authored task manifest hash mismatch')
        shutil.copytree(source, target)
    return {'task_package': str(target), 'layout_id': release.config['layout_id']}


def release_inventory(root):
    files = {}
    for name in ('code', 'runtime', 'bootstrap', 'evaluator', 'evaluator-python', 'gym', 'inputs', 'deployment', 'tasks'):
        for path in sorted((root / name).rglob('*')):
            relative = str(path.relative_to(root))
            if relative in {'deployment/runtime.json', 'deployment/release-files.json'}:
                continue
            if path.is_symlink():
                files[relative] = {'link': os.readlink(path)}
            elif path.is_file():
                files[relative] = {'sha256': digest(path)}
    files['common-inputs.json'] = {'sha256': digest(root / 'common-inputs.json')}
    return files


def prepare(*, environment='robodojo', accept_isaac_terms=False, isolation='trusted',
            installed_descriptor_sha256=None, assets_cache=None):
    from portable.native_admission import verify_admission
    from portable.native_host import CLEAN_ENV, PROBE, set_venv_home
    from portable.official_install import install, verify
    from portable.runtime import verify_release

    if installed_descriptor_sha256 and assets_cache is not None:
        raise ValueError('Choose installed software or an assets cache')
    deployment = ROOT / 'deployment'
    metadata_path = deployment / 'runtime.json'
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())
        if metadata.get('isolation', 'trusted') != isolation:
            raise ValueError('Prepared runtime isolation differs from the selected mode')
        if (metadata.get('environment') != environment
                or metadata.get('provenance', {}).get('release_input', {}).get(
                    'native_input', {}).get('kind') != ('fixture' if environment == 'exemplar' else 'installed_software')):
            raise ValueError('Existing runtime does not match the selected official-source environment')
        receipt_path = deployment / 'native-admission.json'
        if digest(receipt_path) != metadata['provenance']['native_admission_sha256']:
            raise ValueError('Existing runtime admission differs')
        verify_release(metadata['provenance'])
        verify_admission(metadata, json.loads(receipt_path.read_text()))
        return {'status': 'already_prepared', 'environment': environment}
    if any((ROOT / name).exists() for name in ('sdk', 'native-host')):
        raise ValueError('Incomplete derived runtime; restart from a clean common image')
    if digest(ROOT / 'common-inputs.json') != COMMON_INPUTS_SHA256:
        raise ValueError('Common runtime input manifest differs from the qualified package')
    common = json.loads((ROOT / 'common-inputs.json').read_text())
    if common['schema'] != 'nvgym.common-runtime.v1':
        raise ValueError('Unsupported common runtime inputs')
    for name, expected in common['files'].items():
        path = ROOT / name
        if 'link' in expected:
            if not path.is_symlink() or os.readlink(path) != expected['link']:
                raise ValueError('Common runtime link differs: ' + name)
        elif digest(path) != expected['sha256']:
            raise ValueError('Common runtime file differs: ' + name)
    descriptor = OUTPUT / 'sealed/installed-software.json'
    if environment == 'exemplar':
        if installed_descriptor_sha256 or assets_cache is not None:
            raise ValueError('Exemplar does not accept native installed software or asset seeds')
        authority = prepare_fixture()
    elif installed_descriptor_sha256:
        authority = verify(descriptor, installed_descriptor_sha256)
    else:
        options = {'version': '0.2.1', 'task': 'BlocksInBinTask'} if environment == 'robolab' else {}
        if environment == 'roboforge':
            options = stage_forge_task(ROOT / 'inputs' / environment)
        authority = install(ROOT / 'deployment/wheels/nanoverse-0.1.0-py3-none-any.whl',
                            environment=environment, options=options,
                            accept_isaac_terms=accept_isaac_terms, assets_cache=assets_cache)
    if environment != 'exemplar' and json.loads(descriptor.read_text())['environment'] != environment:
        raise ValueError('Installed software selects a different environment')
    release = derive_release(ROOT / 'inputs' / environment, deployment / 'release-inputs',
                              authority.get('descriptor_sha256'), fixture_python=authority.get('fixture_python'))
    if isolation not in ('trusted', 'strict'):
        raise ValueError('Unknown isolation mode')
    ttc_manifest_path = deployment / 'ttc-manifest.json'
    ttc_manifest = json.loads(ttc_manifest_path.read_text())
    ttc_manifest['unsafe_no_host_isolation'] = isolation != 'strict'
    ttc_manifest_path.write_text(json.dumps(ttc_manifest, indent=2)+'\n')
    files = release_inventory(ROOT)
    inventory_path = deployment / 'release-files.json'
    inventory_path.write_text(json.dumps(files, sort_keys=True))
    ttc = json.loads((deployment / 'ttc-manifest.json').read_text())
    metadata = {'environment': release.environment, 'episode_seconds': release.episode_seconds, 'isolation': isolation,
                'config': release.config, 'case': release.case,
                'provenance': {'scope': 'operator-owned official-source runtime',
                               'gym_version': GYM_VERSION,
                               'native_pair': {'commit': PAIR},
                               'release_files_sha256': digest(inventory_path),
                               'ttc': ttc['ttc']['sha256'], 'ttcd': ttc['ttcd']['sha256'],
                               'guest_archive': ttc['image']['sha256'],
                               'node': digest(deployment / 'node'),
                               'codex': digest(deployment / 'codex'),
                               'release_input': release.provenance}}
    if environment == 'behavior1k':
        from portable.behavior_data import source_contract
        metadata['external_data'] = source_contract(ROOT / 'code/nanoverse')
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
    process_env = {'PATH': '/opt/nvgym/deployment:/opt/nvgym/runtime/python/bin:'
                           '/usr/local/bin:/usr/bin:/bin',
                   'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': '/opt/nvgym/gym'}
    python = release.native['python']
    subprocess.run([sys.executable, '-B', '-m', 'portable.native_admission', 'prepare'],
                   cwd=ROOT / 'gym', env=process_env, check=True)
    subprocess.run([python, '-B', '-I', '-m', 'venv', '--without-pip', str(ROOT / 'sdk')],
                   cwd=ROOT / 'gym', env=process_env, check=True)
    identity = json.loads(subprocess.check_output(
        [python, '-B', '-I', '-c', PROBE], env=CLEAN_ENV, text=True, timeout=120))
    set_venv_home(ROOT / 'sdk', identity['base_prefix'])
    # RoboLab's frozen uv runtime has no pip; its same-ABI setup venv owns pip.
    packager = str(NATIVE_ROOT / 'venv/bin/python') if environment == 'robolab' else python
    if packager != python:
        packaging_identity = json.loads(subprocess.check_output(
            [packager, '-B', '-I', '-c', PROBE], env=CLEAN_ENV, text=True, timeout=120))
        if any(packaging_identity[name] != identity[name] for name in ('abi', 'base_prefix')):
            raise ValueError('SDK dependency packager differs from the admitted native Python ABI/base')
    subprocess.run([packager, '-B', '-I', '-m', 'pip', 'install', '--prefix', str(ROOT / 'sdk'),
                    '--ignore-installed', '--no-deps', '--no-compile', '--only-binary=:all:',
                    '--no-cache-dir', 'numpy==1.26.4', 'pillow==11.3.0'],
                   cwd=ROOT / 'gym', env=process_env, check=True)
    subprocess.run([sys.executable, '-B', '-m', 'portable.native_admission', 'validate'],
                   cwd=ROOT / 'gym', env=process_env, check=True)
    return {'status': 'prepared', 'environment': environment,
            **authority,
            'native_admission_sha256': digest(deployment / 'native-admission.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=['exemplar', 'robodojo', *INSTALLERS], default='robodojo')
    parser.add_argument('--accept-isaac-terms', action='store_true')
    parser.add_argument('--isolation', choices=['trusted', 'strict'], default='trusted')
    parser.add_argument('--installed-descriptor-sha256')
    parser.add_argument('--assets-cache', type=Path)
    args = parser.parse_args()
    try:
        result = prepare(environment=args.environment, accept_isaac_terms=args.accept_isaac_terms,
                         isolation=args.isolation,
                         installed_descriptor_sha256=args.installed_descriptor_sha256,
                         assets_cache=args.assets_cache)
    except Exception:
        for log in sorted((NATIVE_ROOT / 'evidence').glob('*/commands.log'))[-1:]:
            try:
                with log.open('rb') as source:
                    source.seek(max(0, log.stat().st_size - 131072))
                    tail = source.read().decode(errors='replace')
                print(f'Native preparation diagnostics: {log}\n{tail}', file=sys.stderr, flush=True)
            except OSError:
                pass
        raise
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
