"""Prepare operator-owned native software from the frozen official installers."""

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
ROBOTWIN_BASE = ('nvcr.io/nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04@sha256:'
                 'cc55d151af1e8e083f3210af753a5cfbcbc5455421531eb0459887026bb4699f')
PAIR = 'sha256:d8836d1569a505d4635ee367159c2621930e3f2b55335e73bc9a1b777a5f09a0'
OWNERS = {
    'nanoverse.install': '02ddb7518f96d83bfe27ec1c01306bd373ca89427a54c4cb2ea708c6a7c4da6d',
    'nanoverse.simulators.robodojo.install': 'f440db75418c31ba93b5aa5321b7c4a33466d84c3b496f4f1886245fe66f790f',
    'robodojo_env.install': '23ecaea98aa57f2ff70e1865481972adcfe1cfc22ac1d97349d06a3a1cc2ff59',
}
INSTALLERS = {
    'molmospaces': 'fbc171e15f80a77ce9ee8befc3d989c6a82581e6d5dd246c43eecbcea4cd8067',
    'behavior1k': 'f9baf8f40819bdb40bfebcc794bcd324ecbfc7c2523fd02b920e7303a6367cde',
    'roboforge': '63759f710fc8274215e0b24a330ba33a909abd6915c7846a8a7934031b904432',
    'libero': '04910c4b6f6c928dca8e02cbae9c7177cc47c15070e28169289c0012b66f8b56',
    'robotwin': 'cd0e4e35c5584adbde435e8408809ceb25113521318d697678f29282bbfcc10b',
    'robolab': '016229867c7864e17a2a2ed78a3d29ba424601c48ca55a95c06ddab110b8c435',
    'robocasa365': '24ab1c8fb67e5ee1ddd87f977e7d242c4d5af403947f6768bc50a373c77a31f4',
}
KIT_SCRIPTS = Path('venv/lib/python3.11/site-packages/isaacsim/kit/data/documents/'
                   'Kit/apps/Isaac-Sim/scripts')


def public_base(environment):
    return ROBOTWIN_BASE if environment == 'robotwin' else PUBLIC_BASE


def owners_for(environment):
    if environment == 'robodojo':
        return OWNERS
    if environment == 'roboforge':
        return {name: value for name, value in OWNERS.items() if name != 'robodojo_env.install'} | {
            'roboforge_env.install': INSTALLERS[environment]}
    if environment not in INSTALLERS:
        raise ValueError('Official installation is not implemented for ' + environment)
    return {'nanoverse.install': OWNERS['nanoverse.install'],
            environment + '_env.install': INSTALLERS[environment]}


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o644)


def prepare_inventory(root):
    """Discard finished caches and protect the completed image-owned installation."""
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
    kit_paths = [Path(venv) / 'lib/python3.11/site-packages' / namespace
                 / 'data/documents/Kit/apps/Isaac-Sim/scripts'
                 for venv in ('venv', 'native-venv')
                 for namespace in ('isaacsim/kit', 'omni')]
    for relative in [path for scripts in kit_paths if (root / scripts).parent.exists()
                     for path in (scripts, scripts / 'new_stage')]:
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
    reject_mounts([root])
    protected_modes = []
    for path in [root, *root.rglob('*')]:
        if path.is_symlink():
            continue
        status = path.lstat()
        if status.st_uid != 0:
            raise ValueError('Installed software entry is not image owned')
        if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
            raise ValueError('Installed software contains a special entry: '
                             + str(path.relative_to(root)) + ' (mode=' + oct(status.st_mode) + ')')
        before = stat.S_IMODE(status.st_mode)
        after = before & ~0o022
        if before != after:
            path.chmod(after)
            protected_modes.append({'path': str(path.relative_to(root)), 'before': before, 'after': after})
    return {'removed_installer_caches': removed, 'kit_directory_modes': modes,
            'protected_installation_modes': protected_modes}


