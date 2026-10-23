"""Retire the frozen qualifier's endpoint after its native child exits cleanly."""

import errno
import json
import socket
import stat
from pathlib import Path

from portable.native_inventory import digest


def retire_endpoint(output):
    output = Path(output)
    report = json.loads((output / 'qualification.json').read_text())
    if report.get('status') != 'passed' or report.get('process_exit_code') != 0:
        raise ValueError('RoboForge qualifier did not confirm a clean native exit')
    path = output / 'native.sock'
    before = path.lstat()
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != 0:
        raise ValueError('RoboForge qualifier endpoint is not an image-owned socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        code = client.connect_ex(str(path))
    if code != errno.ECONNREFUSED:
        raise ValueError('RoboForge qualifier endpoint is still live or its state is unknown')
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
        raise ValueError('RoboForge qualifier endpoint changed during cleanup')
    path.unlink()
    record = {'endpoint': 'native.sock', 'native_process_exit_code': 0,
              'connect_errno': code, 'removed': True}
    (output / 'endpoint-cleanup.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def public_recipe(recipe):
    verify = recipe.verify
    recipe.SPEC = {**recipe.SPEC, 'qualifier_cleanup_owner_sha256': digest(__file__)}

    def qualify(ctx):
        verify(ctx)
        retire_endpoint(ctx.attempt / 'native')

    recipe.verify = qualify
    return recipe
