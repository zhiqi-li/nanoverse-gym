"""Prepare only the reviewed BEHAVIOR software roots in a private input stage."""

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_inventory import digest, inventory

SOFTWARE_ROOTS = ('python', 'venv', 'src/BEHAVIOR-1K')
RESOURCES = ('OmniGibson/omnigibson/omnigibson_5_1_0.kit', 'docs/assets/OmniGibson_logo.png')


def member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'behavior1k':
        raise ValueError(f'Unexpected native archive path: {name}')
    return PurePosixPath(*path.parts[1:])


def selected(path):
    parts = path.parts
    if any(part in ('.git', '.behavior1k-datasets', 'omnigibson.key', '.env')
           or part.startswith('.env.') for part in parts):
        return False
    if parts and parts[0] == 'datasets':
        return False
    return any(path == PurePosixPath(root) or PurePosixPath(root) in path.parents
               for root in SOFTWARE_ROOTS)


def link_path(relative, target, prefix, hardlink=False):
    if PurePosixPath(target).is_absolute():
        absolute = PurePosixPath(posixpath.normpath(target))
        if not absolute.is_relative_to(prefix):
            raise ValueError(f'Native software link escapes its original prefix: {relative}')
        result = absolute.relative_to(prefix)
    elif hardlink:
        result = member_path(target)
    else:
        result = PurePosixPath(posixpath.normpath(str(relative.parent / target)))
    if '..' in result.parts or not selected(result):
        raise ValueError(f'Native software link targets excluded content: {relative}')
    return result


def extract_software(archive, payload, prefix):
    seen, links, roots, members = set(), [], set(), []
    with subprocess.Popen(['zstd', '-dc', str(archive)], stdout=subprocess.PIPE) as decoder:
        try:
            with tarfile.open(fileobj=decoder.stdout, mode='r|') as stream:
                for member in stream:
                    relative = member_path(member.name)
                    if relative in seen:
                        raise ValueError(f'Duplicate native archive destination: {relative}')
                    seen.add(relative)
                    if member.isdev() or member.isfifo() or not (
                            member.isdir() or member.isfile() or member.issym() or member.islnk()):
                        raise ValueError(f'Special native archive member: {relative}')
                    if not selected(relative):
                        if relative not in (PurePosixPath('.'), PurePosixPath('src')) and not (
                                '.git' in relative.parts or '.behavior1k-datasets' in relative.parts
                                or 'omnigibson.key' in relative.parts or 'datasets' == str(relative).split('/')[0]
                                or any(p == '.env' or p.startswith('.env.') for p in relative.parts)):
                            raise ValueError(f'Native archive layout differs from reviewed roots: {relative}')
                        continue
                    if member.mode & 0o7000:
                        raise ValueError(f'Privileged native archive mode: {relative}')
                    roots.add(next(root for root in SOFTWARE_ROOTS
                                   if relative == PurePosixPath(root) or PurePosixPath(root) in relative.parents))
                    destination = payload / str(relative)
                    for parent in destination.parents:
                        if parent == payload:
                            break
                        if parent.is_symlink():
                            raise ValueError(f'Native archive parent is a symlink: {relative}')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    record = {'path': str(relative), 'mode': member.mode, 'size': member.size}
                    if member.isdir():
                        destination.mkdir(exist_ok=True)
                        destination.chmod(member.mode & 0o755)
                        record['kind'] = 'directory'
                    elif member.isfile():
                        with stream.extractfile(member) as source, destination.open('xb') as target:
                            shutil.copyfileobj(source, target, 1 << 20)
                        destination.chmod(member.mode & 0o755)
                        record['kind'] = 'file'
                    else:
                        target = link_path(relative, member.linkname, prefix, member.islnk())
                        links.append((destination, target, member.linkname, member.islnk()))
                        record.update(kind='hardlink' if member.islnk() else 'symlink', link=member.linkname)
                    members.append(record)
            if decoder.wait(timeout=60):
                raise ValueError('Native archive decompression failed')
        except BaseException:
            decoder.kill()
            decoder.wait()
            raise
    if roots != set(SOFTWARE_ROOTS):
        raise ValueError('Native archive lacks a reviewed software root')
    # Delaying every link prevents tar ordering from redirecting a later file write.
    for destination, target, original, hardlink in links:
        if destination.exists() or destination.is_symlink() or any(
                parent.is_symlink() for parent in destination.parents if parent != payload):
            raise ValueError('Native archive link conflicts with an extracted path')
        if hardlink:
            source = payload / str(target)
            if not source.is_file() or source.is_symlink():
                raise ValueError('Native archive hardlink target must be an extracted regular file')
            os.link(source, destination)
        else:
            destination.symlink_to(original)
    return members


