import json
import subprocess
import sys
import time
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from shaq_daily_oracle import research_schedule as schedule
from shaq_daily_oracle.background_process import background_process_options


class ResearchScheduleTests(unittest.TestCase):
    def test_due_forecast_starts_before_historical_price_refresh(self):
        calls = []
        class Lab:
            def refresh_labels_if_due(self):
                calls.append('refresh')
                return {'status': 'running', 'operation_id': 'owned'}
            def wait_result_refresh(self, operation_id, timeout=180):
                calls.append(('wait', timeout))
                return {'status': 'complete', 'operation_id': operation_id}
            def start_batch(self, **kwargs):
                calls.append('forecast')
                return {'job_id': 'forecast', 'status': 'queued'}
            def job_statuses(self):
                return [{'job_id': 'forecast', 'status': 'complete'}]
        with tempfile.TemporaryDirectory() as directory:
            paths = SimpleNamespace(research_root=Path(directory))
            with patch('shaq_daily_oracle.lab_service.LabService', return_value=Lab()), \
                 patch.object(schedule, 'schedule_status', return_value={
                     'enabled': True, 'start_et': '08:35', 'selections': [], 'model_profile_id': 'fixture'}), \
                 patch.object(schedule, 'due_status', return_value='due'):
                self.assertEqual(schedule.run_research_worker(paths), 0)
        self.assertEqual(calls[0], 'forecast')
        self.assertIn(('wait', None), calls)

    def test_disabled_worker_drains_owned_refresh_before_exit(self):
        from shaq_daily_oracle.lab_service import LabService
        from test_result_refresh import ResultRefreshTests
        for enabled, due in [(False, 'closed')]:
            with self.subTest(enabled=enabled, due=due), tempfile.TemporaryDirectory() as directory:
                service = ResultRefreshTests().service(Path(directory))
                entered, release, waited, finished = (threading.Event() for _ in range(4))
                observed = {}
                def labels(**kwargs):
                    entered.set()
                    release.wait(3)
                    return {'refreshed_batches': ['LAB-good'], 'failures': []}
                real_wait = service.wait_result_refresh
                def short_wait(operation_id, timeout=180):
                    # Compress the old 180-second join to zero. A correctly
                    # draining scheduler passes None instead of the UI timeout.
                    observed['timeout'] = timeout
                    waited.set()
                    return real_wait(operation_id, timeout=None if timeout is None else 0)
                service.wait_result_refresh = short_wait
                outcome = []
                def worker():
                    try:
                        outcome.append(schedule.run_research_worker(service.paths))
                    finally:
                        finished.set()
                with patch('shaq_daily_oracle.lab_service.LabService', return_value=service), \
                     patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=labels), \
                     patch.object(LabService, '_refresh_minute_accounts', return_value={
                         'refreshed_dates': [], 'failures': []}), \
                     patch.object(schedule, 'schedule_status', return_value={'enabled': enabled, 'start_et': '08:35'}), \
                     patch.object(schedule, 'due_status', return_value=due):
                    thread = threading.Thread(target=worker)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertTrue(waited.wait(1))
                        self.assertIsNone(observed['timeout'])
                        self.assertFalse(finished.is_set())
                        self.assertEqual(service.result_refresh_status()['status'], 'running')
                    finally:
                        release.set()
                        thread.join(3)
                        if getattr(service, '_owned_result_refresh', None):
                            service._owned_result_refresh[1].join(2)
                    self.assertEqual(outcome, [0])
                    self.assertEqual(service.result_refresh_status()['status'], 'complete')

    def test_waiting_for_premarket_start_does_not_hold_schedule_lock_for_prices(self):
        calls = []
        lab = SimpleNamespace(refresh_labels_if_due=lambda: calls.append('price refresh') or {'status': 'not_due'})
        with tempfile.TemporaryDirectory() as directory:
            paths = SimpleNamespace(research_root=Path(directory))
            with patch('shaq_daily_oracle.lab_service.LabService', return_value=lab), \
                 patch.object(schedule, 'schedule_status', return_value={'enabled': True, 'start_et': '08:35'}), \
                 patch.object(schedule, 'due_status', return_value='waiting'):
                self.assertEqual(schedule.run_research_worker(paths), 0)
        self.assertEqual(calls, [])

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

    def test_existing_mac_service_is_not_reregistered_when_time_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = SimpleNamespace(research_root=root, package_root=root)
            destination = root/'Library/LaunchAgents'/f'{schedule.SERVICE_LABEL}.plist'
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b'existing service definition')
            with patch.object(schedule.sys, 'platform', 'darwin'), \
                 patch.object(Path, 'home', return_value=root), \
                 patch.object(schedule.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,b'',b'')) as external:
                for start in ('08:30','08:35'):
                    schedule.save_schedule(paths, Mock(), dict(enabled=True,start_et=start,selections=[{}],model_profile_id='local'))
            self.assertEqual(destination.read_bytes(), b'existing service definition')
            self.assertTrue(all(call.args[0][1]=='print' for call in external.call_args_list))


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
