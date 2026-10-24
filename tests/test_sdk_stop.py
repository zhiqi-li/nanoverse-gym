# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Remote controlled owners exercise process identity, authorization and close budgets."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from environment_servers.nanoverse_sdk.native import (
    NATIVE_ROOT,
    EpisodePaths,
    EpisodeProcess,
    NativeFailure,
    OperatorCase,
)
from environment_servers.nanoverse_sdk.stop import HarnessStop
from portable import native_host

CLI = r"""
import json,os,pathlib,subprocess,sys
if sys.argv[1]=='validate': sys.exit(0)
config=json.loads(pathlib.Path(sys.argv[sys.argv.index('--config')+1]).read_text())
root=pathlib.Path(sys.argv[sys.argv.index('--output')+1]);(root/'actor').mkdir(parents=True)
(root/'actor/input.json').write_text('{}')
child=subprocess.Popen([*config['harness_command'],str(root/'actor/input.json')])
child.wait()
if os.environ.get('RESTART_HARNESS') == '1':
 child=subprocess.Popen([*config['harness_command'],str(root/'actor/input.json')]);child.wait()
for name,body in [('native/sdk/summary.json',{'closed':True,'executions':0}),
                  ('actor/ttc-runtime.json',{'state':'closed','sdk_probe':'controlled process fixture'})]:
 p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(body))
"""
HARNESS = r"""
import os,pathlib,signal,sys,time
ready=pathlib.Path(sys.argv[1]).with_name('ready')
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
ready.write_text(str(os.getpid()))
while True: time.sleep(.01)
"""


@pytest.fixture(params=('raw', 'admitted-entry'))
def owner(tmp_path, request, monkeypatch):
    python = Path(sys.executable)
    if request.param == 'admitted-entry':
        prefix = tmp_path / 'native-python'
        subprocess.run([str(python), '-B', '-m', 'venv', '--without-pip', str(prefix)], check=True)
        entry = prefix / 'bin/python'
        entry.unlink()
        entry.with_name('_nvgym-python').symlink_to(python)
        monkeypatch.setattr(native_host, 'PYTHON', entry)
        entry.write_bytes(native_host.entrypoint_bytes())
        entry.chmod(0o555)
        python = entry
    package = tmp_path / 'bridge/src/ember_bridge'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text(CLI)
    harness = tmp_path / 'harness.py'
    harness.write_text(HARNESS)
    config = {'model': {}, 'roles': {'actor': {}}, 'sdk': {},
              'harness_command': [str(python), str(harness)]}
    template = tmp_path / 'config.json'
    template.write_text(json.dumps(config))
    case = OperatorCase(case_id='stop-test', environment='exemplar', python=python,
                        config_path=template, cleanup_seconds=3)
    case._template = config
    output = tmp_path / 'episode'
    output.mkdir()
    return EpisodeProcess(case, EpisodePaths(output), NATIVE_ROOT, tmp_path / 'bridge')


async def started(owner):
    await owner.start(model_base_url='http://unused.invalid/v1', model_id='controlled-test')
    async with asyncio.timeout(5):
        while not (owner.paths.native / 'actor/ready').exists():
            await asyncio.sleep(.01)


async def test_authenticated_request_binds_once_and_native_finalizers_survive(owner):
    await started(owner)
    try:
        token = owner.environment()['NV_API_KEY']
        assert token not in repr(owner)
        await owner.native_request('forged')
        assert owner._harness_stop is None and not owner._request_seen
        await owner.native_request(token)
        target = owner._harness_stop
        assert target is not None
        await owner.native_request(token)
        assert owner._harness_stop is target
        await owner.aclose()
        assert owner.closed and owner.process.returncode == 0
        assert not owner._capability and owner._harness_stop is None
        assert json.loads((owner.paths.native / 'native/sdk/summary.json').read_text())['closed'] is True
        with pytest.raises(OSError):
            os.fstat(target.descriptor)
        await owner.aclose()
    finally:
        if not owner.closed:
            await owner.aclose()


async def test_wrong_launch_argv_does_not_gain_stop_authority(owner):
    await started(owner)
    try:
        harness = (*owner.case._template['harness_command'], str(owner.paths.native / 'actor/input.json'))
        driver, cli = owner._driver_command, owner._cli_command
        for commands in (
            (driver, ('wrong-cli',), harness),
            ((driver[0], '-S', *driver[1:]), cli, harness),
            (driver, (*cli, '-B'), harness),
            (driver, cli, (*harness, 'unexpected-input')),
        ):
            assert HarnessStop.bind(owner.process.pid, *commands) is None
        owner._closing = True
        async with asyncio.timeout(.1):
            async with owner._lock:
                await owner.native_request(owner.environment()['NV_API_KEY'])
        assert owner._harness_stop is None
    finally:
        with pytest.raises((NativeFailure, FileNotFoundError)):
            await owner.aclose()
        assert not owner.closed and not owner._capability


async def test_repeated_cleanup_cannot_reset_deadline_or_wait_for_late_spawn(owner):
    future = asyncio.create_task(asyncio.sleep(60))
    owner._spawning = future
    owner._cleanup_deadline = asyncio.get_running_loop().time() - 1
    try:
        for _ in range(2):
            async with asyncio.timeout(.2):
                with pytest.raises(NativeFailure, match='quarantined'):
                    await owner.aclose()
        assert not owner.closed and not owner._capability
        assert not future.done()
    finally:
        future.cancel()
        with pytest.raises(asyncio.CancelledError):
            await future


async def test_exited_target_cannot_transfer_readiness_to_identical_replacement(owner):
    owner.case.runtime_env['RESTART_HARNESS'] = '1'
    await started(owner)
    try:
        token = owner.environment()['NV_API_KEY']
        await owner.native_request(token)
        target = owner._harness_stop
        assert target is not None
        assert target.request()
        async with asyncio.timeout(5):
            while int((owner.paths.native / 'actor/ready').read_text()) == target.chain[0].pid:
                await asyncio.sleep(.01)
        await owner.native_request(token)
        assert owner._harness_stop is target
        assert not target.valid() and not target.request()
        with pytest.raises((NativeFailure, FileNotFoundError)):
            await owner.aclose()
        assert not owner.closed and not owner._capability
    finally:
        if owner.process.returncode is None:
            with pytest.raises((NativeFailure, FileNotFoundError)):
                await owner.aclose()


async def test_missing_pidfd_uses_existing_fallback_without_claiming_closure(owner, monkeypatch):
    await started(owner)

    def unavailable(*args):
        raise PermissionError('controlled unsupported pidfd')

    monkeypatch.setattr(os, 'pidfd_open', unavailable)
    await owner.native_request(owner.environment()['NV_API_KEY'])
    assert owner._harness_stop is None
    with pytest.raises((NativeFailure, FileNotFoundError)):
        await owner.aclose()
    assert not owner.closed and owner.process.returncode is not None


async def test_cancelled_close_waiter_reuses_deadline_and_sends_fallback_once(owner):
    await started(owner)
    calls = []
    send = owner.process.send_signal

    def record(sig):
        calls.append(sig)
        send(sig)

    owner.process.send_signal = record
    closing = asyncio.create_task(owner.aclose())
    async with asyncio.timeout(5):
        while not owner._fallback_sent:
            await asyncio.sleep(.001)
    deadline = owner._cleanup_deadline
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises((NativeFailure, FileNotFoundError)):
        await owner.aclose()
    assert owner._cleanup_deadline == deadline
    assert len(calls) == 1
    assert not owner._capability and not owner.closed
