import json
import subprocess
import sys
import time
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from shaq_daily_oracle import research_schedule as schedule
from shaq_daily_oracle.background_process import background_process_options


class ResearchScheduleTests(unittest.TestCase):
    def test_windows_registers_current_user_worker_without_password_or_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = SimpleNamespace(research_root=root, package_root=root)
            command = [str(root / '中文 space' / 'app.exe'), '--research-worker']
            submitted = dict(enabled=True, start_et='08:35', selections=[{'version_id': 'main'}],
                             model_profile_id='local')
            documents = []

            def external(args, **kwargs):
                if args[0] == 'whoami':
                    return subprocess.CompletedProcess(args, 0, b'"team\\member","S-1-5-21-1-2-3-1001"\r\n', b'')
                if '/Create' in args:
                    documents.append(ET.fromstring(Path(args[args.index('/XML') + 1]).read_bytes()))
                    self.assertNotIn('/RP', args)
                    self.assertNotIn('/RU', args)
                return subprocess.CompletedProcess(args, 0, b'', b'')

            with patch.object(schedule.sys, 'platform', 'win32'), \
                    patch.object(schedule, 'worker_command', return_value=command), \
                    patch.object(schedule.subprocess, 'run', side_effect=external):
                schedule.save_schedule(paths, Mock(), submitted)
            self.assertEqual(len(documents), 1)
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            doc = documents[0]
            text = lambda path: doc.find(path, ns).text
            self.assertEqual(text('.//t:Exec/t:Command'), command[0])
            self.assertEqual(text('.//t:Exec/t:Arguments'), '--research-worker')
            self.assertEqual(text('.//t:Principal/t:UserId'), 'S-1-5-21-1-2-3-1001')
            self.assertEqual(text('.//t:LogonType'), 'InteractiveToken')
            self.assertEqual(text('.//t:RunLevel'), 'LeastPrivilege')
            self.assertEqual(text('.//t:MultipleInstancesPolicy'), 'IgnoreNew')
            self.assertEqual(text('.//t:StopIfGoingOnBatteries'), 'false')
            self.assertEqual(text('.//t:ExecutionTimeLimit'), 'PT0S')
            self.assertEqual(text('.//t:Repetition/t:Interval'), 'PT1M')
            self.assertEqual(json.loads((root / 'schedule.json').read_text())['enabled'], True)

    def test_failed_windows_registration_keeps_previous_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = SimpleNamespace(research_root=root, package_root=root)
            prior = {'enabled': False, 'start_et': '08:35:00', 'selections': [], 'model_profile_id': ''}
            (root / 'schedule.json').write_text(json.dumps(prior))
            def external(args, **kwargs):
                if args[0] == 'whoami':
                    return subprocess.CompletedProcess(args, 0, b'"test","S-1-5-21-1"', b'')
                return subprocess.CompletedProcess(args, 1, b'', b'access denied')
            with patch.object(schedule.sys, 'platform', 'win32'), \
                    patch.object(schedule.subprocess, 'run', side_effect=external):
                with self.assertRaisesRegex(ValueError, 'access denied'):
                    schedule.save_schedule(paths, Mock(), {**prior, 'enabled': True, 'selections': [{}]})
            self.assertEqual(json.loads((root / 'schedule.json').read_text()), prior)

    def test_disable_does_not_stop_running_task_or_require_windows_password(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = SimpleNamespace(research_root=Path(directory))
            with patch.object(schedule.sys, 'platform', 'win32'), \
                    patch.object(schedule.subprocess, 'run') as external:
                result = schedule.save_schedule(paths, Mock(), {'enabled': False})
            self.assertFalse(result['enabled'])
            external.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'requires native Windows process and Task Scheduler')
class NativeWindowsResearchTests(unittest.TestCase):
    def test_native_console_child_has_no_console_window(self):
        completed = subprocess.run([sys.executable, '-c',
            'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'],
            capture_output=True, text=True, timeout=15, check=True, **background_process_options())
        self.assertEqual(completed.stdout.strip(), '0')

    def test_registered_task_runs_windowless_worker_in_chinese_space_path(self):
        label = 'SHAQ-Test-' + uuid.uuid4().hex
        options = dict(capture_output=True, timeout=30, **background_process_options())
        with tempfile.TemporaryDirectory(prefix='shaq-中文 space-') as directory:
            root = Path(directory)
            paths = SimpleNamespace(research_root=root, package_root=root)
            result = root / 'finished.json'
            worker = root / 'worker.py'
            worker.write_text('import json,ctypes\nfrom pathlib import Path\n'
                              'Path(__file__).with_name("finished.json").write_text(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))\n', encoding='utf-8')
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            self.assertTrue(pythonw.is_file())
            try:
                with patch.object(schedule, 'SERVICE_LABEL', label), \
                        patch.object(schedule, 'worker_command', return_value=[str(pythonw), str(worker)]):
                    schedule._register_windows_worker(paths)
                subprocess.run(['schtasks', '/Run', '/TN', label], check=True, **options)
                deadline = time.monotonic() + 20
                while not result.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertTrue(result.is_file(), 'registered Windows task did not run')
                self.assertEqual(json.loads(result.read_text())['console'], 0)
            finally:
                subprocess.run(['schtasks', '/End', '/TN', label], **options)
                subprocess.run(['schtasks', '/Delete', '/F', '/TN', label], **options)
