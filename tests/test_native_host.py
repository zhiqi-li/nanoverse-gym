"""Native-host startup and tamper checks. Run only in the authorized remote runner."""

import copy
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from portable import native_host
from portable.native_admission import verify_prefix, verify_seed
from portable.native_inventory import inventory
from portable.release import ReleaseInput, digest


@pytest.mark.parametrize('environment', (
    'exemplar', 'robodojo', 'roboforge', 'behavior1k', 'libero', 'molmospaces',
    'robocasa365', 'robolab', 'robotwin'))
def test_public_release_interpreter_bindings(environment):
    release = ReleaseInput.load(Path(__file__).parents[1] / 'releases' / environment / 'release.json')
    host = '/opt/nvgym/native-host/bin/python'
    assert release.case['python'] == host
    assert release.native['python'] != host
    assert release.config['sdk']['python'] == '/opt/nvgym/sdk/bin/python'
    if environment in ('robodojo', 'roboforge'):
        assert release.config['python'] == host
        if environment == 'robodojo':
            assert release.config['simulator_command'][0] == host
    elif environment == 'exemplar':
        assert release.native['python'] == '/usr/local/bin/python3.11'
    else:
        assert release.config['native']['python'] == host


@pytest.fixture
def owned_host(tmp_path, monkeypatch, request):
    if os.getuid() != 0:
        pytest.skip('Native-host admission requires image-owned root files')
    source_python = os.environ.get('NVGYM_TEST_NATIVE_PYTHON310' if getattr(request, 'param', '') == 'python310'
                                   else 'NVGYM_TEST_NATIVE_PYTHON')
    if not source_python:
        pytest.skip('Set NVGYM_TEST_NATIVE_PYTHON to the supported standalone native Python 3.11')
    root = tmp_path / 'image'
    root.mkdir()
    deployment = root / 'deployment'
    deployment.mkdir()
    native = tmp_path / 'payload'
    native.mkdir()
    source_base = Path(subprocess.check_output(
        [source_python, '-B', '-I', '-c', 'import sys; print(sys.base_prefix)'],
        env=native_host.CLEAN_ENV, text=True).strip())
    interpreter_name = subprocess.check_output(
        [source_python, '-B', '-I', '-c',
         'import sys; print("python" + ".".join(map(str, sys.version_info[:2])))'],
        env=native_host.CLEAN_ENV, text=True).strip()
    shutil.copytree(source_base, native / 'python', symlinks=True)
    copied_python = native / 'python/bin' / interpreter_name
    original = native / 'venv'
    prefix = tmp_path / 'original'
    prefix.symlink_to(native, target_is_directory=True)
    if getattr(request, 'param', 'stdlib') == 'uv-alias':
        uv = shutil.which('uv')
        if uv is None:
            pytest.skip('Native uv-prefix regression requires uv')
        subprocess.run([uv, 'venv', '--no-project', '--no-python-downloads',
                        '--python', str(prefix / 'python/bin' / interpreter_name), str(prefix / 'venv')],
                       check=True, env=native_host.CLEAN_ENV)
    else:
        subprocess.run([str(copied_python), '-B', '-I', '-m', 'venv', '--without-pip', '--copies', str(original)],
                       check=True, env=native_host.CLEAN_ENV)
    subprocess.run([str(original / 'bin/python'), '-c', 'pass'],
                   check=True, env=native_host.CLEAN_ENV)
    original_python = prefix / 'venv/bin/python'
    original_site = Path(subprocess.check_output(
        [str(original_python), '-B', '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        env=native_host.CLEAN_ENV, text=True).strip())
    (original_site / '_test_native_hook.py').write_text(
        'import os,sys\nsys.nvgym_native_hook = True\n'
        'sys.nvgym_hook_policy = (sys.dont_write_bytecode, os.environ.get("GIT_OPTIONAL_LOCKS"))\n')
    (original_site / 'native-startup.pth').write_text('import _test_native_hook\n')
    code = root / 'code'
    for name, text in (
            ('nanoverse/nanoverse/__init__.py', ''),
            ('nanoverse/nanoverse/sdk/__init__.py', ''),
            ('nanoverse/nanoverse/sdk/actor.py', 'ACTOR = True\n'),
            ('nanoverse/envs/native_sdk.py', 'DESCRIPTOR = True\n'),
            ('ember_bridge/src/ember_bridge/__init__.py', ''),
            ('ember_bridge/src/ember_bridge/cli.py', 'BRIDGE = True\n')):
        path = code / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    records = {str(path.relative_to(root)): {'sha256': digest(path)}
               for path in code.rglob('*') if path.is_file()}
    artifact = deployment / 'release-files.json'
    artifact.write_text(json.dumps(records, sort_keys=True))
    provenance = {'release_files_sha256': digest(artifact), 'native_pair': {'commit': 'frozen-pair'},
                  'release_input_sha256': 'a' * 64}
    roots = (code / 'nanoverse', code / 'nanoverse/envs', code / 'ember_bridge/src')
    for name, value in (('ROOT', root), ('HOST', root / 'native-host'),
                        ('PYTHON', root / 'native-host/bin/python'), ('PAIRED_ROOTS', roots)):
        monkeypatch.setattr(native_host, name, value)
    native_input = {'kind': 'expanded_seed', 'payload_path': str(native), 'prefix': str(prefix),
                    'python': str(original_python), 'descriptor_path': str(tmp_path / 'seed.json')}
    if getattr(request, 'param', '') in ('git', 'git-unprotected'):
        repository = native / 'git-source'
        repository.mkdir()
        (repository / 'tracked').write_text('unchanged source\n')
        for arguments in (['init', '--quiet'], ['add', 'tracked'],
                          ['-c', 'user.name=fixture', '-c', 'user.email=fixture@localhost',
                           'commit', '--quiet', '-m', 'fixture']):
            subprocess.run(['/usr/bin/git', '-C', str(repository), *arguments],
                           check=True, env=native_host.CLEAN_ENV)
    _, payload_identity = inventory(native)
    Path(native_input['descriptor_path']).write_text(json.dumps({
        'schema': 'nanoverse.native-seed.v1', 'payload': payload_identity}))
    native_input['descriptor_sha256'] = digest(Path(native_input['descriptor_path']))
    authority = verify_seed(native_input)
    topology = getattr(request, 'param', 'stdlib')
    if topology in ('host-symlink', 'host-indirect'):
        construct = native_host.construct_entrypoint

        def install(identity, roots):
            paths = sorted(path for path in native_host.PYTHON.parent.iterdir()
                           if native_host.INTERPRETER_NAME.fullmatch(path.name))
            source = native_host.PYTHON.read_bytes()
            for path in paths:
                path.unlink()
            if topology == 'host-indirect':
                backend = native_host.PYTHON.with_name(interpreter_name)
                backend.write_bytes(source)
                backend.chmod(0o755)
                native_host.PYTHON.with_name('python3').symlink_to(backend.name)
                native_host.PYTHON.symlink_to('python3')
            else:
                native_host.PYTHON.write_bytes(source)
                native_host.PYTHON.chmod(0o755)
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    path.symlink_to('python')
            return construct(identity, roots)

        monkeypatch.setattr(native_host, 'construct_entrypoint', install)
    receipt = native_host.create_native_host(
        native_input, authority, provenance, protect_git=getattr(request, 'param', '') == 'git')
    return native_input, authority, provenance, receipt


@pytest.mark.parametrize('owned_host', ('git-unprotected', 'git'), indirect=True)
def test_git_index_stays_immutable_when_a_child_filters_git_environment(owned_host):
    native, authority, provenance, receipt = owned_host
    repository = Path(native['payload_path']) / 'git-source'
    tracked = repository / 'tracked'
    status = tracked.stat()
    os.utime(tracked, (status.st_atime, status.st_mtime + 7))
    before = digest(repository / '.git/index')
    program = '''import json,os,shutil,subprocess,sys
environment={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
result=subprocess.run(['git','status','--porcelain','--untracked-files=no'],cwd=sys.argv[1],
                      env=environment,check=True,capture_output=True,text=True)
print(json.dumps(dict(status=result.stdout,git=shutil.which('git'))))'''
    result = json.loads(subprocess.check_output(
        [str(native_host.PYTHON), '-I', '-c', program, str(repository)],
        env={'PATH': '/usr/bin:/bin'}, text=True))
    assert result['status'] == ''
    if receipt['entry_policy']['git_read_only']:
        assert result['git'] == str(native_host.PYTHON.with_name('git'))
        assert digest(repository / '.git/index') == before
        assert verify_seed(native) == authority
        native_host.verify_native_host(receipt, native, authority, provenance)
    else:
        assert digest(repository / '.git/index') != before
        with pytest.raises(ValueError, match='full inventory differs'):
            verify_seed(native)


@pytest.mark.parametrize('owned_host', ('git',), indirect=True)
@pytest.mark.parametrize('mutation', ('bytes', 'mode', 'link'))
def test_native_git_entry_tampering_is_rejected(owned_host, mutation):
    native, authority, provenance, receipt = owned_host
    git = native_host.PYTHON.with_name('git')
    if mutation == 'bytes':
        git.chmod(0o755)
        git.write_text('#!/bin/bash\nexit 0\n')
        git.chmod(0o555)
    elif mutation == 'mode':
        git.chmod(0o755)
    else:
        git.unlink()
        git.symlink_to('/usr/bin/git')
    with pytest.raises(ValueError, match='Git entry policy'):
        native_host.verify_native_host(receipt, native, authority, provenance)


@pytest.fixture
def root_metadata(monkeypatch):
    lstat = Path.lstat
    original = lstat(Path('/'))
    metadata = {'kind': stat.S_IFDIR, 'mode': 0o755, 'uid': 0}

    def observed(path, *args, **kwargs):
        if path == Path('/'):
            fields = list(original)
            fields[0] = metadata['kind'] | metadata['mode']
            fields[4] = metadata['uid']
            return os.stat_result(fields)
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', observed)
    return metadata


@pytest.fixture
def root_mode_host(root_metadata, request):
    root_metadata['mode'] = getattr(request, 'param', 0o755)
    return root_metadata, request.getfixturevalue('owned_host')


@pytest.mark.parametrize('root_mode_host,observed_mode,accepted', (
    (0o755, 0o755, True), (0o755, 0o555, True),
    (0o555, 0o555, True), (0o555, 0o755, False)), indirect=['root_mode_host'])
def test_native_host_root_permission_transition_is_read_only(
        root_mode_host, observed_mode, accepted, tmp_path, monkeypatch):
    metadata, (native, authority, provenance, receipt) = root_mode_host
    admitted_mode = metadata['mode']
    receipt_path = tmp_path / 'sealed-native-host.json'
    receipt_path.write_text(json.dumps(receipt, sort_keys=True))
    original_receipt = receipt_path.read_bytes()
    original_host = inventory(native_host.HOST, directory_modes=True)
    original_artifact = Path(receipt['inventory_path']).read_bytes()
    original_root = os.lstat('/')
    metadata['mode'] = observed_mode

    def refuse_execution(*args, **kwargs):
        raise AssertionError('Read-only native admission executed a child process')

    monkeypatch.setattr(native_host.subprocess, 'Popen', refuse_execution)
    for _ in range(2):
        if accepted:
            native_host.verify_native_host(receipt, native, authority, provenance)
        else:
            with pytest.raises(ValueError, match='less restrictive'):
                native_host.verify_native_host(receipt, native, authority, provenance)
    assert receipt['schema'] == 'nvgym.native-host.v4'
    assert receipt['entry_policy']['helper']['chain']['/']['mode'] == admitted_mode
    assert json.dumps(receipt, sort_keys=True).encode() == original_receipt
    assert receipt_path.read_bytes() == original_receipt
    assert inventory(native_host.HOST, directory_modes=True) == original_host
    assert Path(receipt['inventory_path']).read_bytes() == original_artifact
    assert os.lstat('/') == original_root


@pytest.mark.parametrize('mode', (
    0o775, 0o757, 0o777, 0o1777, 0o4755, 0o2755, 0o1755,
    0o750, 0o550, 0o500, 0o444, 0o000))
def test_native_helper_rejects_invalid_live_root_modes(root_metadata, mode):
    root_metadata['mode'] = mode
    with pytest.raises(ValueError):
        native_host.helper_identity()


@pytest.mark.parametrize('changes', ({'uid': 1}, {'kind': stat.S_IFREG}, {'kind': stat.S_IFLNK}))
def test_native_helper_rejects_foreign_or_nondirectory_root(root_metadata, changes):
    root_metadata.update(changes)
    with pytest.raises(ValueError):
        native_host.helper_identity()


def test_native_host_rejects_invalid_admitted_root_modes(root_mode_host):
    _, (native, authority, provenance, receipt) = root_mode_host
    for mode in (0o775, 0o757, 0o777, 0o1777, 0o4755, 0o2755, 0o1755,
                 0o750, 0o550, 0o500, 0o444, 0o000, '493', 493.0, True):
        changed = copy.deepcopy(receipt)
        changed['entry_policy']['helper']['chain']['/']['mode'] = mode
        with pytest.raises(ValueError):
            native_host.verify_native_host(changed, native, authority, provenance)


@pytest.mark.parametrize('mutation', (
    'root_missing', 'root_extra', 'root_uid', 'root_uid_bool', 'root_mode_missing',
    'policy_missing', 'policy_unknown', 'helper_extra', 'policy_extra',
    'chain_inserted', 'chain_removed', 'helper_path', 'helper_target', 'legacy_v2'))
def test_native_host_rejects_changed_root_or_helper_contract(root_mode_host, mutation):
    metadata, (native, authority, provenance, receipt) = root_mode_host
    metadata['mode'] = 0o555
    helper = receipt['entry_policy']['helper']
    if mutation == 'root_missing':
        del helper['chain']['/']
    elif mutation == 'root_extra':
        helper['chain']['/']['directory'] = True
    elif mutation == 'root_uid':
        helper['chain']['/']['uid'] = 1
    elif mutation == 'root_uid_bool':
        helper['chain']['/']['uid'] = False
    elif mutation == 'root_mode_missing':
        del helper['chain']['/']['mode']
    elif mutation == 'policy_missing':
        del helper['root_mode_policy']
    elif mutation == 'policy_unknown':
        helper['root_mode_policy'] = 'any-root-mode'
    elif mutation == 'helper_extra':
        helper['unexpected'] = True
    elif mutation == 'policy_extra':
        receipt['entry_policy']['unexpected'] = True
    elif mutation == 'chain_inserted':
        helper['chain']['/unexpected'] = {'mode': 0o755, 'uid': 0}
    elif mutation == 'chain_removed':
        del helper['chain']['/usr/bin']
    elif mutation == 'helper_path':
        helper['path'] = '/usr/bin/bash'
    elif mutation == 'helper_target':
        helper['target'] = '/unadmitted/bash'
    else:
        receipt['schema'] = 'nvgym.native-host.v2'
    with pytest.raises(ValueError):
        native_host.verify_native_host(receipt, native, authority, provenance)


@pytest.mark.parametrize('path,changes', (
    ('/usr', {'mode': 0o555}), ('/usr/bin', {'mode': 0o555}),
    ('/usr/bin', {'uid': 1}), ('/usr/bin', {'kind': stat.S_IFREG}),
    ('/usr/bin/bash', {'mode': 0o555})))
def test_native_host_rejects_other_helper_metadata_drift(root_mode_host, monkeypatch, path, changes):
    metadata, (native, authority, provenance, receipt) = root_mode_host
    metadata['mode'] = 0o555
    lstat = Path.lstat

    def observed(candidate, *args, **kwargs):
        value = lstat(candidate, *args, **kwargs)
        if candidate == Path(path):
            fields = list(value)
            fields[0] = changes.get('kind', stat.S_IFMT(value.st_mode)) | changes.get(
                'mode', stat.S_IMODE(value.st_mode))
            fields[4] = changes.get('uid', value.st_uid)
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'lstat', observed)
    with pytest.raises(ValueError):
        native_host.verify_native_host(receipt, native, authority, provenance)


@pytest.mark.parametrize('mutation', ('bash_bytes', 'bin_link'))
def test_native_host_rejects_changed_helper_bytes_or_link(root_mode_host, monkeypatch, mutation):
    metadata, (native, authority, provenance, receipt) = root_mode_host
    metadata['mode'] = 0o555
    if mutation == 'bash_bytes':
        digest_file = native_host.digest
        target = Path(receipt['entry_policy']['helper']['target'])
        monkeypatch.setattr(native_host, 'digest',
                            lambda path: 'f' * 64 if path == target else digest_file(path))
    else:
        readlink = os.readlink
        monkeypatch.setattr(os, 'readlink',
                            lambda path, *args, **kwargs: './usr/bin' if Path(path) == Path('/bin')
                            else readlink(path, *args, **kwargs))
    with pytest.raises(ValueError):
        native_host.verify_native_host(receipt, native, authority, provenance)


@pytest.mark.parametrize('mounted', ('/usr', '/usr/bin', '/usr/bin/bash'))
def test_native_helper_rejects_mounted_nonroot_chain(root_metadata, monkeypatch, mounted):
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/mountinfo'):
            return ('1 0 0:1 / / rw - overlay overlay rw\n'
                    f'2 1 0:2 / {mounted} rw - tmpfs tmpfs rw\n')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError, match='supplied by a mount'):
        native_host.helper_identity()


@pytest.mark.parametrize('owned_host', ('stdlib', 'uv-alias', 'host-symlink', 'host-indirect'), indirect=True)
def test_native_host_clean_child_and_watchdog_interpreter_reentry(owned_host, tmp_path):
    native, authority, provenance, receipt = owned_host
    empty = tmp_path / 'unrelated'
    empty.mkdir()
    startup = tmp_path / 'hostile-bash-startup'
    startup.write_text('touch '+str(tmp_path / 'hostile-ran')+'\n')
    program = '''import json,os,subprocess,sys
import nanoverse.sdk.actor,ember_bridge.cli,native_sdk
from pathlib import Path
assert sys.nvgym_native_hook
assert sys.nvgym_hook_policy == (True, '0')
assert sys.dont_write_bytecode and os.environ['GIT_OPTIONAL_LOCKS'] == '0'
assert os.readlink('/proc/self/exe') == str(Path(sys.executable).with_name('_nvgym-python'))
assert sys.executable == sys.argv[1]
child='import sys,nanoverse.sdk.actor,ember_bridge.cli,native_sdk; '
child+='assert sys.nvgym_native_hook; assert sys.nvgym_hook_policy == (True, \"0\"); '
child+='assert sys.executable == '+repr(sys.argv[1])
subprocess.run([sys.executable,'-c',child],check=True,
    env={})
print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix)))'''
    observed = json.loads(subprocess.check_output(
        [receipt['python'], '-I', '-c', program, receipt['python']],
        env={**native_host.CLEAN_ENV, 'BASH_ENV': str(startup), 'PYTHONDONTWRITEBYTECODE': '0',
             'GIT_OPTIONAL_LOCKS': '1',
             'BASH_FUNC_exec%%': '() { touch '+str(tmp_path / 'hostile-ran')+'; }'},
        cwd=empty, text=True))
    assert observed == {'executable': receipt['python'], 'prefix': receipt['prefix']}
    assert not (tmp_path / 'hostile-ran').exists()
    assert not list(native_host.PAIRED_ROOTS[0].rglob('*.pyc'))
    native_host.verify_native_host(receipt, native, authority, provenance)
    verify_seed(native, repair_prefix=False)


@pytest.mark.parametrize('mutation', ('bootstrap', 'extra_host', 'pyvenv', 'executable', 'extra_source',
                                     'source', 'native_hook', 'prefix', 'backend', 'alias', 'entry_mode',
                                     'receipt', 'helper'))
def test_native_host_rejects_changed_import_authority(owned_host, mutation):
    native, authority, provenance, receipt = owned_host
    if mutation == 'bootstrap':
        Path(receipt['bootstrap_path']).write_text('pass\n')
    elif mutation == 'extra_host':
        (Path(receipt['bootstrap_path']).parent / 'added.pth').write_text('import malicious\n')
    elif mutation == 'pyvenv':
        (native_host.HOST / 'pyvenv.cfg').write_text('home = /unadmitted\n')
    elif mutation == 'executable':
        native_host.PYTHON.write_text('different executable')
    elif mutation == 'backend':
        native_host.PYTHON.with_name('_nvgym-python').write_bytes(b'changed')
    elif mutation == 'alias':
        alias = native_host.PYTHON.with_name('python3')
        alias.unlink()
        alias.symlink_to('_nvgym-python')
    elif mutation == 'entry_mode':
        native_host.PYTHON.chmod(0o755)
    elif mutation == 'receipt':
        receipt['entry_policy']['observations']['first']['dont_write_bytecode'] = False
    elif mutation == 'helper':
        receipt['entry_policy']['helper']['chain']['/']['mode'] = 0
    elif mutation == 'extra_source':
        (native_host.PAIRED_ROOTS[0] / 'new-module.py').write_text('added')
    elif mutation == 'source':
        (native_host.PAIRED_ROOTS[0] / 'nanoverse/sdk/actor.py').write_text('changed')
    elif mutation == 'native_hook':
        site = next((Path(native['payload_path']) / 'venv/lib').glob('python*/site-packages'))
        (site / '_test_native_hook.py').write_text('mutated native startup')
        with pytest.raises(ValueError, match='full inventory differs'):
            verify_seed(native, repair_prefix=False)
        return
    else:
        Path(native['prefix']).unlink()
        with pytest.raises(ValueError, match='cannot recreate'):
            verify_prefix(native)
        assert not Path(native['prefix']).exists()
        return
    with pytest.raises((ValueError, FileNotFoundError)):
        native_host.verify_native_host(receipt, native, authority, provenance)


def test_native_host_rejects_mounted_dependency_ancestors(tmp_path, monkeypatch):
    root = tmp_path / 'baked'
    root.mkdir()
    mountinfo = '1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / '+str(tmp_path)+' rw - tmpfs tmpfs rw\n'
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/mountinfo'):
            return mountinfo
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError, match='supplied by a mount'):
        native_host.reject_mounts([root])


