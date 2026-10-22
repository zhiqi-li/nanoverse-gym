"""Construct and authenticate the image-owned native actor interpreter."""

import copy
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from portable.native_inventory import inventory
from portable.release import digest

ROOT = Path('/opt/nvgym')
HOST = ROOT / 'native-host'
PYTHON = HOST / 'bin/python'
PAIRED_ROOTS = (ROOT / 'code/nanoverse', ROOT / 'code/nanoverse/envs', ROOT / 'code/ember_bridge/src')
BASH = Path('/bin/bash')
GIT = Path('/usr/bin/git')
ROOT_MODE_POLICY = 'owner-write-may-tighten-v1'
INTERPRETER_NAME = re.compile(r'python(?:[0-9]+(?:\.[0-9]+)*)?\Z')
POLICY_ENV = {'PYTHONDONTWRITEBYTECODE': '1', 'GIT_OPTIONAL_LOCKS': '0'}
CLEAN_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONNOUSERSITE': '1'}

# This runs under the original admitted interpreter, including its native .pth hooks.
PROBE = '''import sys
sys.dont_write_bytecode = True
import json, os, site, sysconfig
from pathlib import Path
hooks = {}
for module in tuple(sys.modules.values()):
    name = getattr(module, '__name__', '')
    if name.startswith('__editable__'):
        hooks[name] = {key: getattr(module, key) for key in ('MAPPING', 'NAMESPACES', 'PATH_PLACEHOLDER')
                       if hasattr(module, key)}
print(json.dumps(dict(executable=sys.executable, executable_target=str(Path(sys.executable).resolve(strict=True)),
    prefix=sys.prefix, base_prefix=sys.base_prefix, base_executable=sys._base_executable,
    version=list(sys.version_info[:3]), implementation=sys.implementation.name,
    abi=sysconfig.get_config_var('SOABI'),
    sites=list(dict.fromkeys([sysconfig.get_path('purelib'), sysconfig.get_path('platlib'),
                              *site.getsitepackages()])), path=sys.path,
    startup_modules={name: str(Path(module.__file__).resolve(strict=True))
                     for name, module in tuple(sys.modules.items())
                     if getattr(module, '__file__', None) and os.path.exists(module.__file__)},
    editable_hooks=hooks)))'''


def protected(path):
    status = path.lstat()
    if status.st_uid != 0 or (not stat.S_ISLNK(status.st_mode) and status.st_mode & 0o022):
        raise ValueError(f'Unprotected native host dependency: {path}')


def reject_mounts(paths):
    # The original-prefix alias may live below OSMO's workspace mount. Its
    # canonical bytes must still be image baked, as must every protected root.
    roots = [Path(path).resolve(strict=True) for path in paths]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        value = line.split(' - ', 1)[0].split()[4]
        for escaped, actual in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
            value = value.replace(escaped, actual)
        mount = Path(value)
        if mount == Path('/'):
            continue
        if any(mount == root or mount in root.parents or root in mount.parents for root in roots):
            raise ValueError(f'Native host/code dependency is supplied by a mount: {mount}')


