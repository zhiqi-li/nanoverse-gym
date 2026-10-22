"""Prepare operator-owned RoboDojo software from the frozen official installer."""

import argparse
import fcntl
import importlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from portable.native_inventory import digest, inventory
from portable.robodojo_assets import REVISION, SOURCE_REVISION, recover

SCHEMA = 'nvgym.installed-software.v2'
ROOT = Path('/opt/envsdk/native/payload')
OUTPUT = ROOT.parent / 'preparation'
PUBLIC_BASE = ('nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:'
               '61f6c08f2b59036cb935e56d1e31a6b64e3ae2c7ddb86d33fa0b044c7917b719')
PAIR = 'sha256:d8836d1569a505d4635ee367159c2621930e3f2b55335e73bc9a1b777a5f09a0'
OWNERS = {
    'nanoverse.install': '02ddb7518f96d83bfe27ec1c01306bd373ca89427a54c4cb2ea708c6a7c4da6d',
    'nanoverse.simulators.robodojo.install': 'f440db75418c31ba93b5aa5321b7c4a33466d84c3b496f4f1886245fe66f790f',
    'robodojo_env.install': '23ecaea98aa57f2ff70e1865481972adcfe1cfc22ac1d97349d06a3a1cc2ff59',
}
KIT_SCRIPTS = Path('venv/lib/python3.11/site-packages/isaacsim/kit/data/documents/'
                   'Kit/apps/Isaac-Sim/scripts')


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o644)


def prepare_inventory(root):
    """Discard finished installer caches and protect known Kit appdata directories."""
    from portable.native_host import reject_mounts

    removed, modes = [], []
    for relative in ('cache/uv', 'cache/pip', 'cache/huggingface', 'python/.lock'):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        status = path.lstat()
        if (path.is_symlink() or status.st_uid != 0
                or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True))):
            raise ValueError('Installer cache escaped its owned prefix')
        reject_mounts([path])
        if relative == 'python/.lock':
            if not stat.S_ISREG(status.st_mode):
                raise ValueError('Managed Python lock is not an ordinary file')
            path.unlink()
        else:
            shutil.rmtree(path)
        removed.append(relative)
    for relative in (KIT_SCRIPTS, KIT_SCRIPTS / 'new_stage'):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve(strict=True)):
            raise ValueError('Kit appdata directory escaped its owned prefix')
        path.mkdir(parents=True, exist_ok=True)
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0:
            raise ValueError('Kit appdata directory is not image owned')
        reject_mounts([path])
        before = stat.S_IMODE(status.st_mode)
        after = before & ~0o022
        if before != after:
            path.chmod(after)
        modes.append({'path': str(relative), 'before': before, 'after': after})
    return {'removed_installer_caches': removed, 'kit_directory_modes': modes}


def verify(descriptor, expected_sha256):
    descriptor = Path(descriptor)
    if digest(descriptor) != expected_sha256:
        raise ValueError('Installed software descriptor SHA256 differs')
    record = json.loads(descriptor.read_text())
    if (record.get('schema') != SCHEMA or record.get('environment') != 'robodojo'
            or record.get('root') != str(ROOT) or record.get('paired_revision') != PAIR
            or record.get('public_base') != PUBLIC_BASE or record.get('owners') != OWNERS
            or record.get('python') != str(ROOT / 'venv/bin/python')
            or record.get('wheel', {}).get('revision') != PAIR
            or record.get('recipe', {}).get('source_commit') != SOURCE_REVISION
            or record.get('recipe', {}).get('assets_commit') != REVISION
            or record.get('preparation', {}).get('native_closure', {}).get('self_contained') is not True
            or record.get('preparation', {}).get('native_closure', {}).get('dependency_roots') != [str(ROOT)]
            or record.get('preparation_owner_sha256') != digest(__file__)):
        raise ValueError('Installed software provenance differs')
    for path in (descriptor, descriptor.parent, ROOT):
        status = path.lstat()
        if path.is_symlink() or status.st_uid != 0 or status.st_mode & 0o022:
            raise ValueError('Installed software authority is not image owned')
    from portable.native_host import reject_mounts
    reject_mounts([descriptor.parent, ROOT])
    files, observed = inventory(ROOT, directory_modes=True)
    if observed != record['inventory'] or ROOT.stat().st_mode & 0o777 != record['root_mode']:
        raise ValueError('Installed software inventory differs')
    manifest = descriptor.parent / 'installed-files.json'
    if (manifest.is_symlink() or manifest.stat().st_uid != 0 or manifest.stat().st_mode & 0o022
            or digest(manifest) != record['files_sha256']
            or json.loads(manifest.read_text()) != files):
        raise ValueError('Installed software file records differ')
    return {'mode': 'installed_software', 'descriptor_sha256': expected_sha256,
            'payload': observed, 'original_prefix': str(ROOT),
            'public_base': PUBLIC_BASE, 'origin': 'operator official-source installation'}