def test_original_prefix_alias_can_live_below_workspace_mount(tmp_path, monkeypatch):
    baked = tmp_path / 'baked'
    baked.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    alias = workspace / 'original'
    alias.symlink_to(baked, target_is_directory=True)
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/mountinfo'):
            return ('1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / '+str(workspace)
                    +' rw - tmpfs tmpfs rw\n')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    native_host.reject_mounts([alias])


@pytest.mark.parametrize('interrupted,mutated', ((False, True), (True, False), (True, True)))
def test_runtime_checks_identity_after_collector_cleanup(tmp_path, monkeypatch, interrupted, mutated):
    from portable import runtime

    root = tmp_path / 'image'
    deployment = root / 'deployment'
    deployment.mkdir(parents=True)
    output = tmp_path / 'output'
    output.mkdir()
    credentials = tmp_path / 'model.env'
    credentials.write_text('')
    admission = deployment / 'native-admission.json'
    admission.write_text('{}')
    metadata = {'environment': 'exemplar', 'episode_seconds': 1,
                'config': {}, 'case': {'case_id': 'fixture'},
                'provenance': {'release_input': {'native_input': {'kind': 'fixture'}},
                               'native_admission_sha256': digest(admission)}}
    (deployment / 'runtime.json').write_text(json.dumps(metadata))
    checks = []

    class Registry:
        @classmethod
        def load(cls, path):
            assert json.loads(path.read_text())['cases'][0]['case_id'] == 'fixture'
            return cls()

        def validate_deployment(self, native, bridge):
            assert native == root / 'code/nanoverse'
            assert bridge == root / 'code/ember_bridge'

    class Collector:
        stopped = False

        def wait(self, timeout=None):
            if interrupted and not self.stopped:
                raise KeyboardInterrupt
            episode = output / 'episode'
            episode.mkdir(exist_ok=True)
            (episode / 'rollouts.jsonl').write_text('{"reward": 1}\n')
            node = episode / 'n/node'
            node.mkdir(parents=True, exist_ok=True)
            (node / 'cleanup.json').write_text('{"complete": true}')
            actor = node / 'n/actor'
            actor.mkdir(parents=True, exist_ok=True)
            (actor / 'ttc-runtime.json').write_text('{"state": "closed"}')
            self.stopped = True
            return 0

        def poll(self):
            return 0 if self.stopped else None

        def send_signal(self, signum):
            self.stopped = True

    collector = Collector()

    def verify(metadata, receipt, *, restore_prefix=False):
        checks.append(restore_prefix)
        if restore_prefix:
            assert not collector.stopped
        else:
            assert collector.stopped
            if mutated:
                raise ValueError('Exact native host inventory differs')
        return {'measured': 'unchanged'}

    monkeypatch.setattr(runtime, 'ROOT', root)
    monkeypatch.setattr(runtime, 'CaseRegistry', Registry)
    monkeypatch.setattr(runtime, 'verify_release', lambda provenance: None)
    monkeypatch.setattr(runtime, 'verify_admission', verify)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda command, env: collector)
    monkeypatch.setattr(runtime.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['portable.runtime', 'exemplar', '--output-dir', str(output),
                                     '--credentials-file', str(credentials)])
    with pytest.raises(RuntimeError if mutated else KeyboardInterrupt):
        runtime.main()
    summary = json.loads((output / 'summary.json').read_text())
    assert checks == [True, False]
    assert summary['status'] == ('failed' if mutated else 'interrupted')
    if mutated:
        assert summary['final_verification_error'] == 'Exact native host inventory differs'
    else:
        assert summary['final_identities'] == {'measured': 'unchanged'}


