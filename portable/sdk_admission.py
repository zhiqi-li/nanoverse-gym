"""Admit the clean SDK and exercise its unchanged paired staging boundary."""

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from portable.native_host import CLEAN_ENV, protected, reject_mounts
from portable.native_inventory import inventory
from portable.release import digest

ROOT = Path('/opt/nvgym')
SDK = ROOT / 'sdk'
PYTHON = SDK / 'bin/python'
INTERPRETER_NAME = re.compile(r'python(?:[0-9]+(?:\.[0-9]+)*)?\Z')

IDENTITY = '''import json,site,sys,sysconfig
from pathlib import Path
print(json.dumps(dict(executable=sys.executable,
    executable_target=str(Path(sys.executable).resolve(strict=True)), prefix=sys.prefix,
    base_prefix=str(Path(sys.base_prefix).resolve(strict=True)),
    implementation=sys.implementation.name, version=list(sys.version_info[:3]),
    abi=sysconfig.get_config_var('SOABI'),dont_write_bytecode=sys.dont_write_bytecode,
    sites=list(dict.fromkeys([sysconfig.get_path('purelib'), sysconfig.get_path('platlib'),
                              *site.getsitepackages()])))))'''

STAGING = '''import importlib,json,subprocess,sys
from pathlib import Path
from ember_bridge.environments.registry import resolve_config
from nanoverse.catalog import load_catalog
from nanoverse.environment.config import load_config
from nanoverse.sdk.config import effective_capabilities,episode_mechanics,guidance
from nanoverse.sdk.staging import stage_client,probe_interpreter
root,environment,config_path,destination,observation=sys.argv[1:]
config=resolve_config(load_config(config_path))
if config['integration'] != environment:
    raise ValueError('Resolved SDK configuration selects a different environment')
spec=load_catalog(Path(root)/'code/nanoverse')[environment]
descriptor=importlib.import_module(spec.sdk).DESCRIPTOR
sdk=config['sdk']
if sdk['python'] != str(Path(root)/'sdk/bin/python'):
    raise ValueError('Resolved SDK interpreter differs from the admitted binding')
capabilities=effective_capabilities(descriptor,sdk)
parts=guidance(sdk)
mechanics=episode_mechanics(descriptor)
client=Path(destination)
manifest=stage_client(spec,sdk['profile'],client,sdk['python'],
    capabilities=capabilities,guidance=parts,mechanics=mechanics)
owner=probe_interpreter(client,descriptor.private_modules)
minimal={'PATH':str(client/'bin')+':/usr/bin:/bin','PYTHONPATH':str(client)}
result=subprocess.run([str(client/'bin/python3'),'-c',observation],env=minimal,
    cwd=client,capture_output=True,text=True,timeout=60,check=True)
supplement=json.loads(result.stdout.strip().splitlines()[-1])
import hashlib
print(json.dumps(dict(environment=environment,profile=sdk['profile'],
    capabilities=sorted(capabilities),guidance=sorted(parts),mechanics=sorted(mechanics),
    manifest_sha256=hashlib.sha256((client/'MANIFEST.json').read_bytes()).hexdigest(),
    manifest=manifest,owner=owner,supplement=supplement)))'''

OBSERVATION = '''import json,site,sys,sysconfig
from pathlib import Path
import numpy,PIL
print(json.dumps(dict(executable=sys.executable,
    executable_target=str(Path(sys.executable).resolve(strict=True)),prefix=sys.prefix,
    base_prefix=str(Path(sys.base_prefix).resolve(strict=True)),
    implementation=sys.implementation.name,version=list(sys.version_info[:3]),
    abi=sysconfig.get_config_var('SOABI'),dont_write_bytecode=sys.dont_write_bytecode,
    path=sys.path,sites=site.getsitepackages(),
    packages={'numpy':dict(version=numpy.__version__,origin=str(Path(numpy.__file__).resolve(strict=True))),
              'PIL':dict(version=PIL.__version__,origin=str(Path(PIL.__file__).resolve(strict=True)))})))'''


def host_digest(receipt):
    return hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()


