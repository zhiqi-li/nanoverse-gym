"""Check preparation argument handling without starting Docker or a model."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class PreparationLauncherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='nvgym-launcher-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = Path(__file__).resolve().parents[1]
        (self.root / 'portable').mkdir()
        shutil.copyfile(source / 'nvgym', self.root / 'nvgym')
        shutil.copyfile(source / 'portable/images.tsv', self.root / 'portable/images.tsv')
        tools = self.root / 'tools'
        tools.mkdir()
        docker = tools / 'docker'
        docker.write_text('''#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$1" in info) exit 0 ;; image) exit 1 ;; pull) exit 75 ;; esac
exit 99
''')
        docker.chmod(0o755)
        (self.root / 'portable/prepare.sh').write_text('''#!/bin/sh
printf '%s\n' "$@" > "$PREPARATION_ARGUMENTS"
printf '%s\n' sha256:prepared-candidate
''')
        self.calls = self.root / 'docker.calls'
        self.arguments = self.root / 'preparation.arguments'
        self.environment = {
            'PATH': str(tools) + ':/usr/bin:/bin', 'HOME': str(self.root),
            'LANG': 'C.UTF-8', 'DOCKER_CALLS': str(self.calls),
            'PREPARATION_ARGUMENTS': str(self.arguments),
        }

    def launch(self, credential, assets_cache=None):
        extra = ['--assets-cache', str(assets_cache)] if assets_cache is not None else []
        return subprocess.run(
            ['/bin/sh', str(self.root / 'nvgym'), 'run', 'robodojo', '--prepare',
             '--accept-isaac-terms', '--credentials', str(credential),
             '--output', str(self.root / 'output'), '--cache', str(self.root / 'cache'), *extra],
            env=self.environment, capture_output=True, text=True, timeout=15,
        )

    def test_unpublished_catalog_entry_can_prepare_before_image_resolution(self):
        credential = self.root / 'model.env'
        credential.write_text('synthetic-credential\n')
        result = self.launch(credential)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.arguments.read_text().splitlines(),
                         [str(self.root), '0', str(self.root / 'cache'), '', 'yes', '', 'robodojo'])
        self.assertEqual(self.calls.read_text().splitlines(),
                         ['info', 'image inspect sha256:prepared-candidate',
                          'pull sha256:prepared-candidate'])
        self.assertEqual(credential.read_text(), 'synthetic-credential\n')
        self.assertFalse((self.root / 'output').exists())

    def test_credentials_inside_copied_sources_are_rejected_before_preparation(self):
        for relative in ('portable', 'releases/robodojo', 'releases/libero', 'releases/behavior1k',
                         'releases/exemplar', 'releases/molmospaces', 'releases/robocasa365',
                         'releases/roboforge', 'releases/robolab', 'releases/robotwin'):
            with self.subTest(relative=relative):
                directory = self.root / relative
                directory.mkdir(parents=True, exist_ok=True)
                credential = directory / 'credentials.txt'
                credential.write_text('synthetic-credential\n')
                self.calls.unlink(missing_ok=True)
                result = self.launch(credential)
                self.assertEqual(result.returncode, 2)
                self.assertIn('Keep model credentials outside packaged source directories',
                              result.stderr)
                self.assertEqual(self.calls.read_text().splitlines(), ['info'])
                self.assertFalse(self.arguments.exists())
                self.assertFalse((self.root / 'output').exists())
                self.assertFalse((self.root / 'cache').exists())
                self.assertEqual(credential.read_text(), 'synthetic-credential\n')

    def test_operator_asset_cache_is_passed_to_preparation_without_credentials(self):
        cache = self.root / 'downloaded-data'
        (cache / 'Assets').mkdir(parents=True)
        credential = self.root / 'model.env'
        credential.write_text('synthetic-credential\n')
        result = self.launch(credential, cache)
        self.assertEqual(result.returncode, 75, result.stderr)
        arguments = self.arguments.read_text().splitlines()
        self.assertEqual(arguments, [str(self.root), '0', str(self.root / 'cache'), '', 'yes', str(cache), 'robodojo'])
        self.assertNotIn(str(credential), arguments)

    def test_credentials_inside_asset_cache_are_not_mounted_for_preparation(self):
        cache = self.root / 'downloaded-data'
        (cache / 'Assets').mkdir(parents=True)
        credential = cache / 'credentials.txt'
        credential.write_text('synthetic-credential\n')
        result = self.launch(credential, cache)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Keep model credentials outside the asset cache', result.stderr)
        self.assertFalse(self.arguments.exists())
        self.assertEqual(self.calls.read_text().splitlines(), ['info'])


if __name__ == '__main__':
    unittest.main()