def paired_inventory(provenance):
    """Derive exact packaged code membership from the existing release inventory."""
    source = ROOT / 'deployment/release-files.json'
    for root in (ROOT, ROOT / 'deployment'):
        protected(root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Native host authority must be an image-owned directory')
    reject_mounts([ROOT / 'deployment'])
    protected(source)
    if digest(source) != provenance['release_files_sha256']:
        raise ValueError('Release inventory hash mismatch')
    expected = {name: record for name, record in json.loads(source.read_text()).items()
                if name.startswith('code/')}
    code = ROOT / 'code'
    protected(code)
    reject_mounts([code])
    actual = {}
    for path in sorted(code.rglob('*')):
        protected(path)
        name = str(path.relative_to(ROOT))
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(code.resolve()):
                raise ValueError(f'Paired source link escapes packaged code: {name}')
            actual[name] = {'link': os.readlink(path)}
        elif path.is_file():
            actual[name] = {'sha256': digest(path)}
        elif not path.is_dir():
            raise ValueError(f'Special paired source entry: {name}')
    if expected != actual:
        raise ValueError('Exact paired source inventory differs from the release')
    return {'release_files_sha256': provenance['release_files_sha256'],
            'native_pair': provenance['native_pair']}


def check_bindings(metadata):
    if metadata['case']['python'] != str(PYTHON):
        raise ValueError('Case interpreter differs from the admitted native host')
    config = metadata['config']
    environment = metadata['environment']
    if environment in ('robodojo', 'roboforge'):
        configured = config['python']
    elif environment == 'exemplar':
        configured = str(PYTHON)
    else:
        configured = config['native']['python']
    if environment == 'robodojo':
        command = config.get('simulator_command')
        if not isinstance(command, list) or not command or command[0] != str(PYTHON):
            raise ValueError('RoboDojo simulator command bypasses the admitted native host')
    if configured != str(PYTHON) or config['sdk']['python'] != str(ROOT / 'sdk/bin/python'):
        raise ValueError('Native/SDK interpreter bindings differ from the admitted contract')


def dependency_roots(native, identity):
    if identity['implementation'] != 'cpython' or not identity['abi']:
        raise ValueError('Native host requires an admitted CPython ABI')
    base = Path(identity['base_prefix']).resolve(strict=True)
    if base in (Path('/'), Path('/usr'), Path('/opt')) or base.is_relative_to(ROOT):
        raise ValueError('Native base prefix is not a bounded dependency installation')
    roots = [] if native['kind'] == 'fixture' else [Path(native['payload_path']).resolve(strict=True)]
    if not any(base.is_relative_to(root) for root in roots):
        roots.append(base)
    for root in roots:
        protected(root)
        if not root.is_dir() or root.is_symlink():
            raise ValueError('Native dependency root must be an owned directory')
    return roots


def check_dependencies(native, identity, roots):
    paths = [native['python'], identity['executable_target'], identity['base_executable'],
             identity['prefix'], identity['base_prefix'], *identity['sites'],
             *identity['startup_modules'].values()]
    placeholders = {hook.get('PATH_PLACEHOLDER') for hook in identity['editable_hooks'].values()}
    for value in identity['path']:
        if value in placeholders:
            continue
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f'Unadmitted native startup path: {value!r}')
        # CPython includes this optional, often absent, standard-library archive.
        if not path.exists() and path.suffix == '.zip' and path.name.startswith('python'):
            paths.append(str(path.parent))
        else:
            paths.append(value)
    for hook in identity['editable_hooks'].values():
        paths.extend(hook.get('MAPPING', {}).values())
        for values in hook.get('NAMESPACES', {}).values():
            paths.extend(value for value in values if value not in placeholders)
    for value in paths:
        path = Path(value)
        target = path.resolve(strict=True)
        if not any(target == root or target.is_relative_to(root) for root in roots):
            raise ValueError(f'Native dependency escapes admitted authority: {value}')
        protected(path)
        protected(target)
    for site in identity['sites']:
        directory = Path(site)
        if not directory.is_dir():
            raise ValueError(f'Native site is absent: {site}')
        for path in directory.rglob('*'):
            protected(path)
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not any(target == root or target.is_relative_to(root) for root in roots):
                    raise ValueError(f'Native site link escapes admitted authority: {path}')
                protected(target)
    if native['kind'] != 'fixture':
        protected(Path(native['prefix']))
        paths.append(native['prefix'])
    if native['kind'] == 'expanded_seed':
        paths.append(native['descriptor_path'])
    reject_mounts([*roots, *paths])


def bootstrap_bytes(identity):
    sites = json.dumps(identity['sites'])
    roots = json.dumps([str(path) for path in PAIRED_ROOTS])
    return ('import sys\nsys.dont_write_bytecode = True\nimport site\n'
            f'for path in {sites}:\n    site.addsitedir(path)\n'
            f'roots = {roots}\nsys.path[:] = roots + [path for path in sys.path if path not in roots]\n').encode()


