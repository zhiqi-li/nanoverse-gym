"""Run an immutable packaged case using the existing Gym collector entrypoint."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from environment_servers.nanoverse_sdk.native import CaseRegistry
from portable.behavior_data import validate_data
from portable.native_admission import verify_admission
from portable.native_host import paired_inventory
from portable.gym_version import require_gym_release
from environment_servers.nanoverse_sdk.models import PROFILES

ROOT = Path('/opt/nvgym')


def verify_release(provenance):
    inventory = ROOT / 'deployment/release-files.json'
    if hashlib.sha256(inventory.read_bytes()).hexdigest() != provenance['release_files_sha256']:
        raise ValueError('Release inventory hash mismatch')
    for name, expected in json.loads(inventory.read_text()).items():
        path = ROOT / name
        if 'link' in expected:
            if not path.is_symlink() or str(path.readlink()) != expected['link']:
                raise ValueError(f'Release symlink mismatch: {name}')
        else:
            value = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1 << 20), b''):
                    value.update(block)
            if value.hexdigest() != expected['sha256']:
                raise ValueError(f'Release file hash mismatch: {name}')
    paired_inventory(provenance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('environment')
    parser.add_argument('--output-dir', type=Path, default=Path('/output'))
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--credentials-file', type=Path, default=Path('/run/secrets/model.env'))
    parser.add_argument('--model-profile', choices=PROFILES, default='gpt-6.1-sol')
    args = parser.parse_args()
    metadata = json.loads((ROOT / 'deployment/runtime.json').read_text())
    if metadata['environment'] != args.environment:
        raise ValueError('Selected environment does not match the packaged image')
    output = args.output_dir.resolve(strict=True)
    if (output / 'summary.json').exists() or (output / 'episode').exists():
        raise ValueError('Refusing to reuse an evaluation output directory')
    ownership = output.stat()
    report = {'environment': args.environment, 'status': 'failed', 'image_provenance': metadata['provenance']}
    report_path = output / 'summary.json'
    process = None
    receipt = None
    temporary = tempfile.TemporaryDirectory(prefix='nvgym-')

    def stop(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        report['gym_release'] = require_gym_release()
        report['isolation'] = metadata.get('isolation', 'trusted')
        ttc_manifest = json.loads((ROOT / 'deployment/ttc-manifest.json').read_text())
        if ttc_manifest.get('unsafe_no_host_isolation') is not (report['isolation'] != 'strict'):
            raise ValueError('TTC isolation manifest differs from the prepared runtime')
        profile = PROFILES[args.model_profile]
        report['model_profile'] = profile.name
        report['model'] = profile.model
        verify_release(metadata['provenance'])
        admission = ROOT / 'deployment/native-admission.json'
        if hashlib.sha256(admission.read_bytes()).hexdigest() != metadata['provenance']['native_admission_sha256']:
            raise ValueError('Native admission receipt hash mismatch')
        receipt = json.loads(admission.read_text())
        report['initial_identities'] = verify_admission(metadata, receipt, restore_prefix=True)
        if metadata['provenance']['release_input']['native_input']['kind'] == 'software_archive':
            report['native_authority'] = metadata['native_authority']
        work = Path(temporary.name)
        config = metadata['config']
        if args.environment in ('robodojo', 'roboforge') and ownership.st_uid:
            config['kit_trusted_owners'] = [ownership.st_uid]
        if args.environment == 'behavior1k':
            contract = metadata['external_data']
            if json.loads(admission.read_text())['validation']['external_data_owner'] != contract:
                raise ValueError('BEHAVIOR data contract differs from admitted native owner')
            data_receipt = validate_data(args.data_root, contract)
            data = args.data_root.resolve(strict=True)
            config['native']['data'] = str(data)
            config['native']['appdata'] = str(output / 'behavior-appdata')
            data_receipt.update(task=config['native']['task'], instance=config['native']['instance'])
            report['external_data'] = data_receipt
        elif args.data_root is not None:
            if args.environment != 'robodojo':
                raise ValueError('A separate data root is supported only for RoboDojo and BEHAVIOR')
            data = args.data_root.resolve(strict=True)
            robot = data / 'Assets/Robots/x5/curobo_tmp.yml'
            if not robot.is_file():
                raise ValueError('Data root must contain Assets/Robots/x5/curobo_tmp.yml')
            config.update(assets=str(data), home_robot_config=str(robot))
        config_path = work / 'config.json'
        config_path.write_text(json.dumps(config))
        case = {**metadata['case'], 'config_path': str(config_path)}
        registry_path = work / 'registry.json'
        registry_path.write_text(json.dumps({'cases': [case]}))
        registry = CaseRegistry.load(registry_path)
        registry.validate_deployment(ROOT / 'code/nanoverse', ROOT / 'code/ember_bridge')
        command = [sys.executable, '-m', 'scripts.sdk_smoke',
                   '--registry', str(registry_path), '--case-id', case['case_id'],
                   '--output-dir', str(output / 'episode'),
                   '--credentials-file', str(args.credentials_file.resolve(strict=True)),
                   '--native-root', str(ROOT / 'code/nanoverse'),
                   '--bridge-root', str(ROOT / 'code/ember_bridge'),
                   '--model', profile.model,
                   '--episode-seconds', str(metadata['episode_seconds'])]
        process = subprocess.Popen(command, env=dict(os.environ))
        code = process.wait(timeout=metadata['episode_seconds'] + 180)
        report['exit_code'] = code
        if code:
            raise RuntimeError(f'Gym collection failed with exit code {code}')
        rows = [json.loads(line) for line in (output / 'episode/rollouts.jsonl').read_text().splitlines() if line]
        if len(rows) != 1:
            raise ValueError('Expected exactly one collected result')
        row = rows[0]
        receipts = list((output / 'episode/n').glob('*/cleanup.json'))
        guests = list((output / 'episode/n').glob('*/n/actor/ttc-runtime.json'))
        report['native_cleanup_confirmed'] = bool(receipts and guests) and all(
            json.loads(path.read_text()).get('complete') is True for path in receipts
        ) and all(json.loads(path.read_text()).get('state') == 'closed' for path in guests)
        if not report['native_cleanup_confirmed']:
            raise ValueError('Native process and TTC cleanup were not confirmed')
        report['kind'] = row.get('kind')
        if 'reward' in row:
            report.update(status='scored', reward=row['reward'])
        elif row.get('kind') == 'fixture':
            report['status'] = 'fixture'
        else:
            raise ValueError('Collector produced no score or fixture completion')
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        final_error = None
        try:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    report['forced_collector_stop'] = True
                    raise RuntimeError('Collector required a forced stop; native cleanup is unconfirmed')
        except Exception as error:
            report['status'] = 'failed'
            report['cleanup_error'] = str(error)
            final_error = error
        if process is not None and 'native_cleanup_confirmed' not in report:
            try:
                receipts = list((output / 'episode/n').glob('*/cleanup.json'))
                guests = list((output / 'episode/n').glob('*/n/actor/ttc-runtime.json'))
                report['native_cleanup_confirmed'] = bool(receipts and guests) and all(
                    json.loads(path.read_text()).get('complete') is True for path in receipts
                ) and all(json.loads(path.read_text()).get('state') == 'closed' for path in guests)
            except Exception as error:
                report['native_cleanup_confirmed'] = False
                report['cleanup_receipt_error'] = str(error)
        verification_errors = []
        try:
            verify_release(metadata['provenance'])
        except Exception as error:
            verification_errors.append(str(error))
        if receipt is not None:
            try:
                admission = ROOT / 'deployment/native-admission.json'
                expected = metadata['provenance']['native_admission_sha256']
                if hashlib.sha256(admission.read_bytes()).hexdigest() != expected:
                    raise ValueError('Native admission receipt changed during collection')
            except Exception as error:
                verification_errors.append(str(error))
            try:
                report['final_identities'] = verify_admission(metadata, receipt)
            except Exception as error:
                verification_errors.append(str(error))
        if verification_errors:
            report['status'] = 'failed'
            report['final_verification_error'] = '; '.join(verification_errors)
            final_error = ValueError(report['final_verification_error'])
        temporary.cleanup()
        if report['status'] != 'scored':
            report.pop('reward', None)
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        for parent, directories, files in os.walk(output, followlinks=False):
            for name in (*directories, *files):
                os.chown(Path(parent) / name, ownership.st_uid, ownership.st_gid, follow_symlinks=False)
        if final_error is not None:
            raise RuntimeError('Post-cleanup immutable release verification failed') from final_error


if __name__ == '__main__':
    main()
