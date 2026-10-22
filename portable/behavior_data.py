import ast
import os
import stat
from pathlib import Path, PurePosixPath

from portable.release import digest


def source_contract(native_root):
    owner = Path(native_root) / 'envs/behavior1k_env'
    values = {}
    for name, wanted in (('install.py', ('RESOURCES', 'IDENTITY_FILES')),
                         ('cells.py', ('ASSET_VERSIONS', 'BEHAVIOR_COMMIT'))):
        for node in ast.parse((owner / name).read_text()).body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in wanted:
                        values[target.id] = ast.literal_eval(node.value)
    if set(values) != {'RESOURCES', 'IDENTITY_FILES', 'ASSET_VERSIONS', 'BEHAVIOR_COMMIT'}:
        raise ValueError('Paired BEHAVIOR source lacks its external data contract')
    resources = values['RESOURCES']
    if set(resources) != {'behavior_assets', 'robot_assets', 'task_instances', 'dataset_key'}:
        raise ValueError('Paired BEHAVIOR resource contract changed')
    names = [*resources.values(), *values['IDENTITY_FILES']]
    if any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts for name in names):
        raise ValueError('BEHAVIOR data contract contains an escaping path')
    if len(values['IDENTITY_FILES']) != 5 or len(values['ASSET_VERSIONS']) != 2:
        raise ValueError('Paired BEHAVIOR identity contract changed')
    return {'schema': 'nvgym.behavior-data.v1', 'required': True, 'mount': '/data',
            'resources': resources, 'asset_versions': values['ASSET_VERSIONS'],
            'identity_files': list(values['IDENTITY_FILES']),
            'behavior_commit': values['BEHAVIOR_COMMIT'],
            'source_files': {name: digest(owner / name) for name in ('install.py', 'cells.py')},
            'identity_scope': 'user-supplied identities; historical extracted hashes unavailable'}


def validate_data(root, contract):
    if root is None:
        raise ValueError('BEHAVIOR requires an explicit --data-root with the official user-supplied data')
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise ValueError('BEHAVIOR data root must be a readable directory')

    def contained(relative):
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError(f'BEHAVIOR data path escapes its mounted root: {relative}')
        return path

    resources = contract['resources']
    for name, relative in resources.items():
        path = contained(relative)
        if name == 'dataset_key':
            status = path.stat()
            if (not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= 65536
                    or not status.st_mode & 0o444):
                raise ValueError('BEHAVIOR requires a nonempty regular omnigibson.key of at most 64 KiB')
            with path.open('rb'):
                pass
        elif not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError(f'BEHAVIOR requires a readable data directory: {relative}')
    versions = {}
    for name, expected in contract['asset_versions'].items():
        path = contained(name + '/VERSION')
        status = path.stat()
        if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= 65536:
            raise ValueError('BEHAVIOR VERSION must be a nonempty regular file of at most 64 KiB')
        version = path.read_text().strip()
        if version != expected:
            raise ValueError(f'BEHAVIOR {name} version differs; expected {expected}')
        versions[name] = version
    identities = {}
    for relative in contract['identity_files']:
        path = contained(relative)
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'BEHAVIOR identity file is missing or empty: {relative}')
        identities[relative] = digest(path)
    return {'schema': 'nvgym.behavior-data-admission.v1', 'versions': versions,
            'identity_files': identities, 'identity_scope': contract['identity_scope'],
            'key_readable': True}