def check_identity(metadata, identity, native_host_receipt):
    if metadata['config']['sdk']['python'] != str(PYTHON):
        raise ValueError('SDK interpreter differs from the effective configuration binding')
    backend = PYTHON.with_name('_nvgym-python')
    if identity['executable'] != str(backend) or identity['prefix'] != str(SDK):
        raise ValueError('SDK backend executable or prefix differs from admission')
    if identity['executable_target'] != str(backend.resolve(strict=True)):
        raise ValueError('SDK executable target differs from admission')
    if identity['dont_write_bytecode'] is not True:
        raise ValueError('SDK entrypoint does not disable bytecode before interpreter startup')
    original = native_host_receipt['original_identity']
    if (identity['base_prefix'] != str(Path(original['base_prefix']).resolve(strict=True))
            or any(identity[key] != original[key] for key in ('implementation', 'version', 'abi'))):
        raise ValueError('SDK interpreter changed its admitted native base or ABI')
    if not identity['sites'] or len(identity['sites']) != len(set(identity['sites'])):
        raise ValueError('SDK has no unique own site directory')
    for name in identity['sites']:
        site = Path(name)
        if not site.is_relative_to(SDK) or not site.resolve(strict=True).is_relative_to(SDK.resolve()):
            raise ValueError('SDK site escapes its own prefix')
        if not site.is_dir() or site.is_symlink():
            raise ValueError('SDK site must be an image-owned directory')
        protected(site)


def sdk_inventory(native_host_receipt):
    for root in (ROOT, SDK, ROOT / 'deployment'):
        if root.is_symlink() or not root.is_dir():
            raise ValueError('SDK and inventory roots must be image-owned directories')
        protected(root)
    reject_mounts([SDK, ROOT / 'deployment'])
    roots = [SDK.resolve(strict=True), *(Path(name).resolve(strict=True)
                                       for name in native_host_receipt['dependency_roots'])]
    records, identity = inventory(SDK, directory_modes=True)
    identity['root_mode'] = stat.S_IMODE(SDK.lstat().st_mode)
    for path in SDK.rglob('*'):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not any(target == root or target.is_relative_to(root) for root in roots):
                raise ValueError(f'SDK link escapes admitted native/base authority: {path}')
            protected(target)
    return records, identity


def check_startup(identity):
    config = SDK / 'pyvenv.cfg'
    if config.is_symlink() or not config.is_file():
        raise ValueError('SDK must retain its own pyvenv.cfg')
    values = [line.split('=', 1)[1].strip().lower() for line in config.read_text().splitlines()
              if '=' in line and line.split('=', 1)[0].strip().lower() == 'include-system-site-packages']
    if values != ['false']:
        raise ValueError('SDK must disable native system site packages')
    for name in identity['sites']:
        site = Path(name)
        if any(site.glob('*.pth')):
            raise ValueError('SDK must not install late startup hooks')
        for name in ('sitecustomize', 'usercustomize'):
            if any(site.glob(name + '*')):
                raise ValueError('SDK must not install startup customization modules')


def entrypoint_bytes():
    backend = PYTHON.with_name('_nvgym-python')
    return f'#!/bin/sh\nexec {shlex.quote(str(backend))} -B "$@"\n'.encode()