def verify(descriptor, expected_sha256):
    descriptor = Path(descriptor)
    if digest(descriptor) != expected_sha256:
        raise ValueError('Installed software descriptor SHA256 differs')
    record = json.loads(descriptor.read_text())
    environment = record.get('environment')
    if environment not in ('robodojo', *INSTALLERS):
        raise ValueError('Installed software provenance differs')
    python = ROOT / ('native-venv/bin/python' if environment == 'robolab' else 'venv/bin/python')
    if (record.get('schema') != SCHEMA
            or record.get('root') != str(ROOT) or record.get('paired_revision') != PAIR
            or record.get('public_base') != public_base(environment) or record.get('owners') != owners_for(environment)
            or record.get('python') != str(python)
            or record.get('wheel', {}).get('revision') != PAIR
            or record.get('preparation', {}).get('native_closure', {}).get('self_contained') is not True
            or record.get('preparation', {}).get('native_closure', {}).get('dependency_roots') != [str(ROOT)]
            or record.get('preparation_owner_sha256') != digest(__file__)):
        raise ValueError('Installed software provenance differs')
    if environment in ('robodojo', 'roboforge') and (
            record.get('recipe', {}).get('source_commit') != SOURCE_REVISION
            or record.get('recipe', {}).get('assets_commit') != REVISION):
        raise ValueError('Official RoboDojo source pins differ')
    for path in (descriptor, descriptor.parent, ROOT):
        status = path.lstat()
        if path.is_symlink() or status.st_uid != 0 or status.st_mode & 0o022:
            raise ValueError('Installed software authority is not image owned')
    from portable.native_host import reject_mounts
    reject_mounts([descriptor.parent, ROOT])
    if environment == 'behavior1k':
        from portable.behavior_software import validate_layout
        validate_layout(ROOT)
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
            'public_base': public_base(environment), 'origin': 'operator official-source installation'}


def native_closure(root, python=None):
    """Reject installation-time dependencies that would disappear in a new container."""
    from portable.native_host import CLEAN_ENV, PROBE, check_dependencies, dependency_roots

    native = {'kind': 'installed_software', 'payload_path': str(root),
              'prefix': str(root), 'python': str(python or root / 'venv/bin/python')}
    identity = json.loads(subprocess.check_output(
        [native['python'], '-B', '-I', '-c', PROBE], env=CLEAN_ENV,
        cwd='/', text=True, timeout=120))
    roots = dependency_roots(native, identity)
    if roots != [root.resolve(strict=True)]:
        raise ValueError('Native Python base escapes the installed software payload')
    check_dependencies(native, identity, roots)
    return {'self_contained': True, 'base_prefix': identity['base_prefix'],
            'dependency_roots': [str(path) for path in roots]}


