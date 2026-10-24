"""Assemble explicit release inputs for a Docker build context."""

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from environment_servers.nanoverse_sdk.native import SUPPORTED_ENVIRONMENTS
from portable.behavior_data import source_contract
from portable.gym_version import GYM_VERSION
from portable.release import ReleaseInput, absolute, digest, verified_source

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = Path('/opt/nvgym')


def copy_tree(source, target):
    ignored = shutil.ignore_patterns('.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache',
                                    '.env', '.env.*', '*.pem', '*.key')
    shutil.copytree(source, target, symlinks=True, ignore=ignored)


def dockerfile(release):
    python = str(DESTINATION / 'runtime/python/bin/python3.13')
    environment = 'PYTHONPATH=/opt/nvgym/gym:/opt/nvgym/runtime/site:/opt/nvgym/runtime/gym'
    if release.native['kind'] == 'software_archive':
        packages = ('ca-certificates', 'bash', 'procps', 'curl', 'ffmpeg', 'libgl1', 'libglib2.0-0',
                    'libegl1', 'libglu1-mesa', 'libxt6', 'libvulkan1', 'vulkan-tools', 'libsm6',
                    'libxext6', 'libxrender1', 'libxi6', 'libxrandr2', 'libgomp1', 'libasound2',
                    'libxtst6', 'libxkbcommon0')
        lines = ['FROM ' + release.native['source_image'] + ' AS native-software', 'USER root',
                 'COPY native-source /opt/nvgym-stage',
                 'RUN ' + json.dumps(['python3', '-B', '-I', '/opt/nvgym-stage/software_prepare.py',
                                      '--input', '/opt/nvgym-stage/native.json',
                                      '--output', '/opt/nvgym-stage/native']),
                 'FROM ' + release.native['public_base'], 'USER root',
                 'RUN ' + json.dumps(['sh', '-ec',
                     'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; '
                     'apt-get install -y -qq --no-install-recommends ' + ' '.join(packages) +
                     '; rm -r /var/lib/apt/lists/*']),
                 'COPY --from=native-software /opt/nvgym-stage/native /opt/envsdk/native']
    else:
        lines = ['FROM ' + release.native['image'], 'USER root']
    lines.extend(['COPY payload /opt/nvgym',
             'ENV ' + environment, 'ENV PYTHONDONTWRITEBYTECODE=1', 'WORKDIR /opt/nvgym/gym'])
    native_python = release.native['python']
    if release.native['kind'] == 'fixture':
        lines.append('RUN ' + json.dumps([native_python, '-B', '-I', '-m', 'pip', 'install', '--no-cache-dir',
                     'numpy==1.26.0', 'pillow==11.3.0', 'scipy==1.15.3', 'msgpack==1.2.2',
                     'pyyaml==6.0.2', 'httpx==0.28.1', 'python-fcl==0.7.0.11']))
    lines.extend(['RUN ' + json.dumps([python, '-m', 'portable.native_admission', 'prepare']),
                  'RUN ' + json.dumps([native_python, '-B', '-I', '-m', 'venv', '--without-pip', '/opt/nvgym/sdk']),
                  'RUN ' + json.dumps([native_python, '-B', '-I', '-m', 'pip', 'install',
                                       '--prefix', '/opt/nvgym/sdk', '--ignore-installed', '--no-deps',
                                       '--no-compile', '--only-binary=:all:', '--no-cache-dir',
                                       'numpy==1.26.4', 'pillow==11.3.0']),
                  'RUN ' + json.dumps([python, '-m', 'portable.native_admission', 'validate']),
                  'ENTRYPOINT ' + json.dumps([python, '-m', 'portable.runtime'])])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--bridge-root', type=Path, required=True)
    parser.add_argument('--gym-root', type=Path, required=True)
    parser.add_argument('--gym-version', choices=(GYM_VERSION,), default=GYM_VERSION)
    parser.add_argument('--gym-site', type=Path)
    parser.add_argument('--gym-python-prefix', type=Path, default=Path(sys.base_prefix))
    parser.add_argument('--ttc-manifest', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--node-license', type=Path, required=True)
    parser.add_argument('--codex-program', type=Path, required=True)
    parser.add_argument('--codex-license', type=Path, required=True)
    args = parser.parse_args()
    release = ReleaseInput.load(args.release_input)
    catalog = [line.split('\t')[0] for line in (ROOT / 'portable/images.tsv').read_text().splitlines()[1:]
               if line and not line.startswith('#')]
    if len(catalog) != len(SUPPORTED_ENVIRONMENTS) or set(catalog) != set(SUPPORTED_ENVIRONMENTS):
        raise ValueError('Image catalog must contain exactly the supported SDK environments')
    original_pair = json.loads((args.native_root / 'nanoverse/_revision.json').read_text())
    if original_pair != release.provenance['qualification']['native_pair']:
        raise ValueError('Supplied paired source differs from the qualified source identity')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    payload = output / 'payload'
    payload.mkdir()
    code = payload / 'code'
    code.mkdir()
    for source, name in ((args.native_root, 'nanoverse'), (args.bridge_root, 'ember_bridge')):
        copy_tree(source.resolve(), code / name)
    modules = args.bridge_root / 'node_modules'
    queue = ['typebox', 'ws', 'toml', '@silvia-odwyer/photon-node', 'diff', 'ignore', 'minimatch']
    seen = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        source = (modules / name).resolve(strict=True)
        package = json.loads((source / 'package.json').read_text())
        queue.extend(package.get('dependencies', {}))
        copy_tree(source, code / 'ember_bridge/node_modules' / name)
    link = code / 'ember_bridge/node_modules/@nanoverse/environments'
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to('../../../nanoverse')
    (code / 'nanoverse/node_modules').symlink_to('../ember_bridge/node_modules')
    runtime = payload / 'runtime'
    runtime.mkdir()
    copy_tree(args.gym_python_prefix, runtime / 'python')
    site = args.gym_site or Path(next(path for path in sys.path if path.endswith('site-packages')))
    copy_tree(site, runtime / 'site')
    copy_tree(args.gym_root / 'nemo_gym', runtime / 'gym/nemo_gym')
    copy_tree(args.gym_root / 'responses_api_models', runtime / 'gym/responses_api_models')
    app = payload / 'gym'
    for name in ('environment_servers', 'responses_api_models', 'scripts', 'portable'):
        source = ROOT / name
        target = app / name
        if name == 'scripts':
            target.mkdir(parents=True)
            shutil.copy2(source / 'sdk_smoke.py', target / 'sdk_smoke.py')
        else:
            copy_tree(source, target)
    deployment = payload / 'deployment'
    deployment.mkdir()
    manifest = json.loads(args.ttc_manifest.read_text())
    manifest['resource_mode'] = 'outer_task'
    for name in ('ttc', 'ttcd'):
        source = Path(manifest[name]['path'])
        if digest(source) != manifest[name]['sha256']:
            raise ValueError(f'Invalid {name} release input')
        shutil.copy2(source, deployment / name)
        manifest[name]['path'] = str(DESTINATION / 'deployment' / name)
    archive = Path(manifest['image']['archive'])
    if digest(archive) != manifest['image']['sha256']:
        raise ValueError('Invalid TTC guest archive')
    shutil.copy2(archive, deployment / 'sdk-code-image.tar')
    manifest['image']['archive'] = str(DESTINATION / 'deployment/sdk-code-image.tar')
    (deployment / 'ttc-manifest.json').write_text(json.dumps(manifest))
    ttc_directory = Path(json.loads(args.ttc_manifest.read_text())['ttc']['path']).parent
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ttc_directory / name, deployment / name)
    program = json.loads(args.codex_program.read_text())
    if len(program['program']['assets']) != 1:
        raise ValueError('Expected the qualified single Codex executable')
    asset = program['program']['assets'][0]
    if digest(Path(asset['source'])) != asset['sha256']:
        raise ValueError('Invalid Codex release input')
    shutil.copy2(asset['source'], deployment / 'codex')
    asset['source'] = str(DESTINATION / 'deployment/codex')
    (deployment / 'codex-program.json').write_text(json.dumps(program))
    (deployment / 'programs.json').write_text(json.dumps({
        'schema': 'ember_bridge.guest.programs.v1',
        'programs': {'codex': str(DESTINATION / 'deployment/codex-program.json')}}))
    shutil.copy2(args.node, deployment / 'node')
    licenses = deployment / 'licenses'
    licenses.mkdir()
    for source, name in ((args.node_license, 'node-LICENSE'), (args.codex_license, 'codex-LICENSE')):
        shutil.copy2(source, licenses / name)
    for artifact in release.artifacts:
        if set(artifact) != {'path', 'sha256', 'archive_root', 'destination', 'manifest_sha256'}:
            raise ValueError('A task artifact requires exact archive and manifest identities')
        archive = verified_source(release.source.parent, artifact)
        destination = absolute(artifact['destination'])
        tasks = DESTINATION / 'tasks'
        if not destination.is_relative_to(tasks) or destination == tasks:
            raise ValueError('Task packages must be bundled below /opt/nvgym/tasks')
        relative = Path(artifact['archive_root'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid task archive root')
        with tempfile.TemporaryDirectory(prefix='nvgym-task-') as temporary:
            with tarfile.open(archive) as stream:
                stream.extractall(temporary, filter='data')
            source = Path(temporary) / relative
            if digest(source / 'native_package.json') != artifact['manifest_sha256']:
                raise ValueError('Authored task manifest hash mismatch')
            target = payload / destination.relative_to(DESTINATION)
            copy_tree(source, target)
    if release.environment == 'roboforge':
        package = release.config.get('task_package')
        artifacts = {item['destination']: item for item in release.artifacts}
        if (package not in artifacts
                or artifacts[package]['manifest_sha256'] != release.config.get('task_package_sha256')):
            raise ValueError('RoboForge requires its exact declared authored native package')
    external_data = None
    if release.native['kind'] == 'software_archive':
        external_data = source_contract(args.native_root)
        preparation = verified_source(release.source.parent, release.native['preparation'])
        source_stage = output / 'native-source'
        source_stage.mkdir()
        shutil.copy2(preparation, source_stage / 'software_prepare.py')
        inventory_owner = ROOT / 'portable/native_inventory.py'
        shutil.copy2(inventory_owner, source_stage / 'native_inventory.py')
        (source_stage / 'native.json').write_text(json.dumps({
            'native': release.native, 'behavior_commit': external_data['behavior_commit'],
            'inventory_owner_sha256': digest(inventory_owner)}, indent=2))
    metadata = {
        'environment': release.environment, 'episode_seconds': release.episode_seconds,
        'config': release.config, 'case': release.case,
        'provenance': {'scope': 'release candidate; public availability not qualified',
                       'gym_version': args.gym_version,
                       'native_pair': json.loads((args.native_root / 'nanoverse/_revision.json').read_text()),
                       'ttc': manifest['ttc']['sha256'], 'ttcd': manifest['ttcd']['sha256'],
                       'guest_archive': manifest['image']['sha256'], 'node': digest(args.node),
                       'codex': asset['sha256'], 'release_input': release.provenance}}
    if external_data is not None:
        metadata['external_data'] = external_data
    originals = deployment / 'release-inputs'
    originals.mkdir()
    input_document = json.loads(release.source.read_text())
    for field in ('config_source', 'registry_source'):
        shutil.copy2(verified_source(release.source.parent, input_document[field]), originals / (field + '.json'))
    shutil.copy2(release.source, originals / 'release.json')
    files = {}
    for path in sorted(payload.rglob('*')):
        relative = str(path.relative_to(payload))
        if path.is_symlink():
            files[relative] = {'link': str(path.readlink())}
        elif path.is_file():
            files[relative] = {'sha256': digest(path)}
    inventory = deployment / 'release-files.json'
    inventory.write_text(json.dumps(files, sort_keys=True))
    metadata['provenance']['release_files_sha256'] = digest(inventory)
    (deployment / 'runtime.json').write_text(json.dumps(metadata, indent=2))
    (output / 'Dockerfile').write_text(dockerfile(release))
    print(json.dumps({'context': str(output), 'environment': release.environment}))


if __name__ == '__main__':
    main()