def test_native_process_keeps_seed_cache_and_index_read_only(tmp_path):
    from environment_servers.nanoverse_sdk.native import EpisodePaths, EpisodeProcess, OperatorCase

    config = tmp_path / 'config.json'
    config.write_text('{}')
    case = OperatorCase(case_id='native', environment='robodojo', python=Path(sys.executable),
                        config_path=config, runtime_env={'PYTHONDONTWRITEBYTECODE': '0',
                                                        'GIT_OPTIONAL_LOCKS': '1'})
    process = EpisodeProcess(case, EpisodePaths(tmp_path / 'episode'), tmp_path / 'native', tmp_path / 'bridge')
    env = process.environment()
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert env['GIT_OPTIONAL_LOCKS'] == '0'


def test_native_entry_prevents_optional_git_index_refresh(owned_host, tmp_path):
    native, authority, provenance, receipt = owned_host
    repository = tmp_path / 'git-fixture'
    repository.mkdir()
    clean = {**native_host.CLEAN_ENV, 'GIT_OPTIONAL_LOCKS': '1'}
    for arguments in (('init', '-q'), ('config', 'user.name', 'fixture'),
                      ('config', 'user.email', 'fixture@example.invalid')):
        subprocess.run(['git', '-C', str(repository), *arguments], env=clean, check=True)
    tracked = repository / 'tracked'
    tracked.write_text('unchanged content\n')
    subprocess.run(['git', '-C', str(repository), 'add', 'tracked'], env=clean, check=True)
    index = repository / '.git/index'
    os.utime(tracked, ns=(1_000_000_000, 1_000_000_000))
    original = index.read_bytes()
    subprocess.run(['git', '-C', str(repository), 'status', '--porcelain'], env=clean, check=True)
    assert index.read_bytes() != original
    os.utime(tracked, ns=(2_000_000_000, 2_000_000_000))
    before = index.read_bytes()
    subprocess.run([receipt['python'], '-I', '-c',
                    'import subprocess,sys; subprocess.run(["git","-C",sys.argv[1],'
                    '"status","--porcelain"],check=True)', str(repository)], env=clean, check=True)
    assert index.read_bytes() == before
    native_host.verify_native_host(receipt, native, authority, provenance)