def cached_object(cache, descriptor, expected):
    matches = [item for item in descriptor['objects']
               if item['sha256'] == expected['sha256'] and item['bytes'] == expected['bytes']]
    if len(matches) != 1:
        raise ValueError('Pinned software object is absent or ambiguous in the cache descriptor')
    source = cache / 'sha256' / expected['sha256'] / 'blob'
    if source.is_symlink() or not source.is_file() or source.stat().st_size != expected['bytes']:
        raise ValueError('Pinned software cache object has the wrong type or size')
    if digest(source) != expected['sha256']:
        raise ValueError('Pinned software cache object hash mismatch')
    return source, matches[0]['uri'].rsplit('/', 1)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o022)
    specification = json.loads(args.input.read_text())
    native = specification['native']
    if digest(Path(__file__)) != native['preparation']['sha256']:
        raise ValueError('Software preparation recipe hash mismatch')
    if digest(Path(__file__).with_name('native_inventory.py')) != specification['inventory_owner_sha256']:
        raise ValueError('Software inventory owner hash mismatch')
    descriptor_path = Path(native['cache_descriptor']['path'])
    if descriptor_path.is_symlink() or digest(descriptor_path) != native['cache_descriptor']['sha256']:
        raise ValueError('Private cache descriptor hash mismatch')
    descriptor = json.loads(descriptor_path.read_text())
    if descriptor.get('schema') != 'envsdk.archive_cache_image.v1' or descriptor.get('environment') != 'behavior1k':
        raise ValueError('Wrong BEHAVIOR cache descriptor')
    cache = descriptor_path.parent
    archive, _ = cached_object(cache, descriptor, native['native_archive'])
    wheel, wheel_name = cached_object(cache, descriptor, native['warp_wheel'])
    args.output.mkdir(parents=True, exist_ok=False)
    payload = args.output / 'payload'
    payload.mkdir()
    prefix = Path(native['prefix'])
    if prefix.exists() or prefix.is_symlink():
        raise ValueError('Private source stage already contains the original native prefix')
    members = extract_software(archive, payload, PurePosixPath(native['prefix']))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.symlink_to(payload, target_is_directory=True)
    for path in payload.rglob('*'):
        if path.is_symlink() and (not path.resolve(strict=True).is_relative_to(payload.resolve())):
            raise ValueError('Prepared software contains a nonlocal or missing link')
    clean = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
             'PYTHONDONTWRITEBYTECODE': '1', 'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    with tempfile.TemporaryDirectory(prefix='nvgym-warp-') as temporary:
        pinned_wheel = Path(temporary) / wheel_name
        shutil.copyfile(wheel, pinned_wheel)
        subprocess.run([native['python'], '-B', '-I', '-m', 'pip', 'install',
                        '--no-index', '--no-deps', '--no-compile', '--force-reinstall', str(pinned_wheel)],
                       check=True, timeout=900, env=clean)
    source = payload / 'src/BEHAVIOR-1K'
    apps = payload / 'venv/lib/python3.11/site-packages/isaacsim/apps'
    for name in RESOURCES:
        target = apps / Path(name).name
        if target.is_symlink():
            raise ValueError('Installed Isaac Sim resource is a symlink')
        shutil.copyfile(source / name, target)
        target.chmod(0o644)
    packages = []
    metadata_paths = sorted(path for path in payload.rglob('METADATA') if path.parent.name.endswith('.dist-info'))
    for path in metadata_paths:
        metadata = BytesParser().parsebytes(path.read_bytes(), headersonly=True)
        packages.append({'name': metadata['Name'], 'version': metadata['Version'],
                         'metadata_path': str(path.relative_to(payload)), 'sha256': digest(path)})
    records, identity = inventory(payload, directory_modes=True)
    (args.output / 'software-files.json').write_text(json.dumps(records, sort_keys=True))
    (args.output / 'software-members.json').write_text(json.dumps(members, sort_keys=True))
    receipt = {'schema': 'nvgym.native-software.v1', 'environment': 'behavior1k',
               'source_image': native['source_image'], 'public_base': native['public_base'],
               'cache_descriptor_sha256': native['cache_descriptor']['sha256'],
               'selected_inputs': {name: native[name] for name in ('native_archive', 'warp_wheel')},
               'preparation_sha256': native['preparation']['sha256'],
               'inventory_owner_sha256': specification['inventory_owner_sha256'],
               'payload': identity, 'original_prefix': native['prefix'], 'python': native['python'],
               'inventory_sha256': digest(args.output / 'software-files.json'),
               'member_inventory_sha256': digest(args.output / 'software-members.json'),
               'python_distributions': packages,
               'source_commit': specification['behavior_commit'],
               'software_notices': [name for name in records if any(
                   token in Path(name).name.lower() for token in ('license', 'notice', 'copyright'))],
               'redistribution': 'requires independent software terms and final-layer review',
               'scope': 'new derived software; no historical expanded-seed identity claim'}
    (args.output / 'software.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
