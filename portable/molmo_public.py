"""Prepare the accepted classic-renderer MolmoSpaces case from public sources."""

import ctypes
import os
import shutil
from pathlib import Path

from portable.native_inventory import digest, inventory

PARTS = {'assets', 'cache', 'nltk_data'}
ARCHIVE = 'molmospaces-assets.tar.zst'
ARCHIVE_SHA256 = '09916c2c8b0f00fb814f094125c5b3561a2168df89a07b1ac349b73771b657b9'
ARCHIVE_BYTES = 8406823113


def cache_identity(cache):
    """Admit data only: a portable upstream cache or the accepted data archive."""
    cache = Path(cache).resolve(strict=True)
    names = {p.name for p in cache.iterdir()}
    if names == {ARCHIVE}:
        path = cache / ARCHIVE
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != ARCHIVE_BYTES
                or digest(path) != ARCHIVE_SHA256):
            raise ValueError('MolmoSpaces data archive size or SHA256 differs')
        return {'kind': 'accepted_data_archive', 'sha256': ARCHIVE_SHA256}
    if names != PARTS or not all((cache / name).is_dir() and not (cache / name).is_symlink()
                                 for name in PARTS):
        raise ValueError('MolmoSpaces data cache must contain only assets, cache, nltk_data')
    for path in cache.rglob('*'):
        if path.is_symlink() and (os.path.isabs(os.readlink(path))
                                 or not path.resolve(strict=True).is_relative_to(cache)):
            raise ValueError('MolmoSpaces cache link must be relative and stay inside the data cache')
    _, record = inventory(cache, directory_modes=True, protected=False)
    return {'kind': 'portable_data_cache', **record}


def copy_cache(recipe, cache, target, expected):
    observed = cache_identity(cache)
    if observed != expected:
        raise ValueError('MolmoSpaces input cache changed before copying')
    if observed['kind'] == 'accepted_data_archive':
        declared = recipe.data()
        if declared['resource']['sha256'] != ARCHIVE_SHA256:
            raise ValueError('Accepted MolmoSpaces data declaration differs')
        files = recipe.extract_data(Path(cache) / ARCHIVE, target, declared['fixed_root'])
        if (len(files) != declared['regular_files']
                or sum(p.is_symlink() for p in target.rglob('*')) != declared['symlinks']
                or any(p.is_symlink() and not p.exists() for p in target.rglob('*'))):
            raise ValueError('MolmoSpaces data archive layout differs')
    else:
        shutil.copytree(cache, target, symlinks=True)
        if cache_identity(target) != expected:
            raise ValueError('Copied MolmoSpaces cache differs from its input')
        files = [p for p in target.rglob('*') if p.is_file() and not p.is_symlink()]
    return files


