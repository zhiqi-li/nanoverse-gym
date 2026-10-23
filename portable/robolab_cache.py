"""Keep Kit's mutable runtime files outside the sealed RoboLab installation."""

import json
from pathlib import Path

from portable.native_inventory import digest

LAUNCHER = Path('native-venv/lib/python3.11/site-packages/isaaclab/source/isaaclab/'
                'isaaclab/app/app_launcher.py')
LAUNCHER_SHA256 = 'b559d9cec89193eecb8263655335445a301996bbd52f2c45ad3bcfb41bd83c4d'


def configure_kit(root):
    path = root / LAUNCHER
    before = digest(path)
    if before != LAUNCHER_SHA256:
        raise ValueError('Pinned RoboLab AppLauncher differs')
    old = '''        self._kit_args = []
        if "kit_args" in launcher_args:
            self._kit_args = [arg for arg in launcher_args["kit_args"].split()]
            sys.argv += self._kit_args
'''
    new = '''        self._kit_args = []
        if "kit_args" in launcher_args:
            self._kit_args = [arg for arg in launcher_args["kit_args"].split()]
        self._kit_args += ["--portable-root", os.path.join(
            os.environ.get("HOME", "/tmp"), ".cache", "nvgym-robolab-kit")]
        sys.argv += self._kit_args
'''
    text = path.read_text()
    if text.count(old) != 1 or 'import os\n' not in text:
        raise ValueError('Pinned RoboLab Kit argument handling differs')
    path.write_text(text.replace(old, new))
    return {'path': str(LAUNCHER), 'before_sha256': before,
            'after_sha256': digest(path),
            'configuration': '--portable-root $HOME/.cache/nvgym-robolab-kit',
            'reference': 'https://docs.omniverse.nvidia.com/kit/docs/carbonite/latest/docs/Kernel/CommandLineOptions.html'}


def public_recipe(recipe):
    dependencies = recipe.dependencies
    recipe.SPEC = {**recipe.SPEC, 'kit_cache_owner_sha256': digest(__file__)}

    def install_dependencies(ctx):
        dependencies(ctx)
        record = configure_kit(ctx.root)
        (ctx.root / 'robolab-kit-profile.json').write_text(json.dumps(record, indent=2) + '\n')

    recipe.dependencies = install_dependencies
    return recipe