def host_inventory(roots):
    protected(HOST)
    if HOST.is_symlink() or not HOST.is_dir():
        raise ValueError('Native host must be an image-owned directory')
    reject_mounts([HOST])
    records, identity = inventory(HOST, directory_modes=True)
    identity['root_mode'] = stat.S_IMODE(HOST.lstat().st_mode)
    for path in HOST.rglob('*'):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not any(target == root or target.is_relative_to(root) for root in (HOST, *roots)):
                raise ValueError(f'Native host link escapes admitted dependencies: {path}')
            protected(target)
    return records, identity


def helper_identity(path=None):
    bash = path is None
    path = BASH if bash else path
    if bash and path != Path('/bin/bash'):
        raise ValueError('Native host requires the fixed Bash helper')
    if not bash and path != Path('/usr/bin/git'):
        raise ValueError('Native host requires the fixed Git helper')
    pending = list(path.parts[1:])
    current = Path('/')
    records = {}
    links = 0
    while True:
        protected(current)
        status = current.lstat()
        record = {'mode': stat.S_IMODE(status.st_mode), 'uid': status.st_uid}
        if current == Path('/') and (not stat.S_ISDIR(status.st_mode) or status.st_uid != 0
                                     or record['mode'] not in (0o555, 0o755)):
            raise ValueError('Native host Bash helper root must be a UID-zero directory with mode 0555 or 0755')
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(current)
            record['link'] = target
            records[str(current)] = record
            links += 1
            if links > 40:
                raise ValueError('Native host Bash helper contains a link cycle')
            expanded = Path(os.path.normpath(str(current.parent / target)))
            pending = [*expanded.parts[1:], *pending]
            current = Path('/')
            continue
        if pending:
            if not stat.S_ISDIR(status.st_mode):
                raise ValueError('Native host Bash helper ancestor is not a directory')
            records[str(current)] = record
            current = current / pending.pop(0)
            continue
        if not stat.S_ISREG(status.st_mode) or not os.access(current, os.X_OK):
            raise ValueError('Native host Bash helper is not a regular executable')
        record['sha256'] = digest(current)
        records[str(current)] = record
        break
    reject_mounts([path])
    return {'path': str(path), 'target': str(current), 'root_mode_policy': ROOT_MODE_POLICY,
            'chain': records}


def check_entry_policy(admitted, observed):
    comparable = copy.deepcopy(observed)
    helpers = ['helper']
    if admitted.get('git_helper') is not None or observed.get('git_helper') is not None:
        helpers.append('git_helper')
    for name in helpers:
        modes = []
        for policy in (admitted, observed):
            try:
                helper = policy[name]
                root = helper['chain']['/']
                if (helper['root_mode_policy'] != ROOT_MODE_POLICY or not isinstance(root, dict)
                        or set(root) != {'mode', 'uid'} or type(root['uid']) is not int or root['uid'] != 0
                        or type(root['mode']) is not int or root['mode'] not in (0o555, 0o755)):
                    raise ValueError('Native host Bash helper root policy differs from admission')
            except (KeyError, TypeError) as error:
                raise ValueError('Native host Bash helper root policy is incomplete') from error
            modes.append(root['mode'])
        if tuple(modes) not in ((0o755, 0o755), (0o755, 0o555), (0o555, 0o555)):
            raise ValueError('Native host Bash helper root permissions became less restrictive')
        comparable[name]['chain']['/']['mode'] = modes[0]
    if admitted != comparable:
        raise ValueError('Native host executable policy differs from admission')


def git_entrypoint_bytes():
    return ('#!/bin/bash -p\n'
            f'exec {shlex.quote(str(GIT))} --no-optional-locks "$@"\n').encode()


def entrypoint_bytes(*, protect_git=False):
    return ('#!/bin/bash -p\n'
            'export PYTHONDONTWRITEBYTECODE=1\n'
            'export GIT_OPTIONAL_LOCKS=0\n'
            + (f'export PATH={shlex.quote(str(PYTHON.parent))}:"${{PATH:-/usr/local/bin:/usr/bin:/bin}}"\n'
               if protect_git else '') +
            f'exec -a {shlex.quote(str(PYTHON))} '
            f'{shlex.quote(str(PYTHON.with_name("_nvgym-python")))} -B "$@"\n').encode()