def construct_entrypoint(native_host_receipt):
    backend = PYTHON.with_name('_nvgym-python')
    if backend.exists() or backend.is_symlink():
        raise ValueError('Refusing a preexisting SDK backend')
    source = PYTHON.resolve(strict=True)
    roots = [SDK.resolve(strict=True), *(Path(name).resolve(strict=True)
                                       for name in native_host_receipt['dependency_roots'])]
    if not source.is_file() or not any(source.is_relative_to(root) for root in roots):
        raise ValueError('SDK original executable escapes admitted authority or is not regular')
    protected(source)
    if not stat.S_ISREG(source.lstat().st_mode) or not os.access(source, os.X_OK):
        raise ValueError('SDK original executable must be a protected executable regular file')
    source_sha256 = digest(source)
    aliases = [path for path in PYTHON.parent.iterdir() if INTERPRETER_NAME.fullmatch(path.name)]
    for path in aliases:
        target = path.resolve(strict=True)
        if (not target.is_file() or not os.access(target, os.X_OK)
                or not any(target.is_relative_to(root) for root in roots)
                or digest(target) != source_sha256):
            raise ValueError(f'SDK interpreter alias names a foreign executable: {path.name}')
        protected(path)
        protected(target)
    # Copy before changing aliases, including indirect python -> python3 chains.
    shutil.copy2(source, backend)
    protected(backend)
    for path in aliases:
        path.unlink()
    with PYTHON.open('xb') as stream:
        stream.write(entrypoint_bytes())
    PYTHON.chmod(0o555)
    for path in aliases:
        if path != PYTHON:
            path.symlink_to('python')
    return {'path': str(backend), 'kind': 'regular', 'source_target': str(source),
            'source_sha256': source_sha256, 'target': str(backend.resolve(strict=True)),
            'sha256': digest(backend)}


def check_entrypoint(receipt, native_host_receipt):
    backend = PYTHON.with_name('_nvgym-python')
    expected = {'path': str(PYTHON), 'sha256': digest(PYTHON), 'mode': 0o555,
                'backend': str(backend), 'arguments': ['-B']}
    if (PYTHON.is_symlink() or not PYTHON.is_file() or PYTHON.read_bytes() != entrypoint_bytes()
            or stat.S_IMODE(PYTHON.lstat().st_mode) != 0o555 or receipt['entrypoint'] != expected):
        raise ValueError('SDK executable entrypoint policy differs from admission')
    original = receipt['backend']
    if (backend.is_symlink() or not backend.is_file() or not os.access(backend, os.X_OK)
            or original['kind'] != 'regular' or original['path'] != str(backend)
            or original['target'] != str(backend.resolve(strict=True))
            or digest(backend) != original['sha256'] or original['source_sha256'] != original['sha256']):
        raise ValueError('SDK copied backend differs from admitted executable bytes')
    protected(PYTHON)
    protected(backend)
    source = Path(original['source_target'])
    if not source.is_absolute() or '..' in source.parts:
        raise ValueError('SDK backend source provenance is not a canonical path')
    if not source.is_relative_to(SDK):
        roots = [Path(name).resolve(strict=True) for name in native_host_receipt['dependency_roots']]
        if (not any(source.is_relative_to(root) for root in roots)
                or source.resolve(strict=True) != source or not source.is_file()
                or digest(source) != original['source_sha256']):
            raise ValueError('SDK original native executable provenance differs from admission')
        protected(source)
    for path in PYTHON.parent.iterdir():
        if INTERPRETER_NAME.fullmatch(path.name) and path != PYTHON:
            if not path.is_symlink() or os.readlink(path) != 'python' or path.resolve(strict=True) != PYTHON:
                raise ValueError('SDK interpreter alias bypasses the admitted entrypoint')


def admit_sdk(metadata, native_host_receipt):
    """Construct the early SDK executable policy and capture its first identity."""
    artifact = ROOT / 'deployment/sdk-files.json'
    if artifact.exists() or artifact.is_symlink():
        raise ValueError('Refusing to reuse or reseal an existing SDK inventory')
    sdk_inventory(native_host_receipt)
    sites = list(SDK.glob('lib*/python*/site-packages'))
    if not sites:
        raise ValueError('SDK has no own site directory')
    for site in sites:
        if site.is_symlink() or not site.resolve(strict=True).is_relative_to(SDK.resolve()):
            raise ValueError('SDK site escapes its own prefix')
    check_startup({'sites': [str(site) for site in sites]})
    backend = construct_entrypoint(native_host_receipt)
    with tempfile.TemporaryDirectory(prefix='nvgym-sdk-identity-') as directory:
        identity = json.loads(subprocess.check_output(
            [str(PYTHON), '-I', '-c', IDENTITY], env=CLEAN_ENV,
            cwd=directory, text=True, timeout=120))
    check_identity(metadata, identity, native_host_receipt)
    check_startup(identity)
    records, admitted = sdk_inventory(native_host_receipt)
    with artifact.open('x') as stream:
        stream.write(json.dumps(records, sort_keys=True))
    artifact.chmod(0o644)
    receipt = {'schema': 'nvgym.sdk-admission.v2', 'prefix': str(SDK), 'python': str(PYTHON),
               'owner_sha256': digest(Path(__file__)), 'native_host_sha256': host_digest(native_host_receipt),
               'identity': identity,
               'entrypoint': {'path': str(PYTHON), 'sha256': digest(PYTHON), 'mode': 0o555,
                              'backend': str(PYTHON.with_name('_nvgym-python')), 'arguments': ['-B']},
               'backend': backend,
               'inventory_path': str(artifact), 'inventory_sha256': digest(artifact), 'inventory': admitted}
    verify_sdk(metadata, receipt, native_host_receipt)
    return receipt