def install(wheel, *, environment='robodojo', options=None, resume=False,
            accept_isaac_terms=False, assets_cache=None):
    if environment in ('robodojo', 'roboforge', 'robolab', 'behavior1k') and not accept_isaac_terms:
        raise ValueError('Official installation requires --accept-isaac-terms')
    if assets_cache is not None and environment not in ('robodojo', 'molmospaces'):
        raise ValueError('The assets cache supports RoboDojo and MolmoSpaces only')
    if os.getuid() != 0:
        raise ValueError('Image preparation requires root')
    owners = owners_for(environment)
    for name, expected in owners.items():
        if digest(importlib.import_module(name).__file__) != expected:
            raise ValueError('Frozen installation owner differs: ' + name)
    from nanoverse.catalog import load_catalog
    from nanoverse.install import Installation, wheel_identity

    identity = wheel_identity(wheel)
    if identity['revision'] != PAIR:
        raise ValueError('Installer wheel differs from the accepted paired revision')
    recipe = importlib.import_module(load_catalog()[environment].installer)
    if environment == 'molmospaces':
        from portable.molmo_public import public_recipe
        recipe = public_recipe(recipe, assets_cache)
    if environment == 'behavior1k':
        from portable.behavior_software import public_recipe
        recipe = public_recipe(recipe)
    if environment == 'robocasa365':
        from portable.robocasa_assets import public_recipe
        recipe = public_recipe(recipe)
    if environment == 'robolab':
        from portable.robolab_cache import public_recipe
        recipe = public_recipe(recipe)
    if environment == 'roboforge':
        from portable.roboforge_prepare import public_recipe
        recipe = public_recipe(recipe)
    if environment in ('robodojo', 'roboforge') and (recipe.SPEC['source_commit'] != SOURCE_REVISION
                                     or recipe.SPEC['assets_commit'] != REVISION):
        raise ValueError('Registered official source pins differ')
    transport = None
    if assets_cache is not None and environment == 'robodojo':
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
                     'wheel': identity, 'owners': owners, 'environment': environment,
                     'options': options or {}, 'paired_revision': PAIR,
                     'public_base': public_base(environment)}
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
        process_env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LD_LIBRARY_PATH',
                       'CUDA_HOME', 'CUDA_PATH', 'NVIDIA_VISIBLE_DEVICES',
                       'NVIDIA_DRIVER_CAPABILITIES', 'SSL_CERT_FILE', 'HTTPS_PROXY',
                       'HTTP_PROXY', 'NO_PROXY') if name in os.environ}
        process_env.update(HOME=str(ROOT), PYTHONNOUSERSITE='1',
                           PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0',
                           OMNI_KIT_ACCEPT_EULA='YES', OMNI_KIT_ALLOW_ROOT='1',
                           ACCEPT_EULA='Y', PRIVACY_CONSENT='Y',
                           UV_PYTHON_PREFERENCE='only-managed', TORCH_CUDA_ARCH_LIST='8.9',
                           HF_HUB_DISABLE_XET='1')
        os.environ.clear()
        os.environ.update(process_env)
        # Installation.owned creates ROOT before invoking tools. Kit's downloaded
        # extensions must stay in that authority rather than a temporary HOME.
        installation = Installation(ROOT, recipe, options or {})
        try:
            installation.install(wheel.resolve(), resume=resume)
        except subprocess.CalledProcessError:
            state = json.loads(installation.state_path.read_text())
            log = (Path(state['last_attempt']) / 'commands.log').read_text(errors='replace')
            if (environment not in ('robodojo', 'roboforge') or state.get('phase') != 'assets'
                    or state.get('completed') != ['python', 'source', 'dependencies']
                    or '429' not in log or 'huggingface.co/' not in log):
                raise
            recover(ROOT, ROOT.parent / ('asset-download-' + str(time.time_ns())),
                    environment=environment)
            installation.install(wheel.resolve(), resume=True)
        with installation.owned():
            installation.check()
            recipe.verify(installation)
            installation.state.update(status='verified', verification=str(installation.attempt))
        preparation = prepare_inventory(ROOT)
        python = (recipe.runtime_python(installation) if environment == 'robolab'
                  else installation.python)
        preparation['native_closure'] = native_closure(ROOT, python)
        if transport is not None:
            preparation['asset_transport'] = transport
        template = ROOT / 'RoboDojo/Assets/Robots/x5/curobo_tmp.yml'
        if environment in ('robodojo', 'roboforge') and (template.stat().st_size != 12526 or digest(template)
                != '5eea08bf7ef276bf9dfd078ef9f69221e9a67c8cf6cd19fce5ef66d42e96b3f2'):
            raise ValueError('Official robot template differs from the accepted case')
        # The native owner has finished writing its state. Hold its lock while
        # sealing, without creating another owner attempt inside the inventory.
        with (ROOT / '.setup.lock').open('a') as native_lock:
            fcntl.flock(native_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            staging = Path(tempfile.mkdtemp(prefix='seal-', dir=OUTPUT))
            files, observed = inventory(ROOT, directory_modes=True)
            write_json(staging / 'installed-files.json', files)
            record = {'schema': SCHEMA, 'environment': environment, 'root': str(ROOT),
                      'root_mode': ROOT.stat().st_mode & 0o777, 'paired_revision': PAIR,
                      'public_base': public_base(environment), 'wheel': identity, 'owners': owners,
                      'preparation_owner_sha256': digest(__file__),
                      'preparation': preparation,
                      'python': str(python), 'recipe': recipe.SPEC, 'options': options or {},
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