def construct_entrypoint(identity, roots, *, protect_git=False):
    backend = PYTHON.with_name('_nvgym-python')
    if backend.exists() or backend.is_symlink():
        raise ValueError('Refusing an existing native host backend')
    source = PYTHON.resolve(strict=True)
    original_sha256 = digest(Path(identity['executable_target']))
    allowed = [HOST.resolve(strict=True), *roots]
    aliases = sorted(path for path in PYTHON.parent.iterdir() if INTERPRETER_NAME.fullmatch(path.name))
    if PYTHON not in aliases:
        raise ValueError('Native host has no public interpreter entry')
    for path in aliases:
        target = path.resolve(strict=True)
        protected(path)
        protected(target)
        if (not stat.S_ISREG(target.lstat().st_mode) or not os.access(target, os.X_OK)
                or not any(target.is_relative_to(root) for root in allowed)
                or digest(target) != original_sha256):
            raise ValueError('Native host interpreter alias names a foreign executable')
    shutil.copy2(source, backend)
    protected(backend)
    if protect_git:
        helper_identity(GIT)
        git = PYTHON.with_name('git')
        with git.open('xb') as stream:
            stream.write(git_entrypoint_bytes())
        git.chmod(0o555)
    for path in aliases:
        path.unlink()
    with PYTHON.open('xb') as stream:
        stream.write(entrypoint_bytes(protect_git=protect_git))
    PYTHON.chmod(0o555)
    for path in aliases:
        if path != PYTHON:
            path.symlink_to('python')
    return [path.name for path in aliases]


ENTRY_OBSERVATION = '''import json,os,shutil,sys,sysconfig
from pathlib import Path
print(json.dumps(dict(executable=sys.executable,process_executable=os.readlink('/proc/self/exe'),
    prefix=sys.prefix,base_prefix=str(Path(sys.base_prefix).resolve(strict=True)),
    base_executable=str(Path(sys._base_executable).resolve(strict=True)),
    implementation=sys.implementation.name,version=list(sys.version_info[:3]),
    abi=sysconfig.get_config_var('SOABI'),dont_write_bytecode=sys.dont_write_bytecode,
    environment={name:os.environ.get(name) for name in ('PYTHONDONTWRITEBYTECODE','GIT_OPTIONAL_LOCKS')},
    git_executable=shutil.which('git') if sys.argv[1:]==['git'] else None)))'''


def check_entry_observation(observation, identity, *, protect_git=False):
    expected = {'executable': str(PYTHON), 'process_executable': str(PYTHON.with_name('_nvgym-python')),
                'prefix': str(HOST), 'base_prefix': str(Path(identity['base_prefix']).resolve(strict=True)),
                'base_executable': str(Path(identity['base_executable']).resolve(strict=True)),
                **{key: identity[key] for key in ('implementation', 'version', 'abi')},
                'dont_write_bytecode': True, 'environment': POLICY_ENV,
                'git_executable': str(PYTHON.with_name('git')) if protect_git else None}
    if observation != expected:
        raise ValueError('Native host entry changed executable identity, startup policy, or native ABI')


def entry_policy(aliases, observations, *, protect_git=False):
    if type(protect_git) is not bool:
        raise ValueError('Native Git policy must be an explicit boolean')
    backend = PYTHON.with_name('_nvgym-python')
    for path in (PYTHON, backend):
        protected(path)
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError('Native host entry and backend must be protected regular executables')
    if (PYTHON.read_bytes() != entrypoint_bytes(protect_git=protect_git)
            or stat.S_IMODE(PYTHON.lstat().st_mode) != 0o555):
        raise ValueError('Native host entry policy differs')
    actual = sorted(path.name for path in PYTHON.parent.iterdir() if INTERPRETER_NAME.fullmatch(path.name))
    if actual != aliases:
        raise ValueError('Native host interpreter alias membership differs')
    for name in aliases:
        path = PYTHON.parent / name
        if path != PYTHON and (not path.is_symlink() or os.readlink(path) != 'python'
                               or path.resolve(strict=True) != PYTHON):
            raise ValueError('Native host interpreter alias bypasses the public entry')
    git = PYTHON.with_name('git')
    if protect_git:
        protected(git)
        if (git.is_symlink() or not git.is_file() or git.read_bytes() != git_entrypoint_bytes()
                or stat.S_IMODE(git.lstat().st_mode) != 0o555):
            raise ValueError('Native Git entry policy differs')
    elif git.exists() or git.is_symlink():
        raise ValueError('Unexpected native Git entry')
    return {'entry': str(PYTHON), 'entry_sha256': digest(PYTHON), 'entry_mode': 0o555,
            'backend': str(backend), 'backend_sha256': digest(backend),
            'backend_mode': stat.S_IMODE(backend.lstat().st_mode), 'aliases': aliases,
            'arguments': ['-B'], 'argv0': str(PYTHON), 'environment': POLICY_ENV,
            'helper': helper_identity(), 'observations': observations,
            'git_read_only': protect_git, 'git_helper': helper_identity(GIT) if protect_git else None}


