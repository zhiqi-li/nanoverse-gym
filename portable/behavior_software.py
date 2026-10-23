"""Install BEHAVIOR software from upstream; licensed data is admitted at runtime."""

import ctypes
import os
from pathlib import Path
import shutil

from portable.native_inventory import digest

SDK_CONSTRAINTS = '''numpy==1.26.0
pillow==11.3.0
websockets==12.0
packaging==23.0
typing_extensions==4.12.2
click==8.1.7
psutil==5.9.8
ipython==8.37.0
huggingface-hub==0.36.0
pydantic==2.10.6
warp-lang==1.12.0
av==16.0.1
'''


def apply_sdk_profile(source):
    """Make unused policy networking and dataset conversion optional upstream extras."""
    setup = source / 'OmniGibson/setup.py'
    policies = source / 'OmniGibson/omnigibson/eval/policies.py'
    envs = source / 'OmniGibson/omnigibson/envs/__init__.py'
    replacements = {
        source / 'setup.sh': [
            ('    python -m pip install --force-reinstall "websockets>=15.0.1"',
             '    python -m pip install --force-reinstall "websockets==12.0"'),
            ("    # Force reinstall websockets >= 15.0.1 because it's been overwritten by Isaac Sim with an older version",
             "    # SDK profile keeps Isaac Sim's websocket runtime; remote policy support is optional."),
        ],
        setup: [
            ('    version="3.9.3",', '    version="3.9.3+nvgym.sdk1",'),
            ('        "pillow~=11.0.0",', '        "pillow==11.3.0",'),
            ('        "websockets>=15.0.1",\n', ''),
            ('        "lerobot[dataset] @ git+https://github.com/wensi-ai/lerobot@release/b1k",\n', ''),
            ('    extras_require={\n', '    extras_require={\n'
             '        "remote-policy": ["websockets>=15.0.1"],\n'
             '        "dataset-tools": ["lerobot[dataset] @ git+https://github.com/wensi-ai/lerobot@release/b1k"],\n'),
        ],
        policies: [
            ('from omnigibson.eval.utils.network_utils import WebsocketClientPolicy\n', ''),
            ('        logging.info(f"Creating websocket client policy with host: {host}, port: {port}")',
             '        from omnigibson.eval.utils.network_utils import WebsocketClientPolicy\n'
             '        logging.info(f"Creating websocket client policy with host: {host}, port: {port}")'),
            ('    def update_host(self, host: str, port: int) -> None:\n',
             '    def update_host(self, host: str, port: int) -> None:\n'
             '        from omnigibson.eval.utils.network_utils import WebsocketClientPolicy\n'),
        ],
        envs: [
            ('from omnigibson.envs.lerobot_data_wrapper import LeRobotDataWrapper, LeRobotPlaybackWrapper\n',
             'def __getattr__(name):\n'
             '    if name in ("LeRobotDataWrapper", "LeRobotPlaybackWrapper"):\n'
             '        from omnigibson.envs.lerobot_data_wrapper import LeRobotDataWrapper, LeRobotPlaybackWrapper\n'
             '        return {"LeRobotDataWrapper": LeRobotDataWrapper,\n'
             '                "LeRobotPlaybackWrapper": LeRobotPlaybackWrapper}[name]\n'
             '    raise AttributeError(name)\n\n'),
        ],
    }
    records = {}
    for path, edits in replacements.items():
        before = digest(path)
        text = path.read_text()
        for old, new in edits:
            if text.count(old) != 1:
                raise ValueError('Unexpected pinned BEHAVIOR SDK profile patch input: ' + str(path))
            text = text.replace(old, new)
        path.write_text(text)
        records[str(path.relative_to(source))] = {'before_sha256': before, 'after_sha256': digest(path)}
    return records


