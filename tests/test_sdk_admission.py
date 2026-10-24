"""Immutable SDK filesystem and real-child checks for the authorized remote runner."""

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from portable import native_admission, native_host, sdk_admission
from portable.native_inventory import inventory
from portable.release import ReleaseInput, digest

ROOT = Path(__file__).resolve().parents[1]


def bind_sdk(monkeypatch, root):
    monkeypatch.setattr(sdk_admission, 'ROOT', root)
    monkeypatch.setattr(sdk_admission, 'SDK', root / 'sdk')
    monkeypatch.setattr(sdk_admission, 'PYTHON', root / 'sdk/bin/python')


@pytest.fixture
def sdk_files(tmp_path, monkeypatch):
    if os.getuid() != 0:
        pytest.skip('SDK admission requires image-owned root files')
    root = tmp_path / 'image'
    sdk = root / 'sdk'
    site = sdk / 'lib/python3.11/site-packages'
    site.mkdir(parents=True)
    (sdk / 'bin').mkdir()
    (root / 'deployment').mkdir()
    base = tmp_path / 'original-base'
    (base / 'lib/python3.11/site-packages').mkdir(parents=True)
    python = sdk / 'bin/python'
    python.write_text('#!/bin/sh\nexit 98\n')
    python.chmod(0o755)
    (sdk / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    (site / 'numpy').mkdir()
    (site / 'numpy/extension.so').write_bytes(b'test-owned-extension')
    bind_sdk(monkeypatch, root)
    original = {'base_prefix': str(base), 'implementation': 'cpython', 'version': [3, 11, 15],
                'abi': 'cpython-311-x86_64-linux-gnu', 'sites': [str(base / 'lib/python3.11/site-packages')]}
    host = {'original_identity': original, 'dependency_roots': [str(base)],
            'paired_roots': [str(root / 'code/nanoverse')]}
    (sdk / 'bin/python3').symlink_to('python')
    (sdk / 'bin/python3.11').symlink_to('python3')
    backend = sdk_admission.construct_entrypoint(host)
    identity = {name: original[name] for name in ('base_prefix', 'implementation', 'version', 'abi')}
    identity.update(executable=backend['path'], executable_target=backend['target'], prefix=str(sdk),
                    sites=[str(site)], dont_write_bytecode=True)
    records, admitted = sdk_admission.sdk_inventory(host)
    artifact = root / 'deployment/sdk-files.json'
    artifact.write_text(json.dumps(records, sort_keys=True))
    receipt = {'schema': 'nvgym.sdk-admission.v2', 'prefix': str(sdk), 'python': str(python),
               'owner_sha256': digest(Path(sdk_admission.__file__)),
               'native_host_sha256': sdk_admission.host_digest(host), 'identity': identity,
               'entrypoint': {'path': str(python), 'sha256': digest(python), 'mode': 0o555,
                              'backend': backend['path'], 'arguments': ['-B']},
               'backend': backend,
               'inventory_path': str(artifact), 'inventory_sha256': digest(artifact), 'inventory': admitted}
    metadata = {'config': {'sdk': {'python': str(python)}}}
    return metadata, receipt, host


def test_verification_is_read_only_and_does_not_execute_the_sdk(sdk_files, monkeypatch):
    metadata, receipt, host = sdk_files

    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only SDK verification must not start an interpreter')

    monkeypatch.setattr(sdk_admission.subprocess, 'check_output', forbidden)
    observed = sdk_admission.verify_sdk(metadata, receipt, host)
    assert observed == {'sdk': receipt['inventory']}


@pytest.mark.parametrize('mutation', ('change', 'add', 'remove_entry', 'change_entry', 'pyvenv',
                                     'extension', 'executable', 'escape', 'dangling', 'mode', 'root_mode',
                                     'inventory', 'receipt', 'native_host', 'foreign_hook', 'unprotected',
                                     'backend_bytes', 'backend_link', 'alias', 'source_target', 'old_schema'))
def test_sdk_rejects_tampering_without_repair_or_reseal(sdk_files, tmp_path, mutation):
    metadata, receipt, host = sdk_files
    sdk = sdk_admission.SDK
    entry = Path(receipt['entrypoint']['path'])
    backend = Path(receipt['backend']['path'])
    site = Path(receipt['identity']['sites'][0])
    artifact = Path(receipt['inventory_path'])
    if mutation == 'change':
        (sdk / 'bin/python').write_text('changed interpreter')
    elif mutation == 'add':
        (sdk / 'unexpected.pyc').write_bytes(b'new bytecode')
    elif mutation == 'remove_entry':
        entry.unlink()
    elif mutation == 'change_entry':
        entry.write_bytes(b'#!/bin/sh\nexec /foreign/python \"$@\"\n')
    elif mutation == 'pyvenv':
        (sdk / 'pyvenv.cfg').write_text('include-system-site-packages = true\n')
    elif mutation == 'extension':
        (site / 'numpy/extension.so').write_bytes(b'changed extension')
    elif mutation in ('executable', 'escape', 'dangling'):
        link = sdk / ('bin/python' if mutation == 'executable' else 'external-link')
        if link.exists():
            link.unlink()
        target = tmp_path / ('absent' if mutation == 'dangling' else 'unadmitted')
        if mutation != 'dangling':
            target.write_bytes(b'outside SDK or admitted base')
        link.symlink_to(target)
    elif mutation == 'mode':
        entry.chmod(0o500)
    elif mutation == 'root_mode':
        sdk.chmod(0o700)
    elif mutation == 'inventory':
        artifact.write_text('{}')
    elif mutation == 'receipt':
        receipt = {**receipt, 'owner_sha256': '0' * 64}
    elif mutation == 'native_host':
        host = {**host, 'different_sibling_authority': True}
    elif mutation == 'unprotected':
        entry.chmod(0o777)
    elif mutation == 'backend_bytes':
        backend.write_bytes(b'changed backend bytes')
    elif mutation == 'backend_link':
        backend.unlink()
        backend.symlink_to(entry)
    elif mutation == 'alias':
        alias = entry.with_name('python3')
        alias.unlink()
        alias.symlink_to('_nvgym-python')
    elif mutation == 'source_target':
        receipt['backend']['source_target'] = str(tmp_path / 'unadmitted-source')
    elif mutation == 'old_schema':
        receipt['schema'] = 'nvgym.sdk-admission.v1'
    else:
        (site / 'foreign-startup.pth').write_text('import forbidden_host\n')
    before = inventory(sdk, directory_modes=True, protected=False)[1]
    with pytest.raises((ValueError, FileNotFoundError)):
        sdk_admission.verify_sdk(metadata, receipt, host)
    assert inventory(sdk, directory_modes=True, protected=False)[1] == before
    assert not (mutation == 'remove_entry' and entry.exists())


@pytest.mark.parametrize('location', ('ancestor', 'root', 'descendant', 'deployment'))
def test_sdk_rejects_mounts_covering_its_canonical_bytes(sdk_files, monkeypatch, location):
    metadata, receipt, host = sdk_files
    sdk = sdk_admission.SDK
    mount = {'ancestor': sdk.parent, 'root': sdk, 'descendant': sdk / 'lib',
             'deployment': sdk.parent / 'deployment'}[location]
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/mountinfo'):
            return ('1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / ' + str(mount)
                    + ' rw - tmpfs tmpfs rw\n')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError, match='supplied by a mount'):
        sdk_admission.verify_sdk(metadata, receipt, host)


