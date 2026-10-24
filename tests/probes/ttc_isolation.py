"""Exercise real TTC boundaries in a remote container, without credentials or simulation assets."""

import asyncio
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from nanoverse.sdk.actor import Actor


async def main():
    output = Path('/output')
    manifest = json.loads(Path('/opt/nvgym/deployment/ttc-manifest.json').read_text())
    manifest['unsafe_no_host_isolation'] = False
    manifest_path = Path('/tmp/strict-manifest.json')
    manifest_path.write_text(json.dumps(manifest))
    canary = Path('/tmp/host-canary-' + uuid.uuid4().hex)
    canary.write_text(uuid.uuid4().hex)
    canary.chmod(0o444)
    actors, stopped = [], set()
    listener_calls = []
    held = attack = None
    record = {'kind': 'real TTC isolation probes; no simulator/model score', 'passed': False,
              'unsafe_no_host_isolation': False, 'tenants': [], 'cleanup': []}

    async def listener(reader, writer):
        listener_calls.append(True)
        writer.write(b'owned nonsecret listener\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(listener, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        filesystem = os.statvfs('/tmp')
        record['sandbox_storage_limit_bytes'] = filesystem.f_blocks * filesystem.f_frsize
        assert 0 < record['sandbox_storage_limit_bytes'] <= 8 * 1024**3
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        assert await reader.readline() == b'owned nonsecret listener\n'
        writer.close()
        await writer.wait_closed()
        record['listener_positive_control'] = True
        for name in ('alpha', 'beta'):
            actor = Actor({'manifest': str(manifest_path), 'output': str(output/name)})
            actors.append(actor)
            await actor.command('daemon', 'start', timeout=65)
            status = json.loads(await actor.command('daemon', 'status'))
            assert status['isolation_mode'] == 'rootless' and status['unsafe_no_host_isolation'] is False
            archive = actor.root/'image.tar'
            shutil.copyfile(actor.deployment.archive, archive)
            archive.chmod(0o444)
            await actor.command('load', '-i', str(archive), timeout=180)
            await actor.command('run', '-d', '--name', actor.name, '--pull', 'never',
                                '--network', 'none', '--pids-limit', '128',
                                actor.deployment.image, actor.deployment.python,
                                '-c', 'import time;time.sleep(600)')
            program = """import json,os,pathlib,socket
checks={}
for label,path in [('host_file',%r),('parent_root',%r)]:
 try: pathlib.Path(path).read_bytes();checks[label]=False
 except OSError: checks[label]=True
checks['uid_nonroot']=os.getuid()==1000
try:
 connection=socket.create_connection(('127.0.0.1',%r),timeout=1)
 connection.close();checks['outer_listener_unreachable']=False
except OSError: checks['outer_listener_unreachable']=True
pathlib.Path('/tmp/tenant-marker').write_text(%r)
print(json.dumps(checks))
""" % (str(canary), f'/proc/{os.getpid()}/root{canary}', port, name)
            result = json.loads(await actor.command('exec', '-u', '1000:1000', actor.branch,
                                                    actor.deployment.python, '-c', program))
            record['tenants'].append({'name': name, 'daemon_status': status, 'checks': result,
                                      'outer_limits': actor.deployment.provenance['effective_outer_envelope']})
        assert all(all(item['checks'].values()) for item in record['tenants'])
        assert len(listener_calls) == 1
        alpha, beta = actors

        async def heartbeat():
            value = await beta.command('exec', '-u', '1000:1000', beta.branch,
                                      beta.deployment.python, '-c',
                                      "from pathlib import Path;print(Path('/tmp/tenant-marker').read_text())")
            assert value.strip() == b'beta'

        fork_program = """import errno,json,subprocess,sys
children=[];failure=None
try:
 for _ in range(160):
  try: children.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']))
  except OSError as error: failure=error.errno;break
finally:
 for child in children: child.kill()
 for child in children: child.wait()
print(json.dumps({'spawned':len(children),'errno':failure}))
"""
        attack = asyncio.create_task(alpha.command('exec', '-u', '1000:1000', alpha.branch,
                                                   alpha.deployment.python, '-c', fork_program, timeout=90))
        heartbeats = 0
        while not attack.done():
            await heartbeat()
            heartbeats += 1
            await asyncio.sleep(.05)
        limits = json.loads(await attack)
        record['process_limit'] = {**limits, 'peer_heartbeats': heartbeats}
        assert 16 <= limits['spawned'] < 160 and limits['errno'] == errno.EAGAIN and heartbeats > 0

        held = await asyncio.create_subprocess_exec(
            *alpha.argv('exec', '-u', '1000:1000', alpha.branch, alpha.deployment.python,
                        '-c', "import time;print('READY',flush=True);time.sleep(600)"),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=alpha.env, **alpha.child_options)
        assert await asyncio.wait_for(held.stdout.readline(), 20) == b'READY\n'
        held.terminate()
        await asyncio.wait_for(held.wait(), 10)
        await alpha.command('daemon', 'stop', '--stop-all', timeout=60)
        stopped.add(alpha.name)
        await heartbeat()
        record['cancel_one_tenant'] = {'client_exit': held.returncode, 'daemon_stopped': True,
                                      'peer_still_serving': True}
        record['passed'] = True
    except (Exception, asyncio.CancelledError) as error:
        record['error_type'] = type(error).__name__
        record['error'] = str(error)[-3000:]
    finally:
        if attack is not None and not attack.done():
            attack.cancel()
            await asyncio.gather(attack, return_exceptions=True)
        if held is not None and held.returncode is None:
            held.kill()
            await held.wait()
        for actor in reversed(actors):
            try:
                if actor.name not in stopped:
                    await actor.command('daemon', 'stop', '--stop-all', timeout=60)
                record['cleanup'].append({'container': actor.name, 'daemon_stopped': True})
            except Exception as error:
                record['cleanup'].append({'container': actor.name, 'error': str(error)})
                record['passed'] = False
        server.close()
        await server.wait_closed()
        canary.unlink()
        (output/'isolation-result.json').write_text(json.dumps(record, indent=2)+'\n')
        print(json.dumps(record), flush=True)
    return 0 if record['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