def test_native_host_verification_never_starts_or_repairs_an_executable(owned_host, monkeypatch):
    native, authority, provenance, receipt = owned_host
    native_host.PYTHON.write_text('changed launcher\n')
    before, _ = inventory(native_host.HOST, directory_modes=True)

    def execute(*args, **kwargs):
        pytest.fail('Read-only verification started an executable')

    monkeypatch.setattr(native_host.subprocess, 'run', execute)
    monkeypatch.setattr(native_host.subprocess, 'check_output', execute)
    with pytest.raises(ValueError):
        native_host.verify_native_host(receipt, native, authority, provenance)
    after, _ = inventory(native_host.HOST, directory_modes=True)
    assert after == before


def test_frozen_robodojo_episode_launches_real_controlled_child(owned_host, tmp_path):
    native, authority, provenance, receipt = owned_host
    pair = Path(os.environ.get('NVGYM_TEST_PAIRED_ROOT', Path(__file__).parents[1] / '.cache/ttc-pair'))
    frozen = pair / 'nanoverse'
    assert (frozen / 'nanoverse/simulators/robodojo/episode.py').is_file()
    evidence = tmp_path / 'child.json'
    child = '''import json,os,sys
from pathlib import Path
import nanoverse.sdk.actor
Path(sys.argv[1]).write_text(json.dumps(dict(executable=sys.executable,
    process_executable=os.readlink('/proc/self/exe'),prefix=sys.prefix,
    dont_write_bytecode=sys.dont_write_bytecode,git_optional_locks=os.environ.get('GIT_OPTIONAL_LOCKS'),
    argv=sys.argv[2:])))
print('{"event": "ready"}',flush=True)
'''
    program = '''import contextlib,json,os,sys,time,types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
for module,names in {
    'backend':('NativeBackend',),
    'services':('RoboDojoServices','run_checker'),
    'policy':('policy_executors','RoboDojoDirectEefExecutor'),
}.items():
    name='nanoverse.simulators.robodojo.'+module
    stub=types.ModuleType(name)
    for field in names: setattr(stub,field,object())
    sys.modules[name]=stub
from nanoverse.simulators.robodojo import episode
from nanoverse.simulators.robodojo.adapter import TaskAdapter
from nanoverse.simulators.robodojo.sdk.server.episode import OwnedProcessGroup
frozen,root,entry,child,evidence=sys.argv[1:]
root=Path(root)
class Harness:
    @classmethod
    def from_config(cls,config): return cls()
    def record_config(self,config,output): pass
class BoundaryReached(Exception): pass
def backend(config,output,*,task_adapter):
    deadline=time.monotonic()+10
    while not Path(evidence).exists():
        if time.monotonic()>deadline: raise TimeoutError('controlled child did not emit evidence')
        time.sleep(.01)
    raise BoundaryReached()
config=dict(integration='robodojo',python=entry,robodojo=str(root),
    simulator_command=[entry,'-I','-c',child,evidence],native_rpc='unix_socket',
    task='boundary-fixture',eval_seed=7,step_limit=23,policies=[])
with patch.object(episode,'Harness',Harness),patch.object(episode,'prepare_kit',lambda config:None), \\
     patch.object(episode,'hosted_policies',lambda *a:contextlib.nullcontext([])), \\
     patch.object(episode,'NativeBackend',backend):
    try:
        episode.run(config,None,root/'episode',task_adapter=TaskAdapter())
    except BoundaryReached:
        pass
    else:
        raise AssertionError('the real child boundary was not reached')
observed=json.loads(Path(evidence).read_text())
assert observed['executable']==entry
assert observed['process_executable']==str(Path(entry).with_name('_nvgym-python'))
assert observed['prefix']==str(Path(entry).parent.parent)
assert observed['dont_write_bytecode'] is True and observed['git_optional_locks']=='0'
expected=['--task','boundary-fixture','--output',str(root/'episode/evaluation'),
    '--socket',config['rpc_socket'],'--eval-seed','7','--max-episode-steps','23',
    '--recorder-mode','synchronous','--headless','--enable_cameras']
assert observed['argv']==expected
owned=OwnedProcessGroup()
try:
    deadline=time.monotonic()+5
    while os.readlink(f'/proc/{owned.guard.pid}/exe') != str(Path(entry).with_name('_nvgym-python')):
        if time.monotonic()>deadline: raise TimeoutError('guard did not exec the owned backend')
        time.sleep(.01)
    assert os.getpgid(owned.guard.pid)==owned.guard.pid
    environment=Path(f'/proc/{owned.guard.pid}/environ').read_bytes().split(bytes([0]))
    assert b'PYTHONDONTWRITEBYTECODE=1' in environment and b'GIT_OPTIONAL_LOCKS=0' in environment
finally:
    owned.close()
assert owned.guard.returncode==0 and owned.descriptor is None
print(json.dumps(dict(owner_child=observed,guard_closed=True)))
'''
    observed = json.loads(subprocess.check_output(
        [receipt['python'], '-I', '-c', program, str(frozen), str(tmp_path), receipt['python'],
         child, str(evidence)],
        env={**native_host.CLEAN_ENV, 'PYTHONDONTWRITEBYTECODE': '0', 'GIT_OPTIONAL_LOCKS': '1'},
        text=True, timeout=30))
    assert observed['guard_closed'] is True
    native_host.verify_native_host(receipt, native, authority, provenance)
    verify_seed(native, repair_prefix=False)


def test_robodojo_explicit_simulator_command_cannot_bypass_host():
    release = ReleaseInput.load(Path(__file__).parents[1] / 'releases/robodojo/release.json')
    metadata = {'environment': 'robodojo', 'case': release.case, 'config': release.config}
    native_host.check_bindings(metadata)
    metadata['config']['simulator_command'][0] = release.native['python']
    with pytest.raises(ValueError, match='bypasses'):
        native_host.check_bindings(metadata)


@pytest.mark.parametrize('owned_host', ['python310'], indirect=True)
def test_python310_copied_host_reenters_without_changing_native_authority(owned_host):
    native, authority, provenance, receipt = owned_host
    original = receipt['original_identity']
    assert original['version'][:2] == [3, 10]
    for observed in receipt['entry_policy']['observations'].values():
        assert observed['base_prefix'] == str(Path(original['base_prefix']).resolve())
        assert observed['base_executable'] == str(native_host.PYTHON)
        assert observed['version'] == original['version']
        assert observed['abi'] == original['abi']
    assert verify_seed(native, repair_prefix=False) == authority
    native_host.verify_native_host(receipt, native, authority, provenance)