def test_admission_refuses_preexisting_hook_before_starting_sdk(sdk_files, monkeypatch):
    metadata, receipt, host = sdk_files
    artifact = Path(receipt['inventory_path'])
    artifact.unlink()
    site = Path(receipt['identity']['sites'][0])
    (site / 'old-sdk-policy.pth').write_text('import sys; sys.dont_write_bytecode = True\n')

    def forbidden(*args, **kwargs):
        raise AssertionError('A conflicting startup policy must never execute')

    monkeypatch.setattr(sdk_admission.subprocess, 'check_output', forbidden)
    with pytest.raises(ValueError, match='late startup hooks'):
        sdk_admission.admit_sdk(metadata, host)
    assert not artifact.exists()


def test_admission_checks_system_sites_before_any_sdk_process(sdk_files, monkeypatch):
    metadata, receipt, host = sdk_files
    Path(receipt['inventory_path']).unlink()
    (sdk_admission.SDK / 'pyvenv.cfg').write_text('include-system-site-packages = true\n')

    def forbidden(*args, **kwargs):
        raise AssertionError('System site packages must be rejected before Python starts')

    monkeypatch.setattr(sdk_admission.subprocess, 'check_output', forbidden)
    with pytest.raises(ValueError, match='disable native system site packages'):
        sdk_admission.admit_sdk(metadata, host)


def test_sdk_rejects_special_files_without_opening_them(sdk_files):
    metadata, receipt, host = sdk_files
    os.mkfifo(sdk_admission.SDK / 'unexpected-fifo', mode=0o600)
    with pytest.raises(ValueError, match='Special native seed entry'):
        sdk_admission.verify_sdk(metadata, receipt, host)


