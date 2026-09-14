from pathlib import Path
import hashlib
import functools
import http.server
import io
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


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
    def test_mac_public_installer_requires_matching_release_architecture_and_digest(self):
        acceptance = module()
        self.assertTrue(hasattr(acceptance, 'mac_installer_identity'))
        url = 'https://github.com/example/repo/releases/download/lab-v0.7.0-macos/'
        name = 'SHAQ-Daily-Oracle-Lab-macOS-Apple-Silicon.dmg'
        asset = {'name': name, 'browser_download_url': url + name, 'size': 123,
                 'digest': 'sha256:' + 'a' * 64}
        release = {'tag_name': 'lab-v0.7.0-macos', 'draft': False, 'assets': [asset]}
        valid = acceptance.mac_installer_identity(release, 'example/repo', '0.7.0', 'arm64')
        self.assertEqual(valid['installer_source_url'], url + name)
        for altered in [{**asset, 'digest': None}, {**asset, 'browser_download_url': 'https://other.test/app'},
                        {**asset, 'name': name.replace('Apple-Silicon', 'Intel')}]:
            with self.assertRaises(ValueError):
                acceptance.mac_installer_identity({**release, 'assets': [altered]}, 'example/repo', '0.7.0', 'arm64')

    def test_root_uses_system_temp_even_when_runner_temp_differs(self):
        acceptance = module()
        expected = Path(tempfile.gettempdir()) / 'shaq-installed-update-fixture'
        with patch.dict(os.environ, {'RUNNER_TEMP': str(expected / 'runner-temp')}), patch.object(
                acceptance.tempfile, 'mkdtemp', return_value=str(expected)) as created:
            root = acceptance.create_acceptance_root()
        self.assertEqual(root, expected.resolve())
        created.assert_called_once_with(prefix='shaq-installed-update-')

    def test_loopback_proxy_bypass_is_per_apply_and_replay_child(self):
        acceptance = module()
        cases = [
            ({'NO_PROXY': 'existing.test'}, {'existing.test'}),
            ({'NO_PROXY': 'upper.test', 'no_proxy': 'lower.test'}, {'upper.test', 'lower.test'}),
        ]
        for stage in ('public-base-apply', 'public-base-replay'):
            for environment, expected in cases:
                with self.subTest(stage=stage, environment=environment), patch.object(
                        acceptance.os, 'environ', environment), patch.object(
                        acceptance.subprocess, 'run', return_value=object()) as invoked:
                    before = dict(acceptance.os.environ)
                    acceptance.run_child(['app.exe'], io.StringIO(), 10,
                                         loopback=stage in acceptance.LOOPBACK_STAGES)
                    child = invoked.call_args.kwargs['env']
                    self.assertEqual(acceptance.os.environ, before)
                    self.assertEqual(child['NO_PROXY'], child['no_proxy'])
                    values = set(filter(None, child['NO_PROXY'].split(',')))
                    self.assertTrue(expected.issubset(values))
                    self.assertTrue({'127.0.0.1', 'localhost'}.issubset(values))

    def test_acceptance_server_serves_delta_but_rejects_target_full_fallback(self):
        acceptance = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delta = root / 'SHAQDailyOracleLab-0.7.1-win-x64-stable-delta.nupkg'
            full = root / 'SHAQDailyOracleLab-0.7.1-win-x64-stable-full.nupkg'
            manifest = root / 'releases.win-x64-stable.json'
            delta.write_bytes(b'actual delta')
            full.write_bytes(b'forbidden fallback')
            manifest.write_text('{"Assets":[]}', encoding='utf-8')
            blocked = []
            handler = functools.partial(acceptance.DeltaOnlyHandler, directory=str(root),
                                        blocked_filename=full.name, blocked_requests=blocked)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f'http://127.0.0.1:{server.server_port}/'
            try:
                self.assertEqual(opener.open(base + delta.name).read(), b'actual delta')
                self.assertEqual(opener.open(base + manifest.name).read(), b'{"Assets":[]}')
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(base + full.name)
                self.assertEqual(rejected.exception.code, 409)
                self.assertEqual(blocked, [full.name])
            finally:
                server.shutdown()
                server.server_close()

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

    def test_mac_transition_uses_its_architecture_and_checks_real_package_bytes(self):
        acceptance = module()
        for machine, channel in [('arm64', 'osx-arm64-stable'), ('x86_64', 'osx-x64-stable')]:
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                feed, _ = self.fixture(directory)
                for path in list(feed.iterdir()):
                    content = path.read_bytes()
                    if path.suffix == '.json':
                        content = content.replace(b'win-x64-stable', channel.encode())
                    renamed = path.with_name(path.name.replace('win-x64-stable', channel))
                    renamed.write_bytes(content)
                    path.unlink()
                identity = acceptance.validate_transition(ROOT, feed, '0.7.1', system='Darwin', machine=machine)
                self.assertEqual(identity['channel'], channel)
                (feed / identity['delta_filename']).write_bytes(b'corrupt')
                with self.assertRaises(ValueError):
                    acceptance.validate_transition(ROOT, feed, '0.7.1', system='Darwin', machine=machine)

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
