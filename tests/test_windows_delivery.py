from pathlib import Path
import importlib.util
import io
import json
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

    def exercise(self, failed_stages, diagnostic, malformed_smoke=None):
        delivery = self.module()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'project').mkdir()
            (root / 'project/pyproject.toml').write_text('[project]\nversion="0.6.0"\n', encoding='utf-8')
            commands = []

            def process(args, **kwargs):
                # Only replace external Windows programs; real stage aggregation,
                # file prerequisites, report validation, cleanup and promotion run.
                stage = Path(kwargs['stdout'].name).stem
                commands.append(stage)
                workspace = root / 'fresh stage'
                if stage == 'build-app':
                    target = workspace / 'payload/SHAQ Daily Oracle Lab/SHAQ Daily Oracle Lab.exe'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'app')
                if stage == 'compile-installer':
                    target = workspace / 'installer/SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'installer')
                if stage == 'install':
                    (workspace / 'installed').mkdir()
                    (workspace / 'installed/SHAQ Daily Oracle Lab.exe').write_bytes(b'app')
                    (workspace / 'installed/unins000.exe').write_bytes(b'uninstaller')
                if '--smoke-output' in args:
                    evidence = ({'status':'passed','checks':{'two_methods':True},'methods':[{},{}]}
                                if malformed_smoke is None else malformed_smoke)
                    Path(args[args.index('--smoke-output') + 1]).write_text(json.dumps(evidence), encoding='utf-8')
                if '--gui-smoke' in args:
                    Path(args[args.index('--gui-smoke') + 1]).write_text(json.dumps({'status':'passed','pages':['run','editor','history']}), encoding='utf-8')
                if '--output' in args and stage != 'build-app':
                    Path(args[args.index('--output') + 1]).write_text(json.dumps({'status':'passed'}), encoding='utf-8')
                if stage == 'uninstall':
                    for path in (workspace / 'installed').iterdir():
                        path.unlink()
                    (workspace / 'installed').rmdir()
                class Process:
                    returncode = 2 if stage in failed_stages else 0
                    def wait(self, timeout):
                        return self.returncode
                return Process()

            with patch.object(delivery.subprocess, 'Popen', process), patch.object(delivery, 'windows_prerequisites', return_value={'status':'passed'}):
                result = delivery.validate_delivery(root / 'project', root / 'fresh stage', diagnostic, root / 'ISCC.exe')
            promoted = root / 'project/dist/SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
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
        for name in ('compile-installer','install','installed-smoke','installed-gui','installed-audit','uninstall','uninstall-check'):
            self.assertIn(name, stages)
        self.assertFalse(promoted)

    def test_diagnostic_never_promotes_and_final_requires_all_stages_pass(self):
        for diagnostic in (True, False):
            with self.subTest(diagnostic=diagnostic):
                result, _, promoted = self.exercise(set(), diagnostic)
                self.assertEqual(result['status'], 'passed')
                self.assertEqual(promoted, not diagnostic)

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
