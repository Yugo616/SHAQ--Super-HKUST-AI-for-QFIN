from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / 'packaging/public_base_update_acceptance.py'
    if not path.is_file():
        raise AssertionError('Windows final delivery must apply the public base through the final delta feed')
    spec = importlib.util.spec_from_file_location('public_base_update_acceptance', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PublicBaseUpdateAcceptanceTests(unittest.TestCase):
    def fixture(self, directory):
        feed = Path(directory)
        package_id = 'SHAQDailyOracleLab'
        channel = 'win-x64-stable'
        rows = []
        for version, kind, data in [('0.7.0', 'Full', b'public base'),
                                    ('0.7.1', 'Full', b'final full'),
                                    ('0.7.1', 'Delta', b'actual delta')]:
            filename = f'{package_id}-{version}-{channel}-{kind.lower()}.nupkg'
            (feed / filename).write_bytes(data)
            rows.append(dict(PackageId=package_id, Version=version, Type=kind,
                             FileName=filename, Size=len(data), SHA256=hashlib.sha256(data).hexdigest()))
        (feed / f'releases.{channel}.json').write_text(json.dumps({'Assets': rows}))
        receipt = dict(status='public-base', channel=channel, base_version='0.7.0',
                       target_version='0.7.1', filename=rows[0]['FileName'],
                       sha256=rows[0]['SHA256'], size=rows[0]['Size'],
                       repository='example/repo',
                       installer_source_url='https://github.com/example/repo/releases/download/lab-v0.7.0-windows/SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe',
                       installer_sha256=hashlib.sha256(b'public setup').hexdigest(), installer_size=12)
        (feed / f'delta-base.{channel}.json').write_text(json.dumps(receipt))
        return feed, receipt

    def test_identity_requires_exact_public_base_target_and_delta(self):
        acceptance = module()
        with tempfile.TemporaryDirectory() as directory:
            feed, receipt = self.fixture(directory)
            identity = acceptance.validate_transition(ROOT, feed, '0.7.1')
            self.assertEqual(identity['base_version'], '0.7.0')
            self.assertEqual(identity['target_version'], '0.7.1')
            self.assertEqual(identity['base_sha256'], receipt['sha256'])
            self.assertEqual(identity['installer_sha256'], receipt['installer_sha256'])
            self.assertEqual(identity['delta_filename'], 'SHAQDailyOracleLab-0.7.1-win-x64-stable-delta.nupkg')
            self.assertEqual(identity['delta_sha256'], hashlib.sha256(b'actual delta').hexdigest())

    def test_first_release_or_missing_actual_delta_is_not_partial_update_evidence(self):
        acceptance = module()
        with tempfile.TemporaryDirectory() as directory:
            feed, _ = self.fixture(directory)
            receipt_path = feed / 'delta-base.win-x64-stable.json'
            receipt = json.loads(receipt_path.read_text())
            receipt['status'] = 'first-managed-release'
            receipt_path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, 'public base'):
                acceptance.validate_transition(ROOT, feed, '0.7.1')
            receipt['status'] = 'public-base'
            receipt_path.write_text(json.dumps(receipt))
            manifest = feed / 'releases.win-x64-stable.json'
            value = json.loads(manifest.read_text())
            value['Assets'] = [row for row in value['Assets'] if row['Type'] != 'Delta']
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'delta'):
                acceptance.validate_transition(ROOT, feed, '0.7.1')

    def test_native_event_identity_reads_delta_only_from_bridge_result(self):
        acceptance = module()
        identity = {'base_version': '0.7.0', 'target_version': '0.7.1'}
        # These keys mirror the retained installed-update event reports. The
        # target report intentionally has no invented delta_count field.
        bridge = {'status': 'applying', 'stage': 'bridge', 'actual_version': '0.7.0',
                  'native_target': '0.7.1', 'delta_count': 1, 'download_verified': True,
                  'preserved_hashes': True}
        target = {'status': 'passed', 'stage': 'target', 'actual_version': '0.7.1',
                  'preserved_hashes': True, 'remained_open_after_health': True,
                  'old_processes_before_data_access': [{'running': False}, {'running': False}]}
        peer = {'status': 'passed', 'stage': 'peer', 'actual_version': '0.7.0',
                'preserved_hashes': True, 'cooperatively_closed': True}
        replay = {'status': 'passed', 'stage': 'replay', 'actual_version': '0.7.1',
                  'preserved_hashes': True}
        accepted = acceptance.validate_update_events(identity, bridge, target, peer, replay)
        self.assertEqual(accepted['delta_count'], 1)
        self.assertNotIn('delta_count', target)
        with self.assertRaisesRegex(ValueError, 'delta'):
            acceptance.validate_update_events(identity, {**bridge, 'delta_count': 0}, target, peer, replay)


if __name__ == '__main__':
    unittest.main()
