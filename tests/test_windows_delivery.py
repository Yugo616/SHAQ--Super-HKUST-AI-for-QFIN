from pathlib import Path
import importlib.util
import io
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class WindowsDeliveryTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[1] / 'packaging/windows_delivery.py'
        self.assertTrue(path.is_file(), 'Windows delivery must collect independent stage outcomes')
        spec = importlib.util.spec_from_file_location('windows_delivery', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_process_arguments_with_spaces_round_trip_and_failure_is_retained(self):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            argument = str(root / 'installation path with spaces')
            result = delivery.run_stage('arguments', [sys.executable, '-c',
                'import sys;print(sys.argv[1]);sys.exit(3)', argument], root, root, 10)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['exit_code'], 3)
            self.assertIn(argument, (root / 'arguments.log').read_text(encoding='utf-8'))

    def exercise(self, failed_stages, diagnostic, malformed_smoke=None, upstream_failed=False):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'project').mkdir()
            (root / 'project/pyproject.toml').write_text('[project]\nversion="0.6.0"\n', encoding='utf-8')
            source = Path(__file__).resolve().parents[1]
            shutil.copytree(source/'config', root/'project/config')
            (root/'project/packaging').mkdir()
            shutil.copy2(source/'packaging/updater-toolchain.json', root/'project/packaging/updater-toolchain.json')
            commands = []

            def process(args, **kwargs):
                # Only replace external Windows programs; real stage aggregation,
                # file prerequisites, report validation, cleanup and promotion run.
                stage = Path(kwargs['stdout'].name).stem
                commands.append(stage)
                if stage == 'compile-installer':
                    self.assertIn('--manage-existing', args)
                    self.assertIn('--prepare-public-base', args)
                if stage == 'public-base-update':
                    self.assertIn('--feed', args)
                    self.assertIn('--target-version', args)
                    self.assertIn('--output', args)
                if stage == 'install':
                    self.assertIn('--installto', args)
                    self.assertIn('--silent', args)
                if stage == 'uninstall':
                    self.assertEqual(Path(args[0]).name,'Update.exe')
                    self.assertIn('uninstall',args)
                if stage in ('installed-smoke','installed-gui'):
                    self.assertEqual(Path(args[0]).parent.name, 'current',
                                     'Wait for the real GUI executable, not the detached root launcher')
                workspace = root / 'fresh stage'
                if stage == 'build-app':
                    target = workspace / 'payload/SHAQ Daily Oracle Lab/SHAQ Daily Oracle Lab.exe'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'app')
                if stage == 'compile-installer':
                    target = workspace / 'installer/SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'installer')
                    base = dict(PackageId='SHAQDailyOracleLab', Version='0.5.0', Type='Full',
                                FileName='SHAQDailyOracleLab-0.5.0-win-x64-stable-full.nupkg',
                                Size=11, SHA256=hashlib.sha256(b'public base').hexdigest())
                    asset = dict(PackageId='SHAQDailyOracleLab', Version='0.6.0', Type='Full',
                                 FileName='SHAQDailyOracleLab-0.6.0-win-x64-stable-full.nupkg',
                                 Size=10, SHA256=hashlib.sha256(b'final full').hexdigest())
                    delta = dict(PackageId='SHAQDailyOracleLab', Version='0.6.0', Type='Delta',
                                 FileName='SHAQDailyOracleLab-0.6.0-win-x64-stable-delta.nupkg',
                                 Size=5, SHA256=hashlib.sha256(b'delta').hexdigest())
                    (target.parent/base['FileName']).write_bytes(b'public base')
                    (target.parent/asset['FileName']).write_bytes(b'final full')
                    (target.parent/delta['FileName']).write_bytes(b'delta')
                    (target.parent/'releases.win-x64-stable.json').write_text(json.dumps({'Assets':[base,asset,delta]}))
                    (target.parent/'delta-base.win-x64-stable.json').write_text(json.dumps(dict(
                        status='public-base', base_version='0.5.0', target_version='0.6.0',
                        channel='win-x64-stable', repository='example/repo', filename=base['FileName'], sha256=base['SHA256'],
                        size=base['Size'], installer_source_url='https://example.invalid/base-setup.exe',
                        installer_sha256='0'*64, installer_size=12)))
                    internal = root/'project/dist/installed-update'
                    internal.mkdir(parents=True, exist_ok=True)
                    (internal/'internal-acceptance-0.6.99-full.nupkg').write_bytes(b'not release')
                if stage == 'install':
                    (workspace / 'installed').mkdir()
                    (workspace / 'installed/current').mkdir()
                    (workspace / 'installed/current/SHAQ Daily Oracle Lab.exe').write_bytes(b'app')
                    (workspace / 'installed/SHAQ Daily Oracle Lab.exe').write_bytes(b'app')
                    (workspace / 'installed/Update.exe').write_bytes(b'uninstaller')
                if '--smoke-output' in args:
                    evidence = ({'status':'passed','checks':{'two_methods':True},'methods':[{},{}]}
                                if malformed_smoke is None else malformed_smoke)
                    Path(args[args.index('--smoke-output') + 1]).write_text(json.dumps(evidence), encoding='utf-8')
                if '--gui-smoke' in args:
                    Path(args[args.index('--gui-smoke') + 1]).write_text(json.dumps({'status':'passed','pages':['run','editor','history']}), encoding='utf-8')
                if '--output' in args and stage not in ('build-app','compile-installer'):
                    Path(args[args.index('--output') + 1]).write_text(json.dumps({'status':'passed'}), encoding='utf-8')
                if stage == 'uninstall':
                    for path in (workspace / 'installed').rglob('*.exe'):
                        path.unlink()
                    (workspace / 'installed/current').rmdir()
                    (workspace / 'installed').rmdir()
                class Process:
                    returncode = 2 if stage in failed_stages else 0
                    def wait(self, timeout):
                        return self.returncode
                return Process()

            with patch.object(delivery.subprocess, 'Popen', process), patch.object(delivery, 'windows_prerequisites', return_value={'status':'passed'}):
                result = delivery.validate_delivery(root / 'project', root / 'fresh stage', diagnostic, root / 'ISCC.exe', upstream_failed)
            promoted = root / 'project/dist/SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
            feed = root/'project/dist/update-feed'
            self.assertEqual(feed.exists(), result['release_promoted'])
            if result['release_promoted']:
                self.assertEqual((feed/'SHAQDailyOracleLab-0.6.0-win-x64-stable-full.nupkg').read_bytes(), b'final full')
                self.assertEqual((feed/'SHAQDailyOracleLab-0.5.0-win-x64-stable-full.nupkg').read_bytes(), b'public base')
                self.assertFalse((feed/'internal-acceptance-0.6.99-full.nupkg').exists())
                receipt = json.loads((feed/'delivery.json').read_text())
                self.assertEqual(receipt['installer_sha256'], hashlib.sha256(promoted.read_bytes()).hexdigest())
                self.assertEqual(receipt['target_version'], '0.6.0')
            self.assertEqual(json.loads((root / 'project/dist/diagnostic/windows-delivery.json').read_text(encoding='utf-8')), result)
            return result, commands, promoted.exists()

    def test_malformed_smoke_shapes_do_not_abort_later_checks_or_final_report(self):
        for evidence in ([], {'status':'passed','checks':'yes','methods':[{},{}]},
                         {'status':'passed','checks':{'ok':True},'methods':2}):
            with self.subTest(evidence=evidence):
                result, stages, promoted = self.exercise(set(), False, evidence)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual({s['name'] for s in result['stages'] if s['status']=='failed'},
                                 {'packaged-smoke','installed-smoke'})
                for name in ('compile-installer','installed-gui','installed-audit','uninstall','uninstall-check'):
                    self.assertIn(name, stages)
                self.assertFalse(promoted)

    def test_audit_failure_does_not_hide_installer_gui_or_uninstall_failures(self):
        result, stages, promoted = self.exercise({'payload-audit','installed-gui'}, diagnostic=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual({s['name'] for s in result['stages'] if s['status']=='failed'}, {'payload-audit','installed-gui'})
        for name in ('compile-installer','public-base-update','install','installed-smoke','installed-gui','installed-audit','uninstall','uninstall-check'):
            self.assertIn(name, stages)
        self.assertFalse(promoted)

    def test_diagnostic_never_promotes_and_final_requires_all_stages_pass(self):
        for diagnostic in (True, False):
            with self.subTest(diagnostic=diagnostic):
                result, _, promoted = self.exercise(set(), diagnostic)
                self.assertEqual(result['status'], 'passed')
                self.assertEqual(promoted, not diagnostic)

    def test_upstream_failure_never_promotes_the_final_installer_or_feed(self):
        result, _, promoted = self.exercise(set(), False, upstream_failed=True)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(promoted)

    def test_public_base_native_update_failure_blocks_promotion_but_not_fresh_install_checks(self):
        result, stages, promoted = self.exercise({'public-base-update'}, False)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(promoted)
        self.assertLess(stages.index('public-base-update'), stages.index('install'))
        for name in ('installed-smoke', 'installed-gui', 'installed-audit', 'uninstall-check'):
            self.assertIn(name, stages)

    def test_timed_out_process_is_terminated_and_reported(self):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            result = delivery.run_stage('timeout', [sys.executable, '-c', 'import time;time.sleep(10)'], root, root, 0.1)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('timed out', result['error'])

    def test_previous_success_report_cannot_mask_bootstrap_failure(self):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            report = root / 'smoke.json'
            report.write_text('{"status":"passed"}', encoding='utf-8')
            result = delivery.run_stage('packaged-smoke', [sys.executable, '-c', 'pass'], root, root, 10, report)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(report.exists())

    def test_invalid_microsoft_signature_prevents_runtime_execution(self):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = {'status':'failed', 'webview2_versions':[]}
            with patch.object(delivery, 'windows_prerequisites', return_value=missing), \
                    patch.object(delivery.urllib.request, 'urlopen', return_value=io.BytesIO(b'unsigned download')), \
                    patch.object(delivery, 'run_stage', return_value={'name':'webview2-signature','status':'failed'}) as stage:
                result = delivery.prepare_prerequisites(root, None)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual([call.args[0] for call in stage.call_args_list], ['webview2-signature'])
