"""Release-boundary checks use tiny real files and only fake the GitHub API."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = 'a' * 40
VALIDATION = 'b' * 40
REPO = 'fixture/SHAQ'
LAYOUT = hashlib.sha256(b'reviewed layout fixture').hexdigest()
GEOMETRY = hashlib.sha256(b'unchanged geometry fixture').hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def source_fixture(platform='macOS-Intel', external=True):
    head = VALIDATION if external else APP
    return {
        'repository': REPO,
        'run': {'id': 101, 'head_sha': head, 'status': 'completed', 'conclusion': 'failure',
                'path': '.github/workflows/build-desktop.yml',
                'repository': {'id': 17, 'full_name': REPO}},
        'job': {'id': 201, 'run_id': 101, 'name': f'native ({platform})',
                'status': 'completed', 'conclusion': 'success'},
        'artifact': {'id': 301, 'name': 'SHAQ-Daily-Oracle-Lab-' + platform,
                     'digest': 'sha256:' + 'c' * 64, 'expired': False,
                     'workflow_run': {'id': 101, 'head_sha': head,
                                      'repository_id': 17, 'head_repository_id': 17}},
        'validation_files_sha256': {'tests/native_layout_check.py': LAYOUT,
                                    'tests/native_geometry.py': GEOMETRY} if external else {},
    }


class StageVerifiedAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / 'artifact'
        self.dist = self.artifact / 'dist'
        self.source = source_fixture()
        self.external = {'application_sha': APP, 'validation_sha': VALIDATION,
                         'layout_sha256': LAYOUT, 'geometry_sha256': GEOMETRY,
                         'application_clean_before': True, 'application_clean_after': True}
        self.populate()

    def populate(self, platform='macOS-Intel', external=True):
        self.platform = platform
        windows = platform == 'Windows-x64'
        reports = self.dist / 'diagnostic' if windows else self.dist
        write_json(self.artifact / 'build/third-party/manifest.json',
                   {'source_sha': APP, 'source_dirty': False, 'source_version': '0.9.0'})
        layout = {'status': 'passed', 'checks': [1] * 16, 'geometry': [1, 2]}
        layout.update(application_sha=APP if external else None,
                      validation_sha=VALIDATION if external else None)
        write_json(self.dist / 'diagnostic/desktop-layout.json', layout)
        if external:
            write_json(self.dist / 'diagnostic/external-layout-source.json', self.external)
        write_json(self.dist / 'installed-update/acceptance.json', {'status': 'passed'})
        for name in ('installed-gui', 'installed-smoke', 'installed-native-audit', 'installed-worker-protocol'):
            write_json(reports / (name + '.json'), {'status': 'passed'})
        if windows:
            write_json(reports / 'windows-delivery.json',
                       {'status': 'passed', 'release_promoted': True,
                        'stages': [{'name': 'installed-audit', 'status': 'passed'}]})
        installer = ('SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe' if windows else
                     f'SHAQ-Daily-Oracle-Lab-{platform}.dmg')
        (self.dist / installer).write_bytes(b'small installer fixture')
        (self.dist / (installer + '.sha256')).write_text(hashlib.sha256(b'small installer fixture').hexdigest())
        feed = self.dist / 'update-feed'; feed.mkdir(exist_ok=True)
        assets = []
        for name, payload in [('base.nupkg', b'base'), ('target.nupkg', b'target'), ('delta.nupkg', b'delta')]:
            (feed / name).write_bytes(payload)
            assets.append({'FileName': name, 'SHA256': hashlib.sha256(payload).hexdigest(), 'Size': len(payload)})
        write_json(feed / 'releases.fixture.json', {'Assets': assets})
        write_json(feed / 'delta-base.fixture.json', {})
        write_json(reports / 'public-base-update.json', {
            'status': 'passed', 'identity': {'target_version': '0.9.0', 'base_version': '0.7.10',
                'channel': 'fixture', 'target_filename': 'target.nupkg',
                'target_sha256': hashlib.sha256(b'target').hexdigest(), 'delta_filename': 'delta.nupkg'},
            'target_full_requests': [], 'uninstalled': True,
            'target': {'preserved_hashes': True}, 'replay': {'status': 'passed'}})

    def stage(self, include_source=True):
        write_json(self.root / 'native-source.json', self.source)
        command = [sys.executable, str(ROOT / 'packaging/stage_verified_assets.py'),
                   '--artifact', str(self.artifact), '--output', str(self.root / 'public'),
                   '--platform', self.platform, '--version', '0.9.0', '--commit', APP,
                   '--job-url', f'https://github.com/{REPO}/actions/runs/101/job/201']
        if include_source:
            command += ['--native-source', str(self.root / 'native-source.json')]
        return subprocess.run(command, text=True, capture_output=True)

    def receipt(self):
        return json.loads((self.root / 'public' / f'Acceptance-{self.platform}.json').read_text())

    def assert_rejected(self, marker):
        result = self.stage()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(marker, result.stderr)
        self.assertFalse((self.root / 'public').exists(), 'Rejected source must not produce staged assets')

    def test_external_harness_requires_independent_source_metadata(self):
        result = self.stage(include_source=False)
        self.assertNotEqual(result.returncode, 0, 'Unbound external harness was accepted')
        self.assertIn('External layout:', result.stderr)

    def test_external_identity_is_bound_to_actual_failed_run_and_successful_job(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt()
        self.assertEqual(receipt['external_layout_harness'], {
            **self.external, 'source': 'GitHub commit content verified independently'})
        self.assertEqual(receipt['native_source']['run_conclusion'], 'failure')
        self.assertEqual(receipt['native_source']['job_conclusion'], 'success')
        self.assertEqual(receipt['native_source']['artifact_digest'], 'sha256:' + 'c' * 64)
        self.assertEqual(receipt['native_source']['run_head_sha'], VALIDATION)

    def test_harness_metadata_cannot_substitute_a_different_run_commit(self):
        self.source['run']['head_sha'] = 'd' * 40
        self.source['artifact']['workflow_run']['head_sha'] = 'd' * 40
        self.assert_rejected('External layout:')

    def test_harness_hashes_must_match_independent_commit_bytes(self):
        for field in ('layout_sha256', 'geometry_sha256'):
            with self.subTest(field=field):
                changed = dict(self.external, **{field: 'd' * 64})
                write_json(self.dist / 'diagnostic/external-layout-source.json', changed)
                self.assert_rejected('External layout:')

    def test_layout_report_must_name_the_same_validation_and_application(self):
        path = self.dist / 'diagnostic/desktop-layout.json'
        original = json.loads(path.read_text())
        for field in ('application_sha', 'validation_sha'):
            with self.subTest(field=field):
                write_json(path, dict(original, **{field: 'd' * 40}))
                self.assert_rejected('External layout:')

    def test_external_harness_requires_literal_clean_before_and_after(self):
        for field in ('application_clean_before', 'application_clean_after'):
            for bad in (False, 'true', 1, None):
                with self.subTest(field=field, value=bad):
                    write_json(self.dist / 'diagnostic/external-layout-source.json',
                               dict(self.external, **{field: bad}))
                    self.assert_rejected('External layout:')

    def test_failed_job_wrong_platform_and_wrong_artifact_are_rejected(self):
        changes = [('job', 'conclusion', 'failure'), ('job', 'name', 'native (Windows-x64)'),
                   ('job', 'run_id', 102), ('artifact', 'name', 'wrong artifact'),
                   ('artifact', 'expired', True), ('artifact', 'digest', 'not-a-digest')]
        original = copy.deepcopy(self.source)
        for section, field, value in changes:
            with self.subTest(section=section, field=field):
                self.source = copy.deepcopy(original)
                self.source[section][field] = value
                self.assert_rejected('Native source:')

    def test_missing_external_metadata_cannot_disguise_a_validation_branch(self):
        (self.dist / 'diagnostic/external-layout-source.json').unlink()
        self.assert_rejected('External layout:')

    def test_native_arm_and_windows_without_external_metadata_keep_existing_behavior(self):
        (self.dist / 'diagnostic/external-layout-source.json').unlink()
        for platform in ('macOS-Apple-Silicon', 'Windows-x64'):
            with self.subTest(platform=platform):
                self.populate(platform=platform, external=False)
                self.source = source_fixture(platform=platform, external=False)
                result = self.stage()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('external_layout_harness', self.receipt())
                self.assertEqual(self.receipt()['native_source']['run_head_sha'], APP)

    def test_existing_installed_acceptance_is_still_required(self):
        write_json(self.dist / 'installed-native-audit.json', {'status': 'failed'})
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Acceptance failed: installed-native-audit.json', result.stderr)

    def test_existing_installer_checksum_is_still_required(self):
        (self.dist / f'SHAQ-Daily-Oracle-Lab-{self.platform}.dmg').write_bytes(b'corrupt installer')
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Installer checksum mismatch', result.stderr)

if __name__ == '__main__':
    unittest.main()