def create_native_host(native, authority, provenance, *, protect_git=False):
    """Build once, from verified native authority, without modifying that authority."""
    source = paired_inventory(provenance)
    if HOST.exists() or HOST.is_symlink():
        raise ValueError('Refusing to reuse an existing native host')
    with tempfile.TemporaryDirectory(prefix='nvgym-native-probe-') as directory:
        identity = json.loads(subprocess.check_output(
            [native['python'], '-B', '-I', '-c', PROBE], env=CLEAN_ENV,
            cwd=directory, text=True, timeout=120))
    if identity['executable'] != native['python']:
        raise ValueError('Native interpreter probe differs from the declared original')
    roots = dependency_roots(native, identity)
    check_dependencies(native, identity, roots)
    base_dependencies = {}
    for root in roots:
        if native['kind'] != 'fixture' and root == Path(native['payload_path']).resolve():
            continue
        _, base_dependencies[str(root)] = inventory(root, directory_modes=True)
        for path in root.rglob('*'):
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not any(target == item or target.is_relative_to(item) for item in roots):
                    raise ValueError(f'Base dependency link escapes admitted authority: {path}')
    subprocess.run([native['python'], '-B', '-I', '-m', 'venv', '--without-pip', '--copies', str(HOST)],
                   env=CLEAN_ENV, check=True, timeout=120)
    host_identity = json.loads(subprocess.check_output(
        [str(PYTHON), '-B', '-I', '-c', PROBE], env=CLEAN_ENV, text=True, timeout=120))
    if (host_identity['executable'] != str(PYTHON) or host_identity['prefix'] != str(HOST)
            or (Path(host_identity['base_prefix']).resolve(strict=True)
                != Path(identity['base_prefix']).resolve(strict=True))
            or any(host_identity[key] != identity[key] for key in ('version', 'abi'))):
        raise ValueError('Derived native host changed interpreter identity or ABI')
    sites = host_identity['sites']
    if len(sites) != 1 or not Path(sites[0]).is_relative_to(HOST):
        raise ValueError('Derived native host has unexpected site directories')
    site = Path(sites[0])
    bootstrap = site / '_nvgym_native_host.py'
    bootstrap.write_bytes(bootstrap_bytes(identity))
    bootstrap.chmod(0o644)
    startup = site / '00-nvgym-native-host.pth'
    startup.write_text('import sys; sys.dont_write_bytecode = True; import _nvgym_native_host\n')
    startup.chmod(0o644)
    helper_identity()
    aliases = (construct_entrypoint(identity, roots, protect_git=True) if protect_git
               else construct_entrypoint(identity, roots))
    arguments = ['git'] if protect_git else []
    recursive = ('import subprocess,sys\n'
                 'print(subprocess.check_output([sys.executable,"-I","-c",'
                 + repr(ENTRY_OBSERVATION) + ',' + ','.join(map(repr, arguments))
                 + '],env={},text=True),end="")')
    observations = {}
    with tempfile.TemporaryDirectory(prefix='nvgym-native-entry-') as directory:
        for name, program in (('first', ENTRY_OBSERVATION), ('recursive', recursive)):
            observations[name] = json.loads(subprocess.check_output(
                [str(PYTHON), '-I', '-c', program, *arguments], env={}, cwd=directory, text=True, timeout=120))
            check_entry_observation(observations[name], identity, protect_git=protect_git)
    policy = entry_policy(aliases, observations, protect_git=protect_git)
    if policy['backend_sha256'] != digest(Path(identity['executable_target'])):
        raise ValueError('Native host backend differs from original admitted executable')
    records, host = host_inventory(roots)
    artifact = ROOT / 'deployment/native-host-files.json'
    artifact.write_text(json.dumps(records, sort_keys=True))
    artifact.chmod(0o644)
    receipt = {'schema': 'nvgym.native-host.v4', 'prefix': str(HOST), 'python': str(PYTHON),
               'original_python': native['python'], 'native_input': native, 'native_authority': authority,
               'entry_policy': policy, 'original_identity': identity, 'dependency_roots': [str(root) for root in roots],
               'base_dependencies': base_dependencies, 'paired_roots': [str(path) for path in PAIRED_ROOTS],
               'source': source, 'release_input_sha256': provenance['release_input_sha256']
               if 'release_input_sha256' in provenance else provenance['release_input']['release_input_sha256'],
               'owner_sha256': digest(Path(__file__)), 'bootstrap_path': str(bootstrap),
               'bootstrap_sha256': digest(bootstrap), 'inventory_path': str(artifact),
               'inventory_sha256': digest(artifact), 'inventory': host}
    verify_native_host(receipt, native, authority, provenance)
    return receipt


