"""Join official native preparation to the existing immutable Gym runtime."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from portable.native_inventory import digest
from portable.official_install import OUTPUT, PAIR, PUBLIC_BASE
from portable.official_install import ROOT as NATIVE_ROOT
from portable.release import ReleaseInput, verified_source

ROOT = Path('/opt/nvgym')
ACCEPTED_RELEASE_SHA256 = '87c4339072b93d3420f062c373069b3070e45880012beb72df2db786f157fae3'
COMMON_INPUTS_SHA256 = '3d4be4420be335c73ad59a34525138e8d2ce1d14e55e8a9f8b89cb41511a02f9'


def derive_release(inputs, destination, descriptor_sha256):
    original = inputs / 'release.json'
    if digest(original) != ACCEPTED_RELEASE_SHA256:
        raise ValueError('Original accepted RoboDojo release differs')
    document = json.loads(original.read_text())
    config = json.loads(verified_source(inputs, document['config_source']).read_text())
    verified_source(inputs, document['registry_source'])
    if document['qualification']['native_pair'] != {'commit': PAIR}:
        raise ValueError('Accepted paired source identity differs')
    source = NATIVE_ROOT / 'RoboDojo'
    for name, target in (('robodojo', source), ('assets', source),
                         ('home_robot_config', source / 'Assets/Robots/x5/curobo_tmp.yml')):
        document['bindings'].append({'document': 'config', 'pointer': '/' + name,
                                     'expected': config[name], 'destination': str(target)})
    document['native'] = {'kind': 'installed_software', 'image': PUBLIC_BASE,
                          'descriptor_path': str(OUTPUT / 'sealed/installed-software.json'),
                          'descriptor_sha256': descriptor_sha256,
                          'payload_path': str(NATIVE_ROOT), 'prefix': str(NATIVE_ROOT),
                          'python': str(NATIVE_ROOT / 'venv/bin/python')}
    document['qualification'].update(kind='official-origin preparation of the accepted case',
                                     accepted_release_sha256=ACCEPTED_RELEASE_SHA256)
    destination.mkdir(parents=True, exist_ok=False)
    for field in ('config_source', 'registry_source'):
        target = destination / (field + '.json')
        shutil.copyfile(verified_source(inputs, document[field]), target)
        document[field]['path'] = target.name
    path = destination / 'release.json'
    path.write_text(json.dumps(document, indent=2) + '\n')
    return ReleaseInput.load(path)


def release_inventory(root):
    files = {}
    for name in ('code', 'runtime', 'bootstrap', 'gym', 'inputs', 'deployment'):
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


def prepare(*, accept_isaac_terms=False, installed_descriptor_sha256=None, assets_cache=None):
    from portable.native_admission import verify_admission
    from portable.official_install import install, verify
    from portable.runtime import verify_release

    if installed_descriptor_sha256 and assets_cache is not None:
        raise ValueError('Choose installed software or an assets cache')
    deployment = ROOT / 'deployment'
    metadata_path = deployment / 'runtime.json'
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())
        if (metadata.get('environment') != 'robodojo'
                or metadata.get('provenance', {}).get('release_input', {}).get(
                    'native_input', {}).get('kind') != 'installed_software'):
            raise ValueError('Existing runtime is not official-source RoboDojo')
        receipt_path = deployment / 'native-admission.json'
        if digest(receipt_path) != metadata['provenance']['native_admission_sha256']:
            raise ValueError('Existing runtime admission differs')
        verify_release(metadata['provenance'])
        verify_admission(metadata, json.loads(receipt_path.read_text()))
        return {'status': 'already_prepared', 'environment': 'robodojo'}
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
    if installed_descriptor_sha256:
        authority = verify(descriptor, installed_descriptor_sha256)
    else:
        authority = install(ROOT / 'deployment/wheels/nanoverse-0.1.0-py3-none-any.whl',
                            accept_isaac_terms=accept_isaac_terms, assets_cache=assets_cache)
    release = derive_release(ROOT / 'inputs/robodojo', deployment / 'release-inputs',
                              authority['descriptor_sha256'])
    files = release_inventory(ROOT)
    inventory_path = deployment / 'release-files.json'
    inventory_path.write_text(json.dumps(files, sort_keys=True))
    ttc = json.loads((deployment / 'ttc-manifest.json').read_text())
    metadata = {'environment': release.environment, 'episode_seconds': release.episode_seconds,
                'config': release.config, 'case': release.case,
                'provenance': {'scope': 'operator-owned official-source runtime',
                               'gym_commit': '69c2ff3ebd6e7b6c2106f5f8cb1ae5c0f912b8bb',
                               'native_pair': {'commit': PAIR},
                               'release_files_sha256': digest(inventory_path),
                               'ttc': ttc['ttc']['sha256'], 'ttcd': ttc['ttcd']['sha256'],
                               'guest_archive': ttc['image']['sha256'],
                               'node': digest(deployment / 'node'),
                               'codex': digest(deployment / 'codex'),
                               'release_input': release.provenance}}
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
    environment = {'PATH': '/opt/nvgym/deployment:/opt/nvgym/runtime/python/bin:'
                           '/usr/local/bin:/usr/bin:/bin',
                   'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': '/opt/nvgym/gym:/opt/nvgym/runtime/site:/opt/nvgym/runtime/gym'}
    python = str(NATIVE_ROOT / 'venv/bin/python')
    commands = [[sys.executable, '-B', '-m', 'portable.native_admission', 'prepare'],
                [python, '-B', '-I', '-m', 'venv', '--without-pip', str(ROOT / 'sdk')],
                [python, '-B', '-I', '-m', 'pip', 'install', '--prefix', str(ROOT / 'sdk'),
                 '--ignore-installed', '--no-deps', '--no-compile', '--only-binary=:all:',
                 '--no-cache-dir', 'numpy==1.26.4', 'pillow==11.3.0'],
                [sys.executable, '-B', '-m', 'portable.native_admission', 'validate']]
    for command in commands:
        subprocess.run(command, cwd=ROOT / 'gym', env=environment, check=True)
    return {'status': 'prepared', 'environment': 'robodojo',
            'installed_descriptor_sha256': authority['descriptor_sha256'],
            'native_admission_sha256': digest(deployment / 'native-admission.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accept-isaac-terms', action='store_true')
    parser.add_argument('--installed-descriptor-sha256')
    parser.add_argument('--assets-cache', type=Path)
    args = parser.parse_args()
    try:
        result = prepare(accept_isaac_terms=args.accept_isaac_terms,
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