@pytest.mark.parametrize('missing_policy', (False, True))
def test_failure_in_one_authority_cannot_skip_the_others(monkeypatch, missing_policy):
    seen = []

    def native(*args, **kwargs):
        seen.append('native')
        raise ValueError('native mutation')

    def host(*args):
        seen.append('host')
        raise ValueError('source mutation before base and host checks')

    def source(*args):
        seen.append('source')
        raise ValueError('source mutation')

    def host_files(*args):
        seen.append('host_files')
        return {}, {'host': 'expected'}

    def base_files(*args, **kwargs):
        seen.append('base_files')
        return {}, {'base': 'expected'}

    def sdk(*args):
        seen.append('sdk')
        raise ValueError('SDK mutation')

    metadata = {'environment': 'exemplar', 'provenance': {'release_input': {'native_input': {'kind': 'fixture'}}}}
    receipt = {'native_host': {'native_authority': {}, 'dependency_roots': ['/base'],
                              'inventory': {'host': 'expected'},
                              'base_dependencies': {'/base': {'base': 'expected'}}}, 'sdk': {}}
    if not missing_policy:
        receipt['native_host']['entry_policy'] = {'git_read_only': False}
    monkeypatch.setattr(native_admission, 'verify_authority', native)
    monkeypatch.setattr(native_admission, 'check_bindings', lambda *args: None)
    monkeypatch.setattr(native_admission, 'verify_native_host', host)
    monkeypatch.setattr(native_admission, 'paired_inventory', source)
    monkeypatch.setattr(native_admission, 'host_inventory', host_files)
    monkeypatch.setattr(native_admission, 'inventory', base_files)
    monkeypatch.setattr(native_admission, 'verify_sdk', sdk)
    with pytest.raises(ValueError, match='native mutation.*SDK mutation'):
        native_admission.verify_admission(metadata, receipt)
    expected = ['native', 'host', 'source', 'host_files', 'base_files', 'sdk']
    if missing_policy:
        expected.remove('host')
    assert seen == expected


def test_build_validation_failure_retains_original_receipt_and_finally_verifies_all(tmp_path, monkeypatch):
    deployment = tmp_path / 'deployment'
    deployment.mkdir()
    original = {'mode': 'fixture', 'image': 'test', 'python': '/original/python'}
    artifact = deployment / 'native-admission.json'
    artifact.write_text(json.dumps(original))
    metadata = {'environment': 'exemplar', 'provenance': {'release_input': {'native_input': {}}}}
    (deployment / 'runtime.json').write_text(json.dumps(metadata))
    seen = []

    def verify(metadata, receipt):
        seen.append('full_verification')
        assert receipt['sdk'] == {'sealed_sdk': True}
        return {'all': 'unchanged'}

    def validate(metadata, sdk, host):
        seen.append('validation')
        assert json.loads(artifact.read_text()) == original
        raise RuntimeError('real owner validation refused')

    monkeypatch.setattr(native_admission, 'ROOT', tmp_path)
    monkeypatch.setattr(native_admission, 'prepare', lambda native: dict(original))
    monkeypatch.setattr(native_admission, 'create_native_host', lambda *args, **kwargs: {'sealed_host': True})
    monkeypatch.setattr(native_admission, 'admit_sdk', lambda *args: {'sealed_sdk': True})
    monkeypatch.setattr(native_admission, 'verify_admission', verify)
    monkeypatch.setattr(native_admission, 'validate', validate)
    monkeypatch.setattr(sys, 'argv', ['native_admission', 'validate'])
    with pytest.raises(RuntimeError, match='real owner validation refused'):
        native_admission.main()
    assert seen == ['full_verification', 'validation', 'full_verification']
    assert json.loads(artifact.read_text()) == original