def verify_native_host(receipt, native, authority, provenance):
    """Read-only verification. This owner never reconstructs a missing host."""
    expected = {'schema': 'nvgym.native-host.v4', 'prefix': str(HOST), 'python': str(PYTHON),
                'original_python': native['python'], 'native_input': native, 'native_authority': authority,
                'paired_roots': [str(path) for path in PAIRED_ROOTS],
                'source': paired_inventory(provenance), 'owner_sha256': digest(Path(__file__)),
                'release_input_sha256': provenance.get('release_input_sha256')
                or provenance['release_input']['release_input_sha256']}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('Native host authority/source binding differs from admission')
    identity = receipt['original_identity']
    policy = receipt['entry_policy']
    observations = policy['observations']
    if set(observations) != {'first', 'recursive'}:
        raise ValueError('Native host startup observation set differs')
    for observation in observations.values():
        check_entry_observation(observation, identity, protect_git=policy['git_read_only'])
    check_entry_policy(policy, entry_policy(policy['aliases'], observations, protect_git=policy['git_read_only']))
    if policy['backend_sha256'] != digest(Path(identity['executable_target'])):
        raise ValueError('Native host executable policy differs from admission')
    roots = dependency_roots(native, identity)
    if receipt['dependency_roots'] != [str(root) for root in roots]:
        raise ValueError('Native host dependency roots differ')
    check_dependencies(native, identity, roots)
    expected_base = {str(root) for root in roots if native['kind'] == 'fixture'
                     or root != Path(native['payload_path']).resolve()}
    if set(receipt['base_dependencies']) != expected_base:
        raise ValueError('Native host base dependency set differs')
    for root, expected_identity in receipt['base_dependencies'].items():
        _, observed = inventory(Path(root), directory_modes=True)
        if observed != expected_identity:
            raise ValueError('Native host base dependency inventory differs')
        for path in Path(root).rglob('*'):
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not any(target == item or target.is_relative_to(item) for item in roots):
                    raise ValueError(f'Base dependency link escapes admitted authority: {path}')
    artifact = ROOT / 'deployment/native-host-files.json'
    protected(artifact)
    if receipt['inventory_path'] != str(artifact) or digest(artifact) != receipt['inventory_sha256']:
        raise ValueError('Native host inventory artifact differs')
    records, observed = host_inventory(roots)
    if records != json.loads(artifact.read_text()) or observed != receipt['inventory']:
        raise ValueError('Exact native host inventory differs')
    bootstrap = Path(receipt['bootstrap_path'])
    if (not bootstrap.is_relative_to(HOST) or bootstrap.is_symlink()
            or bootstrap.read_bytes() != bootstrap_bytes(identity)
            or digest(bootstrap) != receipt['bootstrap_sha256']):
        raise ValueError('Native host bootstrap policy differs')
    return {'host': observed, 'source': receipt['source'], 'base_dependencies': receipt['base_dependencies']}