def verify_sdk(metadata, receipt, native_host_receipt):
    """Authenticate immutable SDK bytes without running, repairing, or resealing it."""
    artifact = ROOT / 'deployment/sdk-files.json'
    expected = {'schema': 'nvgym.sdk-admission.v2', 'prefix': str(SDK), 'python': str(PYTHON),
                'owner_sha256': digest(Path(__file__)), 'native_host_sha256': host_digest(native_host_receipt),
                'inventory_path': str(artifact)}
    if any(receipt.get(name) != value for name, value in expected.items()):
        raise ValueError('SDK authority or native-host sibling binding differs from admission')
    records, observed = sdk_inventory(native_host_receipt)
    if artifact.is_symlink() or not artifact.is_file():
        raise ValueError('SDK inventory artifact must be an owned regular file')
    protected(artifact)
    if digest(artifact) != receipt['inventory_sha256']:
        raise ValueError('SDK inventory artifact hash differs from admission')
    if records != json.loads(artifact.read_text()) or observed != receipt['inventory']:
        raise ValueError('Exact SDK inventory differs from admission')
    identity = receipt['identity']
    check_entrypoint(receipt, native_host_receipt)
    check_identity(metadata, identity, native_host_receipt)
    check_startup(identity)
    return {'sdk': observed}


def validate_sdk_staging(metadata, sdk_receipt, native_host_receipt):
    """Run the actual registered SDK owner and its minimal-environment wrapper."""
    with tempfile.TemporaryDirectory(prefix='nvgym-sdk-staging-') as directory:
        work = Path(directory)
        config = work / 'config.json'
        config.write_text(json.dumps(metadata['config']))
        report = json.loads(subprocess.check_output(
            [metadata['case']['python'], '-B', '-I', '-c', STAGING, str(ROOT),
             metadata['environment'], str(config), str(work / 'client'), OBSERVATION],
            env=CLEAN_ENV, cwd=work, text=True, timeout=240).strip().splitlines()[-1])
    observed = report['supplement']
    check_identity(metadata, {name: observed[name] for name in (
        'executable', 'executable_target', 'prefix', 'base_prefix', 'implementation', 'version', 'abi', 'sites',
        'dont_write_bytecode')},
        native_host_receipt)
    if any(observed[name] != sdk_receipt['identity'][name] for name in sdk_receipt['identity']):
        raise ValueError('Staged SDK interpreter differs from its retained admission identity')
    if observed['dont_write_bytecode'] is not True:
        raise ValueError('Staged SDK wrapper does not disable bytecode writes')
    for name, expected in (('numpy', '1.26.4'), ('PIL', '11.3.0')):
        package = observed['packages'][name]
        if package['version'] != expected or not Path(package['origin']).is_relative_to(SDK.resolve(strict=True)):
            raise ValueError(f'Staged SDK must import {name} {expected} from its own prefix')
    for value in observed['path']:
        path = Path(value or directory).resolve()
        if any(path == Path(root).resolve() or path.is_relative_to(Path(root).resolve())
               for root in (*native_host_receipt['paired_roots'],
                            *native_host_receipt['original_identity']['sites'])):
            raise ValueError('Staged SDK exposes paired source or native site packages')
    return report
