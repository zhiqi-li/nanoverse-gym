"""The supported public Gym release; commits are provenance, not admission rules."""

from importlib.metadata import distribution
from pathlib import Path


GYM_VERSION = Path(__file__).with_name('gym-requirements.txt').read_text().strip().removeprefix('nemo-gym==')


def require_gym_release():
    package = distribution('nemo-gym')
    installed = package.version
    if installed != GYM_VERSION:
        raise ValueError(f'Expected NeMo Gym {GYM_VERSION}, found {installed}')
    import nemo_gym
    module_path = Path(nemo_gym.__file__).resolve()
    if module_path != Path(package.locate_file('nemo_gym/__init__.py')).resolve():
        raise ValueError('Gym import is shadowed by a different source tree')
    return {'distribution': 'nemo-gym', 'version': installed, 'module_path': str(module_path)}