def public_recipe(recipe, assets_cache=None):
    cached = cache_identity(assets_cache) if assets_cache is not None else None
    expected = os.environ.get('NVGYM_ASSETS_CACHE_SHA256')
    if expected is not None and (cached is None or cached['sha256'] != expected):
        raise ValueError('MolmoSpaces cache differs from its prepared-image input identity')
    versions = '\n'.join(line.split()[0] for line in recipe.REQUIREMENTS.read_text().splitlines()
                          if line and not line.startswith('#')) + '\n'
    import hashlib
    recipe.SPEC = {key: value for key, value in recipe.SPEC.items()
                   if key not in ('wheels_sha256', 'requirements_sha256', 'data_sha256',
                                  'filament_wheel_sha256')}
    recipe.SPEC.update(source_url='https://github.com/allenai/molmospaces.git',
                       public_versions_sha256=hashlib.sha256(versions.encode()).hexdigest(),
                       data_pins=dict(recipe.cells.DATA_PINS),
                       data_origin=({'operator_data_cache': cached} if cached else
                                    {'upstream_storage': 'official R2 through ResourceManager'}),
                       scope='accepted close_v1-890 case; classic MuJoCo renderer; '
                             'public version-pinned software; original data versions; offline evaluation')
    recipe.SNAPSHOT_PYTHONS = ('venv/bin/python',)

    def preflight(ctx):
        ctx.require_commands('git', 'zstd')
        for name in recipe.EGL_LIBRARIES:
            ctypes.CDLL(name)

    def source(ctx):
        ctx.clone(recipe.SPEC['source_url'], recipe.cells.COMMIT, ctx.source)
        tree = ctx.run('git', '-C', ctx.source, 'rev-parse', 'HEAD^{tree}', capture=True).strip()
        if tree != recipe.cells.TREE:
            raise ValueError('Public MolmoSpaces source tree differs from the accepted source')
        (ctx.source / '.git/info/exclude').write_text('/.molmospaces-data/\n')
        ctx.record_sources([ctx.source])

    def dependencies(ctx):
        lock = ctx.root / 'public-versions.txt'
        lock.write_text(versions)
        ctx.pip('--extra-index-url', 'https://download.pytorch.org/whl/cpu',
                '--report', ctx.root / 'public-downloads.json', '-r', lock)
        ctx.pip('--no-deps', ctx.wheel)
        site = ctx.run(ctx.python, '-I', '-c',
                       'import sysconfig;print(sysconfig.get_paths()["purelib"])', capture=True).strip()
        (Path(site) / 'molmospaces_source.pth').write_text(str(ctx.source) + '\n')

    def assets(ctx):
        data = ctx.source / recipe.DATA_DIRECTORY
        if cached is not None:
            files = copy_cache(recipe, assets_cache, data, cached)
        else:
            data.mkdir()
            ctx.run(ctx.python, '-c', WARM_DATA, str(data), timeout=7200)
            files = [p for p in data.rglob('*') if p.is_file() and not p.is_symlink()]
        ctx.record_assets(files)

    def check(ctx):
        ctx.run('env', 'PYTHONHASHSEED=0', 'MUJOCO_GL=egl', 'PYOPENGL_PLATFORM=egl',
                'MUJOCO_EGL_DEVICE_ID=0', ctx.python, '-c', CHECK_DATA,
                str(ctx.source), str(ctx.source / recipe.DATA_DIRECTORY))

    def verify(ctx):
        ctx.run(ctx.python, '-m', 'molmospaces_env.qualify', '--root', ctx.source,
                '--cells', 'close_v1-890', '--output', ctx.attempt / 'qualification.json', timeout=1800)

    def native_config(ctx):
        return {'python': str(ctx.python), 'root': str(ctx.source),
                'data': str(ctx.source / recipe.DATA_DIRECTORY), 'benchmark': 'close_v1', 'index': 890,
                'commit': recipe.cells.COMMIT, 'tree': recipe.cells.TREE,
                'scope': recipe.SPEC['scope']}

    recipe.preflight = preflight
    recipe.source = source
    recipe.dependencies = dependencies
    recipe.assets = assets
    recipe.check = check
    recipe.verify = verify
    recipe.native_config = native_config
    return recipe


WARM_DATA = '''
import os,sys,tempfile
from pathlib import Path
from molmospaces_env.scene import data_environment,benchmark_directory,make_eval_config
from molmospaces_env import cells
data=Path(sys.argv[1])
os.environ.update(data_environment(data))
import nltk
for name in ('wordnet','wordnet2022'):
    if not nltk.download(name,download_dir=str(data/'nltk_data'),quiet=True,raise_on_error=True):
        raise RuntimeError('Official NLTK resource download failed: '+name)
import molmo_spaces.molmo_spaces_constants as constants
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
OriginalSession=requests.sessions.Session
class DownloadSession(OriginalSession):
    def __init__(self):
        super().__init__()
        retry=Retry(total=8,backoff_factor=5,backoff_max=90,
                    status_forcelist=(429,500,502,503,504),allowed_methods=('GET','HEAD'),
                    respect_retry_after_header=True)
        self.mount('https://',HTTPAdapter(max_retries=retry))
requests.sessions.Session=requests.Session=DownloadSession
constants.get_resource_manager()
from molmo_spaces.evaluation.benchmark_schema import load_all_episodes
from molmo_spaces.tasks.json_eval_task_sampler import JsonEvalTaskSampler
from molmo_spaces.utils.lazy_loading_utils import install_scene_with_objects_and_grasps_from_path
benchmark=cells.benchmark('close_v1')
episodes=load_all_episodes(benchmark_directory(constants.ASSETS_DIR,benchmark))
assert len(episodes)==benchmark.episodes
with tempfile.TemporaryDirectory(prefix='molmo-public-warm-') as temporary:
    episode=episodes[890]
    sampler=JsonEvalTaskSampler(make_eval_config(episode,Path(temporary)),episode)
    install_scene_with_objects_and_grasps_from_path(sampler._current_house_scene_path())
print('Official upstream asset closure downloaded for close_v1-890; offline qualification follows')
'''

CHECK_DATA = '''
import os,sys
from pathlib import Path
from molmospaces_env import network
network.install()
from molmospaces_env.scene import data_environment,MolmoSpacesScene
os.environ.update(data_environment(Path(sys.argv[2])))
scene=MolmoSpacesScene('close_v1',890,root=Path(sys.argv[1]))
try:
    scene.reset()
    images=scene.images()
    assert images and all(image.std()>0 for image in images.values())
finally:
    scene.close()
assert not network.blocked(),network.blocked()
print('Accepted classic MolmoSpaces case constructed and rendered offline')
'''


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Validate a MolmoSpaces data-only preparation cache')
    parser.add_argument('cache', type=Path)
    args = parser.parse_args()
    print(cache_identity(args.cache)['sha256'])
