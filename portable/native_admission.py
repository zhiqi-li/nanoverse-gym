"""Admit native image bytes and validate the packaged case during image build."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from environment_servers.nanoverse_sdk.native import CaseRegistry, OperatorCase
from portable.behavior_data import source_contract
from portable.native_host import (
    CLEAN_ENV,
    check_bindings,
    create_native_host,
    host_inventory,
    paired_inventory,
    verify_native_host,
)
from portable.native_inventory import inventory
from portable.release import digest
from portable.sdk_admission import admit_sdk, sdk_inventory, validate_sdk_staging, verify_sdk

ROOT = Path('/opt/nvgym')


def ensure_prefix(native):
    payload, prefix = Path(native['payload_path']), Path(native['prefix'])
    if not payload.is_dir() or payload.is_symlink():
        raise ValueError('Native payload must be an image-owned directory')
    if prefix.exists() or prefix.is_symlink():
        if not prefix.is_symlink() or prefix.resolve() != payload.resolve():
            raise ValueError('Original native prefix already contains a different installation')
    else:
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.symlink_to(payload, target_is_directory=True)


def verify_seed(native, *, repair_prefix=True):
    descriptor, payload = Path(native['descriptor_path']), Path(native['payload_path'])
    if digest(descriptor) != native['descriptor_sha256']:
        raise ValueError('Native seed descriptor hash mismatch')
    seed = json.loads(descriptor.read_text())
    if seed.get('schema') != 'nanoverse.native-seed.v1' or not payload.is_dir() or payload.is_symlink():
        raise ValueError('Invalid expanded native seed')
    for path in (descriptor, payload):
        status = path.lstat()
        if status.st_uid != 0 or status.st_mode & 0o022:
            raise ValueError('Unprotected native seed root')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(line.split(' - ', 1)[0].split()[4])
        if mount == payload or payload in mount.parents:
            raise ValueError('Native payload must be baked into the image, not supplied by a mount')
    _, actual = inventory(payload)
    if seed['payload'] != actual:
        raise ValueError('Native seed full inventory differs from its descriptor')
    if repair_prefix:
        ensure_prefix(native)
    else:
        verify_prefix(native)
    missing_links = [str(path.relative_to(payload)) for path in payload.rglob('*')
                     if path.is_symlink() and os.path.isabs(os.readlink(path)) and not path.exists()]
    return {'mode': 'expanded_seed', 'descriptor_sha256': native['descriptor_sha256'],
            'payload': actual, 'original_prefix': native['prefix'], 'image_baked_payload': True,
            'missing_absolute_symlinks': missing_links}


def software_authority(native):
    descriptor = Path(native['payload_path']).parent / 'software.json'
    return {'kind': 'software_archive', 'descriptor_path': str(descriptor),
            'descriptor_sha256': digest(descriptor), 'payload_path': native['payload_path'],
            'prefix': native['prefix'], 'python': native['python']}


def verify_software(native, authority, *, repair_prefix=True):
    expected = {name: native[name] for name in ('payload_path', 'prefix', 'python')}
    if (authority.get('kind') != 'software_archive'
            or any(authority.get(name) != value for name, value in expected.items())
            or authority.get('descriptor_path') != str(Path(native['payload_path']).parent / 'software.json')):
        raise ValueError('Derived software runtime authority differs from the release input')
    descriptor = Path(authority['descriptor_path'])
    payload = Path(authority['payload_path'])
    if digest(descriptor) != authority['descriptor_sha256']:
        raise ValueError('Native software descriptor hash mismatch')
    software = json.loads(descriptor.read_text())
    if software.get('schema') != 'nvgym.native-software.v1' or software.get('environment') != 'behavior1k':
        raise ValueError('Invalid derived native software descriptor')
    if not payload.is_dir() or payload.is_symlink():
        raise ValueError('Native software payload must be an image-owned directory')
    bindings = {'source_image': native['source_image'], 'public_base': native['public_base'],
                'cache_descriptor_sha256': native['cache_descriptor']['sha256'],
                'selected_inputs': {name: native[name] for name in ('native_archive', 'warp_wheel')},
                'preparation_sha256': native['preparation']['sha256'],
                'original_prefix': native['prefix'], 'python': native['python']}
    if any(software.get(name) != value for name, value in bindings.items()):
        raise ValueError('Derived native software provenance differs from the release input')
    if software['inventory_owner_sha256'] != digest(Path(__file__).with_name('native_inventory.py')):
        raise ValueError('Derived software inventory owner differs')
    for path in (descriptor, payload, payload.parent / 'software-files.json', payload.parent / 'software-members.json'):
        status = path.lstat()
        if path.is_symlink() or status.st_uid != 0 or status.st_mode & 0o022:
            raise ValueError('Unprotected derived software authority')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(line.split(' - ', 1)[0].split()[4])
        if mount == payload or payload in mount.parents:
            raise ValueError('Native payload must be baked into the image, not supplied by a mount')
    records, actual = inventory(payload, directory_modes=True)
    if software['payload'] != actual:
        raise ValueError('Native software full inventory differs from its descriptor')
    for filename, field in (('software-files.json', 'inventory_sha256'),
                            ('software-members.json', 'member_inventory_sha256')):
        if digest(payload.parent / filename) != software[field]:
            raise ValueError('Native software inventory artifact hash mismatch')
    if json.loads((payload.parent / 'software-files.json').read_text()) != records:
        raise ValueError('Native software inventory records differ')
    if repair_prefix:
        ensure_prefix(native)
    else:
        verify_prefix(native)
    for path in payload.rglob('*'):
        relative = path.relative_to(payload)
        if (relative.parts[0] not in ('python', 'venv', 'src')
                or (relative.parts[0] == 'src' and len(relative.parts) > 1
                    and relative.parts[1] != 'BEHAVIOR-1K')
                or any(part in ('.git', '.behavior1k-datasets', 'omnigibson.key', '.env')
                       or part.startswith('.env.') for part in relative.parts)):
            raise ValueError(f'Excluded content in derived native software: {relative}')
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(payload.resolve()):
            raise ValueError(f'Native software link escapes its payload: {relative}')
    if not os.access(native['python'], os.X_OK):
        raise ValueError('Native software interpreter is not executable')
    return {'mode': 'software_archive', 'authority': authority, 'payload': actual,
            'image_baked_payload': True,
            'public_redistribution': 'requires final-layer and software terms review'}


def prepare(native):
    if native['kind'] == 'installed_software':
        from portable.official_install import verify
        return verify(native['descriptor_path'], native['descriptor_sha256'])
    if native['kind'] == 'expanded_seed':
        return verify_seed(native)
    if native['kind'] == 'fixture':
        subprocess.run([native['python'], '-I', '-c', 'pass'], check=True, env=CLEAN_ENV, timeout=120)
        return {'mode': 'fixture', 'image': native['image'], 'python': native['python']}
    if native['kind'] == 'software_archive':
        return verify_software(native, software_authority(native))
    raise ValueError('Unknown native preparation kind')


def verify_prefix(native):
    prefix, payload = Path(native['prefix']), Path(native['payload_path'])
    if not prefix.is_symlink() or prefix.resolve(strict=True) != payload.resolve(strict=True):
        raise ValueError('Original native prefix differs from admission; runtime cannot recreate it')


def verify_authority(native, receipt, metadata, *, restore_prefix=False):
    if native['kind'] == 'installed_software':
        observed = prepare(native)
    elif native['kind'] == 'expanded_seed':
        observed = verify_seed(native, repair_prefix=restore_prefix)
    elif native['kind'] == 'software_archive':
        observed = verify_software(native, metadata['native_authority'], repair_prefix=restore_prefix)
    elif native['kind'] == 'fixture':
        observed = {'mode': 'fixture', 'image': native['image'], 'python': native['python']}
    else:
        raise ValueError('Unknown native authority')
    if observed != receipt['native_host']['native_authority']:
        raise ValueError('Original native authority differs from host admission')
    return observed


def verify_admission(metadata, receipt, *, restore_prefix=False):
    native = metadata['provenance']['release_input']['native_input']
    host = receipt['native_host']
    identities, errors = {}, []
    try:
        identities['native'] = verify_authority(native, receipt, metadata, restore_prefix=restore_prefix)
    except Exception as error:
        errors.append(f'original native: {error}')
    try:
        check_bindings(metadata)
    except Exception as error:
        errors.append(f'interpreter bindings: {error}')
    try:
        if host['entry_policy'].get('git_read_only') is not (metadata['environment'] in ('robodojo', 'roboforge')):
            raise ValueError('Native Git policy differs from the environment contract')
        identities.update(verify_native_host(host, native, host['native_authority'], metadata['provenance']))
    except Exception as error:
        errors.append(f'native host: {error}')
        # A source failure must not suppress the independent host/base comparisons.
        try:
            paired_inventory(metadata['provenance'])
        except Exception as cause:
            errors.append(f'paired source: {cause}')
        try:
            _, observed = host_inventory([Path(name) for name in host['dependency_roots']])
            if observed != host['inventory']:
                raise ValueError('Exact native host inventory differs')
        except Exception as cause:
            errors.append(f'host inventory: {cause}')
        for name, expected in host['base_dependencies'].items():
            try:
                _, observed = inventory(Path(name), directory_modes=True)
                if observed != expected:
                    raise ValueError('Native host base dependency inventory differs')
            except Exception as cause:
                errors.append(f'base dependency {name}: {cause}')
    try:
        if 'sdk' not in receipt:
            sdk_inventory(host)
            raise ValueError('Missing unconditional SDK admission')
        identities.update(verify_sdk(metadata, receipt['sdk'], host))
    except Exception as error:
        errors.append(f'SDK: {error}')
    if errors:
        raise ValueError('Immutable admission failed; ' + '; '.join(errors))
    return identities


def validate_host_startup(metadata):
    # Node drops parent Python settings; the watchdog repeats this boundary.
    program = '''import importlib,json,os,subprocess,sys
from pathlib import Path
import nanoverse.sdk.actor,ember_bridge.cli
from nanoverse.catalog import load_catalog
expected,root,environment=sys.argv[1:]
assert sys.executable == expected
assert Path(nanoverse.sdk.actor.__file__).resolve().is_relative_to(Path(root)/'code/nanoverse')
assert Path(ember_bridge.cli.__file__).resolve().is_relative_to(Path(root)/'code/ember_bridge/src')
descriptor=importlib.import_module(load_catalog(Path(root)/'code/nanoverse')[environment].sdk).DESCRIPTOR
assert descriptor.private_modules
child='import sys,nanoverse.sdk.actor,ember_bridge.cli; assert sys.executable == '+repr(expected)
subprocess.run([sys.executable,'-c',child],check=True,
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONNOUSERSITE':'1'})
print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix,base_prefix=sys.base_prefix,
                     actor=nanoverse.sdk.actor.__file__,bridge=ember_bridge.cli.__file__,recursive_child=True)))'''
    with tempfile.TemporaryDirectory(prefix='nvgym-native-child-') as directory:
        return json.loads(subprocess.check_output(
            [metadata['case']['python'], '-c', program, metadata['case']['python'], str(ROOT),
             metadata['environment']], env=CLEAN_ENV, cwd=directory, text=True, timeout=120))


def validate(metadata, sdk_receipt, native_host_receipt):
    deployment = ROOT / 'deployment'
    if metadata['environment'] == 'behavior1k':
        config = metadata['config']['native']
        if config['data'] != '/data' or config['appdata'] != '/output/behavior-appdata':
            raise ValueError('BEHAVIOR data and appdata bindings differ from the packaged contract')
    config_path = deployment / 'operator-config.json'
    config_path.write_text(json.dumps(metadata['config']))
    case = OperatorCase.model_validate({**metadata['case'], 'config_path': str(config_path)})
    registry = CaseRegistry(cases=(case,))
    registry.validate_deployment(ROOT / 'code/nanoverse', ROOT / 'code/ember_bridge')
    clean = {**CLEAN_ENV, 'PYTHONDONTWRITEBYTECODE': '1'}
    startup = validate_host_startup(metadata)
    subprocess.run([str(case.python), '-m', 'ember_bridge.cli', 'validate', '--config', str(config_path)],
                   check=True, env={**clean, **{name: value for name, value in case.runtime_env.items()
                                              if name != 'PYTHONPATH'}},
                   timeout=120, stdout=subprocess.DEVNULL)
    subprocess.run([str(case.python), '-B', '-I', '-c',
                    'import sys; '
                    'from nanoverse.environment.paths import check_node_revision; '
                    'from nanoverse.sdk.actor import Deployment; '
                    'check_node_revision(sys.argv[1]); Deployment.load(sys.argv[2])',
                    str(ROOT / 'code/nanoverse'), str(deployment / 'ttc-manifest.json')],
                   check=True, env=clean, timeout=120)
    data_contract = None
    if metadata['environment'] == 'behavior1k':
        data_contract = metadata['external_data']
        if source_contract(ROOT / 'code/nanoverse') != data_contract:
            raise ValueError('Packaged BEHAVIOR data contract differs from paired source')
        native = metadata['provenance']['release_input']['native_input']
        if native['kind'] == 'installed_software':
            descriptor = json.loads(Path(native['descriptor_path']).read_text())
            source_commit = descriptor['recipe']['source_commit']
        else:
            descriptor = json.loads(Path(metadata['native_authority']['descriptor_path']).read_text())
            source_commit = descriptor['source_commit']
        if source_commit != data_contract['behavior_commit']:
            raise ValueError('Prepared BEHAVIOR source binding differs from the paired owner')
        observed = json.loads(subprocess.check_output(
            [str(case.python), '-B', '-I', '-c',
             'import json; '
             'from behavior1k_env import install,cells; '
             'print(json.dumps(dict(resources=install.RESOURCES,identity_files=list(install.IDENTITY_FILES),'
             'asset_versions=cells.ASSET_VERSIONS,behavior_commit=cells.BEHAVIOR_COMMIT)))'],
            env=clean, text=True, timeout=120))
        if observed != {name: data_contract[name] for name in observed} or set(observed) != {
                'resources', 'identity_files', 'asset_versions', 'behavior_commit'}:
            raise ValueError('Native BEHAVIOR owner disagrees with the external data contract')
        subprocess.run([str(case.python), '-B', '-I', '-c',
                        'import ctypes; '
                        '[ctypes.CDLL(n) for n in ("libGLU.so.1","libXt.so.6","libvulkan.so.1","libEGL.so.1")]; '
                        'import importlib.metadata as m; '
                        'assert m.version("isaacsim").startswith("5.1.0"); '
                        'assert m.version("warp-lang")=="1.12.0"'],
                       check=True, env=clean, timeout=120)
    modules = json.loads(subprocess.check_output(
        [str(case.python), '-B', '-I', '-c',
         'import importlib,json,sys; '
         'from nanoverse.catalog import load_catalog; '
         'spec=load_catalog(sys.argv[1])[sys.argv[2]]; '
         'descriptor=importlib.import_module(spec.sdk).DESCRIPTOR; '
         'print(json.dumps(list(descriptor.private_modules)))',
         str(ROOT / 'code/nanoverse'), metadata['environment']],
        env=clean, text=True, timeout=120))
    if not modules or not all(isinstance(name, str) and name for name in modules):
        raise ValueError('Paired SDK descriptor must declare private simulator modules')
    modules = list(dict.fromkeys([*modules, 'nanoverse.environment', 'ember_bridge']))
    subprocess.run([str(ROOT / 'sdk/bin/python'), '-B', '-I', '-c',
                    'import importlib.util,json,sys,numpy,PIL; '
                    'names=json.loads(sys.argv[1]); exposed=[]; '
                    '\nfor name in names:\n'
                    ' try: found=importlib.util.find_spec(name)\n'
                    ' except (ModuleNotFoundError,ValueError): found=None\n'
                    ' if found is not None: exposed.append(name)\n'
                    'if exposed: raise RuntimeError("SDK exposes private modules: "+repr(exposed))',
                    json.dumps(modules)], check=True, env=clean, timeout=120)
    sdk_staging = validate_sdk_staging(metadata, sdk_receipt, native_host_receipt)
    config_path.unlink()
    return {'case_registry': True, 'native_config': True, 'paired_source': True,
            'ttc_manifest': True, 'sdk_private_modules_hidden': True, 'native_host_startup': startup,
            'sdk_staging': sdk_staging,
            **({'external_data_owner': data_contract} if data_contract is not None else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'validate'))
    args = parser.parse_args()
    path = ROOT / 'deployment/runtime.json'
    metadata = json.loads(path.read_text())
    native = metadata['provenance']['release_input']['native_input']
    receipt_path = ROOT / 'deployment/native-admission.json'
    if args.phase == 'prepare':
        receipt = prepare(native)
        if native['kind'] == 'software_archive':
            metadata['native_authority'] = receipt['authority']
            path.write_text(json.dumps(metadata, indent=2))
        receipt_path.write_text(json.dumps(receipt, indent=2))
    else:
        receipt = json.loads(receipt_path.read_text())
        observed = prepare(native)
        if observed != receipt:
            raise ValueError('Original native authority changed during SDK installation')
        receipt['native_host'] = create_native_host(
            native, dict(receipt), metadata['provenance'],
            protect_git=metadata['environment'] in ('robodojo', 'roboforge'))
        try:
            receipt['sdk'] = admit_sdk(metadata, receipt['native_host'])
            verify_admission(metadata, receipt)
            receipt['validation'] = validate(metadata, receipt['sdk'], receipt['native_host'])
        finally:
            receipt['final_identities'] = verify_admission(metadata, receipt)
        if native['kind'] == 'expanded_seed':
            receipt['final_seed_unchanged'] = True
        if native['kind'] in ('software_archive', 'installed_software'):
            receipt['final_software_unchanged'] = True
            receipt['system_packages'] = subprocess.check_output(
                ['dpkg-query', '-W', '-f=${binary:Package}=${Version}\n'], text=True, timeout=120).splitlines()
        receipt_path.write_text(json.dumps(receipt, indent=2))
        metadata['provenance']['native_admission_sha256'] = digest(receipt_path)
        path.write_text(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