def public_recipe(recipe):
    dependencies = recipe.dependencies
    native_source = recipe.source
    recipe.SPEC = {**recipe.SPEC,
                   'scope': 'official-source software only; no dataset or native reset qualification',
                   'sdk_profile_owner_sha256': digest(__file__),
                   'sdk_profile': 'LocalPolicy SDK only; upstream remote-policy and dataset-tools are optional',
                   'warp_lang': '1.12.0', 'data': 'external read-only /data; admitted at evaluation'}

    def preflight(ctx):
        if os.environ.get('OMNI_KIT_ACCEPT_EULA') != 'YES':
            raise ValueError('BEHAVIOR software requires explicit Isaac Sim terms acceptance')
        for name in recipe.LIBRARIES:
            ctypes.CDLL(name)
        ctx.require_commands('git', 'gcc', 'nvidia-smi')
        if ctx.options:
            raise ValueError('Software preparation does not accept licensed data resources')

    def install_dependencies(ctx):
        constraints = ctx.root / 'sdk-constraints.txt'
        constraints.write_text(SDK_CONSTRAINTS)
        previous = os.environ.get('PIP_CONSTRAINT')
        os.environ['PIP_CONSTRAINT'] = str(constraints)
        try:
            dependencies(ctx)
            ctx.pip('warp-lang==1.12.0', 'av==16.0.1')
        finally:
            if previous is None:
                os.environ.pop('PIP_CONSTRAINT', None)
            else:
                os.environ['PIP_CONSTRAINT'] = previous
        apps = Path(ctx.python).parent.parent / 'lib/python3.11/site-packages/isaacsim/apps'
        for relative in ('OmniGibson/omnigibson/omnigibson_5_1_0.kit',
                         'docs/assets/OmniGibson_logo.png'):
            shutil.copyfile(ctx.source / relative, apps / Path(relative).name)
        # Initialize the search roots declared by the pinned Kit app and SimulationApp.
        for name in ('exts', 'extscache', 'extsPhysics', 'extsUser', 'extsDeprecated',
                     'kit/exts', 'kit/extsPhysics'):
            directory = apps.parent / name
            if directory.is_symlink():
                raise ValueError('Unexpected Kit extension search-root symlink')
            directory.mkdir(mode=0o755, exist_ok=True)
            directory.chmod(0o755)

    def source(ctx):
        import json
        native_source(ctx)
        patch = apply_sdk_profile(ctx.source)
        (ctx.root / 'sdk-profile-patch.json').write_text(json.dumps(patch, indent=2) + '\n')
        ctx.record_sources([ctx.source])

    def assets(ctx):
        ctx.record_assets([ctx.source / 'OmniGibson/omnigibson/omnigibson_5_1_0.kit',
                           ctx.source / 'docs/assets/OmniGibson_logo.png'])

    def check(ctx):
        ctx.run(ctx.python, '-c', '''
import importlib.metadata as m
versions = {name: m.version(name) for name in ('omnigibson', 'isaacsim', 'bddl', 'joylo', 'torch', 'warp-lang')}
assert versions['isaacsim'].startswith('5.1.0'), versions
assert versions['warp-lang'] == '1.12.0', versions
import nanoverse.sdk.host, behavior1k_env.native, av
from behavior1k_env import cells
print(versions, len(cells.TASKS), 'software only; runtime data and native evaluation required')
''')
        ctx.run(ctx.python, '-c', '''
import runpy,sys,torch
policy=runpy.run_path(sys.argv[1])['LocalPolicy'](action_dim=23)
value=policy.forward({'observation':torch.ones(1,4)})
assert value.shape==(1,23) and torch.count_nonzero(value)==0
assert 'omnigibson.eval.utils.network_utils' not in sys.modules
print('LocalPolicy import/control shape qualified without the optional network policy')
''', ctx.source / 'OmniGibson/omnigibson/eval/policies.py')

    def verify(ctx):
        # No simulator/data is available in the software image preparation step.
        # Native evaluation is mandatory in the separate real-model qualification.
        validate_layout(ctx.root)

    recipe.preflight = preflight
    recipe.source = source
    recipe.dependencies = install_dependencies
    recipe.assets = assets
    recipe.check = check
    recipe.verify = verify
    native_config = recipe.native_config
    recipe.native_config = lambda ctx: {**native_config(ctx), 'data': '/data',
                                        'scope': 'software only; external data required at evaluation'}
    return recipe


def validate_layout(root):
    excluded = {'omnigibson.key', '.behavior1k-datasets', 'behavior-1k-assets',
                'omnigibson-robot-assets', '2026-challenge-task-instances'}
    if (root / 'datasets').exists() or (root / 'datasets').is_symlink():
        raise ValueError('Licensed data cannot be bundled in the software payload')
    for path in root.rglob('*'):
        if path.name in excluded:
            raise ValueError('Licensed data cannot be bundled in the software payload')