@pytest.fixture
def real_sdk(tmp_path, monkeypatch, request):
    if os.getuid() != 0:
        pytest.skip('Real SDK admission requires image-owned root files')
    python = os.environ.get('NVGYM_TEST_NATIVE_PYTHON')
    if not python:
        pytest.skip('Set NVGYM_TEST_NATIVE_PYTHON to the supported standalone native Python')
    base = Path(subprocess.check_output(
        [python, '-B', '-I', '-c', 'import sys;print(sys.base_prefix)'],
        env=native_host.CLEAN_ENV, text=True).strip())
    copied = tmp_path / 'original-base'
    shutil.copytree(base, copied, symlinks=True)
    copied_python = copied / 'bin/python3.11'
    root = tmp_path / 'image'
    (root / 'deployment').mkdir(parents=True)
    topology = getattr(request, 'param', 'symlink')
    command = [str(copied_python), '-B', '-I', '-m', 'venv', '--without-pip']
    if topology == 'copies':
        command.append('--copies')
    subprocess.run([*command, str(root / 'sdk')], env=native_host.CLEAN_ENV, check=True)
    if topology == 'indirect':
        for name in ('python', 'python3', 'python3.11'):
            (root / 'sdk/bin' / name).unlink()
        (root / 'sdk/bin/python3.11').symlink_to(copied_python)
        (root / 'sdk/bin/python3').symlink_to('python3.11')
        (root / 'sdk/bin/python').symlink_to('python3')
    bind_sdk(monkeypatch, root)
    original = json.loads(subprocess.check_output(
        [str(copied_python), '-B', '-I', '-c', native_host.PROBE],
        env=native_host.CLEAN_ENV, text=True))
    host = {'original_identity': original, 'dependency_roots': [str(copied)],
            'paired_roots': [str(root / 'code/nanoverse'), str(root / 'code/ember_bridge/src')]}
    metadata = {'config': {'sdk': {'python': str(root / 'sdk/bin/python')}}}
    return metadata, host, copied, root


@pytest.mark.parametrize('real_sdk', ('symlink', 'copies', 'indirect'), indirect=True)
def test_real_sdk_entry_preserves_cold_base_and_argument_fidelity(real_sdk):
    metadata, host, base, root = real_sdk
    site = next((root / 'sdk/lib').glob('python*/site-packages'))
    module = site / 'nvgym_owned_cold_import.py'
    module.write_text('VALUE = 42\n')
    before = inventory(base, directory_modes=True)[1]
    receipt = sdk_admission.admit_sdk(metadata, host)
    assert inventory(base, directory_modes=True)[1] == before
    staged = root.parent / 'client'
    staged.mkdir()
    wrapper = staged / 'python3'
    wrapper.write_text('#!/bin/sh\nexec ' + receipt['python'] + ' -s "$@"\n')
    wrapper.chmod(0o555)
    result = subprocess.check_output(
        [str(wrapper), '-c', 'import sys,nvgym_owned_cold_import; '
         'import encodings.aliases,encodings.utf_8,decimal,http.client; '
         'assert nvgym_owned_cold_import.VALUE == 42; print(sys.dont_write_bytecode)'],
        env={'PATH': str(staged) + ':/usr/bin:/bin', 'PYTHONPATH': str(staged)},
        cwd=staged, text=True).strip()
    assert result == 'True'
    assert not list(site.glob('__pycache__/nvgym_owned_cold_import*.pyc'))
    assert receipt['schema'] == 'nvgym.sdk-admission.v2'
    assert receipt['identity']['executable'] == str(root / 'sdk/bin/_nvgym-python')
    assert not (root / 'sdk/bin/_nvgym-python').is_symlink()
    assert not (root / 'sdk/bin/python').is_symlink()
    for name in ('python3', 'python3.11'):
        assert os.readlink(root / 'sdk/bin' / name) == 'python'
    script = staged / 'script with spaces.py'
    program = 'import json,sys;print(json.dumps([sys.argv,sys.executable,sys.prefix,sys.dont_write_bytecode]))'
    script.write_text(program)
    arguments = ['', 'two words', 'double"quote', "single'quote", '$(literal)', '--option']
    minimal = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(staged)}
    for flag in ('-I', '-s', '-E'):
        observed = json.loads(subprocess.check_output(
            [receipt['python'], flag, str(script), *arguments], env=minimal, cwd=staged, text=True))
        assert observed == [[str(script), *arguments], receipt['backend']['path'], str(root / 'sdk'), True]
    (staged / 'entry_module.py').write_text(program)
    observed = json.loads(subprocess.check_output(
        [receipt['python'], '-m', 'entry_module', *arguments], env=minimal, cwd=staged, text=True))
    assert observed == [[str(staged / 'entry_module.py'), *arguments],
                        receipt['backend']['path'], str(root / 'sdk'), True]
    stdin = json.loads(subprocess.check_output(
        [receipt['python'], '-'], input=program, env=minimal, cwd=staged, text=True))
    assert stdin == [['-'], receipt['backend']['path'], str(root / 'sdk'), True]
    code = subprocess.run([receipt['python'], '-c', 'raise SystemExit(23)'], env=minimal, cwd=staged)
    assert code.returncode == 23
    child = subprocess.Popen(
        [receipt['python'], '-u', '-c', 'import os,signal; print(os.getpid()); signal.pause()'],
        env=minimal, cwd=staged, text=True, stdout=subprocess.PIPE)
    try:
        assert int(child.stdout.readline()) == child.pid
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
    assert inventory(base, directory_modes=True)[1] == before
    sdk_admission.verify_sdk(metadata, receipt, host)
    with pytest.raises(ValueError, match='reuse or reseal'):
        sdk_admission.admit_sdk(metadata, host)