def native_closure(root):
    """Reject installation-time dependencies that would disappear in a new container."""
    from portable.native_host import CLEAN_ENV, PROBE, check_dependencies, dependency_roots

    native = {'kind': 'installed_software', 'payload_path': str(root),
              'prefix': str(root), 'python': str(root / 'venv/bin/python')}
    identity = json.loads(subprocess.check_output(
        [native['python'], '-B', '-I', '-c', PROBE], env=CLEAN_ENV,
        cwd='/', text=True, timeout=120))
    roots = dependency_roots(native, identity)
    if roots != [root.resolve(strict=True)]:
        raise ValueError('Native Python base escapes the installed software payload')
    check_dependencies(native, identity, roots)
    return {'self_contained': True, 'base_prefix': identity['base_prefix'],
            'dependency_roots': [str(path) for path in roots]}


def install(wheel, *, resume=False, accept_isaac_terms=False, assets_cache=None):
    if not accept_isaac_terms:
        raise ValueError('Official installation requires --accept-isaac-terms')
    if os.getuid() != 0:
        raise ValueError('Image preparation requires root')
    for name, expected in OWNERS.items():
        if digest(importlib.import_module(name).__file__) != expected:
            raise ValueError('Frozen installation owner differs: ' + name)
    from nanoverse.catalog import load_catalog
    from nanoverse.install import Installation, wheel_identity

    identity = wheel_identity(wheel)
    if identity['revision'] != PAIR:
        raise ValueError('Installer wheel differs from the accepted paired revision')
    recipe = importlib.import_module(load_catalog()['robodojo'].installer)
    if recipe.SPEC['source_commit'] != SOURCE_REVISION or recipe.SPEC['assets_commit'] != REVISION:
        raise ValueError('Registered official source pins differ')
    transport = None
    if assets_cache is not None:
        from portable.robodojo_assets import cached_recipe
        recipe, transport = cached_recipe(recipe, assets_cache,
                                           OUTPUT / ('cached-assets-' + str(time.time_ns())))
    if OUTPUT.is_symlink() or ROOT.is_symlink():
        raise ValueError('Native preparation roots must be ordinary directories')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / '.prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sealed = OUTPUT / 'sealed'
        if sealed.exists() or sealed.is_symlink():
            raise ValueError('Refusing to replace a sealed installed-software descriptor')
        operation = {'schema': 'nvgym.official-install-attempt.v1', 'root': str(ROOT),
                     'wheel': identity, 'owners': OWNERS, 'paired_revision': PAIR,
                     'public_base': PUBLIC_BASE}
        attempt_identity = OUTPUT / 'official-install.json'
        if attempt_identity.exists():
            if not resume or json.loads(attempt_identity.read_text()) != operation:
                raise ValueError('Resuming preparation requires the same recorded inputs')
        elif ROOT.exists():
            if not resume:
                raise ValueError('Existing installation requires --resume')
            # The native owner validates its wheel, recipe, options and root before reuse.
            write_json(attempt_identity, operation)
        else:
            write_json(attempt_identity, operation)
        environment = {name: os.environ[name] for name in ('PATH', 'LANG', 'LD_LIBRARY_PATH',
                       'CUDA_HOME', 'CUDA_PATH', 'NVIDIA_VISIBLE_DEVICES',
                       'NVIDIA_DRIVER_CAPABILITIES', 'SSL_CERT_FILE', 'HTTPS_PROXY',
                       'HTTP_PROXY', 'NO_PROXY') if name in os.environ}
        environment.update(HOME=str(ROOT), PYTHONNOUSERSITE='1',
                           PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0',
                           OMNI_KIT_ACCEPT_EULA='YES', OMNI_KIT_ALLOW_ROOT='1',
                           ACCEPT_EULA='Y', PRIVACY_CONSENT='Y',
                           UV_PYTHON_PREFERENCE='only-managed', TORCH_CUDA_ARCH_LIST='8.9',
                           HF_HUB_DISABLE_XET='1')
        os.environ.clear()
        os.environ.update(environment)
        # Installation.owned creates ROOT before invoking tools. Kit's downloaded
        # extensions must stay in that authority rather than a temporary HOME.
        installation = Installation(ROOT, recipe, {})
        try:
            installation.install(wheel.resolve(), resume=resume)
        except subprocess.CalledProcessError:
            state = json.loads(installation.state_path.read_text())
            log = (Path(state['last_attempt']) / 'commands.log').read_text(errors='replace')
            if (state.get('phase') != 'assets'
                    or state.get('completed') != ['python', 'source', 'dependencies']
                    or '429' not in log or 'huggingface.co/' not in log):
                raise
            recover(ROOT, ROOT.parent / ('asset-download-' + str(time.time_ns())))
            installation.install(wheel.resolve(), resume=True)
        preparation = prepare_inventory(ROOT)
        with installation.owned():
            installation.check()
            recipe.verify(installation)
            installation.state.update(status='verified', verification=str(installation.attempt))
        preparation['native_closure'] = native_closure(ROOT)
        if transport is not None:
            preparation['asset_transport'] = transport
        template = ROOT / 'RoboDojo/Assets/Robots/x5/curobo_tmp.yml'
        if (template.stat().st_size != 12526 or digest(template)
                != '5eea08bf7ef276bf9dfd078ef9f69221e9a67c8cf6cd19fce5ef66d42e96b3f2'):
            raise ValueError('Official robot template differs from the accepted case')
        # The native owner has finished writing its state. Hold its lock while
        # sealing, without creating another owner attempt inside the inventory.
        with (ROOT / '.setup.lock').open('a') as native_lock:
            fcntl.flock(native_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            staging = Path(tempfile.mkdtemp(prefix='seal-', dir=OUTPUT))
            files, observed = inventory(ROOT, directory_modes=True)
            write_json(staging / 'installed-files.json', files)
            record = {'schema': SCHEMA, 'environment': 'robodojo', 'root': str(ROOT),
                      'root_mode': ROOT.stat().st_mode & 0o777, 'paired_revision': PAIR,
                      'public_base': PUBLIC_BASE, 'wheel': identity, 'owners': OWNERS,
                      'preparation_owner_sha256': digest(__file__),
                      'preparation': preparation,
                      'python': str(installation.python), 'recipe': recipe.SPEC,
                      'native_verification': installation.state['verification'],
                      'inventory': observed, 'files_sha256': digest(staging / 'installed-files.json'),
                      'scope': 'native software; Gym, TTC and host Docker lifecycle require separate acceptance'}
            descriptor = staging / 'installed-software.json'
            write_json(descriptor, record)
            result = verify(descriptor, digest(descriptor))
            staging.rename(sealed)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--accept-isaac-terms', action='store_true')
    parser.add_argument('--assets-cache', type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.wheel, resume=args.resume,
                             accept_isaac_terms=args.accept_isaac_terms,
                             assets_cache=args.assets_cache), indent=2))


if __name__ == '__main__':
    main()
