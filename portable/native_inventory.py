import hashlib
import json
import os
import stat
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


def inventory(payload, *, directory_modes=False, protected=True):
    records, files, size = {}, 0, 0
    for path in sorted(Path(payload).rglob('*')):
        relative = str(path.relative_to(payload))
        status = path.lstat()
        if protected and (status.st_uid != 0 or (
                not stat.S_ISLNK(status.st_mode) and status.st_mode & 0o022)):
            raise ValueError(f'Unprotected native seed entry: {relative}')
        if path.is_symlink():
            record = {'link': os.readlink(path)}
        elif path.is_dir():
            record = {'directory': True}
            if directory_modes:
                record['mode'] = stat.S_IMODE(status.st_mode)
        elif path.is_file():
            record = {'sha256': digest(path), 'mode': (stat.S_IMODE(status.st_mode) if directory_modes
                                                     else status.st_mode & 0o777)}
            files += 1
            size += status.st_size
        else:
            raise ValueError(f'Special native seed entry: {relative}')
        if directory_modes or relative != '.nanoverse-resource.json':
            records[relative] = record
    identity = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return records, {'sha256': identity, 'bytes': size, 'files': files}