def test_real_registered_owner_staging_keeps_sdk_native_base_host_and_source_unchanged(real_sdk, monkeypatch):
    metadata, _, base, root = real_sdk
    pair = Path(os.environ.get('NVGYM_TEST_PAIRED_ROOT', ROOT / '.cache/ttc-pair'))
    packages = Path(os.environ.get('NVGYM_TEST_SDK_PACKAGES', '/opt/nvgym/sdk'))
    if not (pair / 'nanoverse/nanoverse/catalog.py').is_file():
        pytest.skip('Set NVGYM_TEST_PAIRED_ROOT to the frozen paired source root')
    sites = list((packages / 'lib').glob('python*/site-packages'))
    if len(sites) != 1 or not (sites[0] / 'numpy').is_dir() or not (sites[0] / 'PIL').is_dir():
        pytest.skip('Set NVGYM_TEST_SDK_PACKAGES to an offline SDK prefix with the exact NumPy/Pillow wheels')
    sdk_site = next((root / 'sdk/lib').glob('python*/site-packages'))
    for path in sites[0].iterdir():
        if path.name in ('numpy', 'numpy.libs', 'PIL', 'pillow.libs') or path.name.startswith(
                ('numpy-', 'pillow-', 'Pillow-')):
            if path.is_dir():
                shutil.copytree(path, sdk_site / path.name, symlinks=True)
            else:
                shutil.copy2(path, sdk_site / path.name)
    ignored = shutil.ignore_patterns('.git', 'node_modules', '__pycache__', '.env', '.env.*', '*.key')
    for name in ('nanoverse', 'ember_bridge'):
        shutil.copytree(pair / name, root / 'code' / name, symlinks=True, ignore=ignored)
    records = {str(path.relative_to(root)): {'link': os.readlink(path)} if path.is_symlink()
               else {'sha256': digest(path)} for path in (root / 'code').rglob('*')
               if path.is_file() or path.is_symlink()}
    release_files = root / 'deployment/release-files.json'
    release_files.write_text(json.dumps(records, sort_keys=True))
    provenance = {'release_files_sha256': digest(release_files), 'native_pair': {'commit': 'owned-test-pair'},
                  'release_input_sha256': 'a' * 64}
    roots = (root / 'code/nanoverse', root / 'code/nanoverse/envs', root / 'code/ember_bridge/src')
    for name, value in (('ROOT', root), ('HOST', root / 'native-host'),
                        ('PYTHON', root / 'native-host/bin/python'), ('PAIRED_ROOTS', roots)):
        monkeypatch.setattr(native_host, name, value)
    native = {'kind': 'fixture', 'python': str(base / 'bin/python3.11'), 'image': 'test-owned-fixture'}
    authority = {'mode': 'fixture', 'python': native['python'], 'image': native['image']}
    host = native_host.create_native_host(native, authority, provenance)
    release = ReleaseInput.load(ROOT / 'releases/exemplar/release.json')
    config = copy.deepcopy(release.config)
    config['sdk']['python'] = str(root / 'sdk/bin/python')
    config['harness_command'] = [part.replace('/opt/nvgym/', str(root) + '/', 1)
                                 if part.startswith('/opt/nvgym/') else part
                                 for part in config['harness_command']]
    metadata = {'environment': 'exemplar', 'config': config, 'case': {'python': host['python']}}
    before = native_host.verify_native_host(host, native, authority, provenance)
    sdk = sdk_admission.admit_sdk(metadata, host)
    try:
        report = sdk_admission.validate_sdk_staging(metadata, sdk, host)
    finally:
        try:
            after = native_host.verify_native_host(host, native, authority, provenance)
        finally:
            sdk_admission.verify_sdk(metadata, sdk, host)
    assert before == after
    assert report['owner']['packages']['numpy'] == '1.26.4'
    assert report['supplement']['packages']['PIL']['version'] == '11.3.0'
    assert report['supplement']['dont_write_bytecode'] is True
    assert report['manifest']['profile'] == report['profile']
    assert report['manifest']['capabilities'] == report['capabilities']
