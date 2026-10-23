import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from environment_servers.nanoverse_sdk.native import SUPPORTED_ENVIRONMENTS

SHA256 = re.compile(r'^[0-9a-f]{64}$')
IMAGE = re.compile(r'^\S+@sha256:[0-9a-f]{64}$')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


def verified_source(base, record):
    path = Path(record['path'])
    if not path.is_absolute():
        path = base / path
    if not SHA256.fullmatch(record['sha256']) or digest(path) != record['sha256']:
        raise ValueError(f'Release source hash mismatch: {path.name}')
    return path


def absolute(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or '\n' in value:
        raise ValueError(f'Invalid image path: {value!r}')
    return path


def bind_paths(config, case, bindings):
    documents = {'config': copy.deepcopy(config), 'case': copy.deepcopy(case)}
    seen = set()
    for binding in bindings:
        if set(binding) != {'document', 'pointer', 'expected', 'destination'}:
            raise ValueError('Path bindings require document, pointer, expected, destination')
        document, pointer = binding['document'], binding['pointer']
        if document not in documents or not isinstance(pointer, str) or not pointer.startswith('/'):
            raise ValueError('Path binding must select an existing config or case JSON pointer')
        identity = (document, pointer)
        if identity in seen:
            raise ValueError('Duplicate path binding')
        seen.add(identity)
        if not isinstance(binding['expected'], str):
            raise ValueError('A path binding expected value must be a string')
        absolute(binding['expected'])
        absolute(binding['destination'])
        tokens = pointer[1:].split('/')
        if any(re.search(r'~(?![01])', token) for token in tokens):
            raise ValueError('Invalid JSON pointer escape')
        tokens = [token.replace('~1', '/').replace('~0', '~') for token in tokens]
        parent = documents[document]
        try:
            for token in tokens[:-1]:
                if isinstance(parent, list) and not re.fullmatch(r'0|[1-9][0-9]*', token):
                    raise ValueError('Invalid JSON pointer array index')
                parent = parent[int(token)] if isinstance(parent, list) else parent[token]
            if isinstance(parent, list) and not re.fullmatch(r'0|[1-9][0-9]*', tokens[-1]):
                raise ValueError('Invalid JSON pointer array index')
            key = int(tokens[-1]) if isinstance(parent, list) else tokens[-1]
            if parent[key] != binding['expected']:
                raise ValueError(f'Path binding expected value differs at {document}{pointer}')
            parent[key] = binding['destination']
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(f'Path binding target absent: {document}{pointer}') from error
    return documents['config'], documents['case']


@dataclass(frozen=True)
class ReleaseInput:
    environment: str
    episode_seconds: int
    config: dict
    case: dict
    native: dict
    artifacts: tuple
    provenance: dict
    source: Path

    @classmethod
    def load(cls, path):
        path = Path(path).resolve(strict=True)
        document = json.loads(path.read_text())
        required = {'schema', 'environment', 'episode_seconds', 'case_id', 'config_source',
                    'registry_source', 'bindings', 'native', 'artifacts', 'qualification'}
        if set(document) != required or document['schema'] != 'nvgym.release-input.v1':
            raise ValueError('Invalid release-input schema or fields')
        if not isinstance(document['qualification'], dict) or not isinstance(
                document['qualification'].get('native_pair'), dict):
            raise ValueError('Qualification must retain the accepted native_pair revision document')
        environment = document['environment']
        if environment not in SUPPORTED_ENVIRONMENTS:
            raise ValueError(f'Unsupported SDK environment {environment!r}')
        seconds = document['episode_seconds']
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
            raise ValueError('episode_seconds must be a positive integer')
        config_path = verified_source(path.parent, document['config_source'])
        registry_path = verified_source(path.parent, document['registry_source'])
        config = json.loads(config_path.read_text())
        registry = json.loads(registry_path.read_text())
        matches = [case for case in registry['cases'] if case['case_id'] == document['case_id']]
        if len(matches) != 1:
            raise ValueError('Release must select exactly one original registry case')
        case = matches[0]
        if not isinstance(document['bindings'], list) or not isinstance(document['artifacts'], list):
            raise ValueError('Release bindings and artifacts must be arrays')
        if case['environment'] != environment or config.get('integration') != environment:
            raise ValueError('Release environment, selected case, and config must agree')
        config, case = bind_paths(config, case, document['bindings'])
        case.pop('config_path', None)
        native = copy.deepcopy(document['native'])
        kind = native.get('kind')
        if kind == 'fixture':
            if environment != 'exemplar' or set(native) != {'kind', 'image', 'python'}:
                raise ValueError('Only exemplar may use the fixture native input')
            absolute(native['python'])
        elif kind in ('expanded_seed', 'software_archive', 'installed_software'):
            fields = {'kind', 'image', 'descriptor_path', 'descriptor_sha256', 'payload_path',
                      'prefix', 'python'}
            if kind == 'software_archive':
                fields = {'kind', 'source_image', 'public_base', 'cache_descriptor', 'native_archive',
                          'warp_wheel', 'preparation', 'payload_path', 'prefix', 'python'}
            if set(native) != fields:
                raise ValueError('Invalid native seed fields')
            if kind in ('expanded_seed', 'installed_software') and not SHA256.fullmatch(native['descriptor_sha256']):
                raise ValueError('Native descriptor requires its exact SHA256')
            paths = (('descriptor_path',) if kind != 'software_archive' else ()) + ('payload_path', 'prefix', 'python')
            for name in paths:
                absolute(native[name])
            prefix, payload = absolute(native['prefix']), absolute(native['payload_path'])
            if kind == 'installed_software':
                python = 'native-venv/bin/python' if environment == 'robolab' else 'venv/bin/python'
                if (environment not in ('robodojo', 'roboforge', 'behavior1k', 'molmospaces', 'libero', 'robotwin', 'robolab', 'robocasa365')
                        or str(payload) != '/opt/envsdk/native/payload'
                        or prefix != payload or native['python'] != str(prefix / python)
                        or native['descriptor_path'] != str(
                            payload.parent / 'preparation/sealed/installed-software.json')):
                    raise ValueError('Official software requires its canonical installation paths')
            elif prefix.is_relative_to(payload) or payload.is_relative_to(prefix):
                raise ValueError('Original prefix and baked payload must be separate image paths')
            if not PurePosixPath(native['python']).is_relative_to(prefix):
                raise ValueError('Native interpreter must be inside its original prefix')
            if kind == 'software_archive':
                if environment != 'behavior1k':
                    raise ValueError('Software archives are supported only for BEHAVIOR')
                if native['payload_path'] != '/opt/envsdk/native/payload':
                    raise ValueError('BEHAVIOR software must use the image-owned native payload')
                for name in ('source_image', 'public_base'):
                    if not IMAGE.fullmatch(native[name]):
                        raise ValueError('Software source and public base must be pinned by digest')
                if native['source_image'] == native['public_base'] or not native['public_base'].startswith(
                        'nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:'):
                    raise ValueError('BEHAVIOR requires an independent public Ubuntu 22.04 CUDA base')
                descriptor = native['cache_descriptor']
                if set(descriptor) != {'path', 'sha256'} or not SHA256.fullmatch(descriptor['sha256']):
                    raise ValueError('Software archive requires the exact cache descriptor')
                absolute(descriptor['path'])
                for name in ('native_archive', 'warp_wheel'):
                    item = native[name]
                    if (set(item) != {'sha256', 'bytes'} or not SHA256.fullmatch(item['sha256'])
                            or type(item['bytes']) is not int or item['bytes'] <= 0):
                        raise ValueError('Software objects require exact SHA256 and byte count')
                preparation = native['preparation']
                if set(preparation) != {'path', 'sha256'}:
                    raise ValueError('Software archive requires one pinned preparation recipe')
                verified_source(path.parent, preparation)
                if (config['native'].get('data') != '/data'
                        or config['native'].get('appdata') != '/output/behavior-appdata'):
                    raise ValueError('BEHAVIOR requires checked external data and writable appdata bindings')
        else:
            raise ValueError('Unknown native preparation kind')
        if environment == 'behavior1k' and kind not in ('software_archive', 'installed_software'):
            raise ValueError('BEHAVIOR requires software-only native preparation')
        if environment == 'behavior1k' and (config['native'].get('data') != '/data'
                or config['native'].get('appdata') != '/output/behavior-appdata'):
            raise ValueError('BEHAVIOR requires checked external data and writable appdata bindings')
        if kind != 'software_archive' and not IMAGE.fullmatch(native['image']):
            raise ValueError('Native build image must be pinned by digest')
        provenance = {'config_sha256': document['config_source']['sha256'],
                      'registry_sha256': document['registry_source']['sha256'],
                      'release_input_sha256': digest(path), 'qualification': document['qualification'],
                      'native_input': native}
        return cls(environment, seconds, config, case, native, tuple(document['artifacts']), provenance, path)
