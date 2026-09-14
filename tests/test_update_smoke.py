import importlib.util
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from shaq_daily_oracle import update_smoke


class InstalledAcceptanceContractTests(unittest.TestCase):
    def test_process_probe_observes_real_running_process_without_signals(self):
        self.assertTrue(update_smoke.process_running(os.getpid()))

    def test_acceptance_validation_can_precede_any_data_directory_creation(self):
        with tempfile.TemporaryDirectory(prefix='shaq-installed-update-') as directory:
            root=Path(directory).resolve()
            try:
                paths=update_smoke.acceptance_paths(root,root/'installed/app',ensure=False)
            except TypeError:
                self.fail('Validate isolated paths before creating native coordination or data files')
            self.assertFalse(paths.data_root.exists())

    def driver(self):
        path = Path(__file__).parents[1] / 'packaging/installed_update_acceptance.py'
        spec = importlib.util.spec_from_file_location('acceptance_driver', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_deferred_apply_rejects_changed_program_or_incorrect_wait_state(self):
        check = getattr(update_smoke, 'verify_deferred', None)
        self.assertIsNotNone(check, 'Native acceptance must reject false waiting evidence')
        good = {'status': 'ready', 'waiting_for_idle': True,
                'message': '已下载，等待本地任务运行完更新'}
        check(good, {'program': 'same'}, {'program': 'same'}, dirty=False)
        for state, after in (({**good, 'status': 'applying'}, {'program': 'same'}),
                             (good, {'program': 'replaced'}),
                             ({**good, 'message': 'wrong'}, {'program': 'same'})):
            with self.assertRaises(RuntimeError):
                check(state, {'program': 'same'}, after, dirty=False)

    def test_coordination_event_requires_success_and_retains_payload(self):
        wait = getattr(update_smoke, 'wait_event', None)
        self.assertIsNotNone(wait, 'Native coordination must fail closed on peer errors')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'peer.json'
            with self.assertRaises(TimeoutError):
                wait(path, timeout=0)
            path.write_text(json.dumps({'status': 'failed', 'error': 'peer died'}))
            with self.assertRaisesRegex(RuntimeError, 'peer died'):
                wait(path, timeout=.1)
            path.write_text(json.dumps({'status': 'passed', 'pid': 42}))
            self.assertEqual(wait(path, timeout=.1)['pid'], 42)

    def test_rendered_target_waits_for_matching_confirmation_generation(self):
        wait = getattr(update_smoke, 'wait_restart_confirmation', None)
        self.assertIsNotNone(wait, 'Rendered DOM may precede asynchronous startup confirmation')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            old = {'version': '0.6.99', 'installation_id': 'previous', 'completed_at': 'earlier'}
            same_version_stale = {**old, 'version': '0.7.0'}
            current = {'version': '0.7.0', 'installation_id': 'current', 'completed_at': 'now'}
            path.write_text(json.dumps(old))
            delayed = iter([same_version_stale, current])
            def confirm_later(_):
                path.write_text(json.dumps(next(delayed)))
            with patch.object(update_smoke.time, 'monotonic', side_effect=[0, .1, .2]), \
                    patch.object(update_smoke.time, 'sleep', side_effect=confirm_later):
                self.assertEqual(wait(path, '0.7.0', 'current', deadline=.3), current)

    def test_restart_confirmation_keeps_existing_deadline_and_rejects_missing_stale_malformed(self):
        wait = getattr(update_smoke, 'wait_restart_confirmation', None)
        self.assertIsNotNone(wait)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            cases = [None, {'version': '0.6.99', 'installation_id': 'previous', 'completed_at': 'earlier'},
                     {'version': '0.7.0', 'installation_id': 'current', 'completed_at': ''}]
            for value in cases:
                if value is not None: path.write_text(json.dumps(value))
                with patch.object(update_smoke.time, 'monotonic', side_effect=[.9, 1.0]), \
                        patch.object(update_smoke.time, 'sleep'):
                    with self.assertRaisesRegex(TimeoutError, 'GUI-health'):
                        wait(path, '0.7.0', 'current', deadline=1.0)
            path.write_text('{malformed')
            with patch.object(update_smoke.time, 'monotonic', return_value=.9):
                with self.assertRaises(json.JSONDecodeError):
                    wait(path, '0.7.0', 'current', deadline=1.0)

    def test_restart_confirmation_retries_only_bounded_windows_read_denial(self):
        wait = update_smoke.wait_restart_confirmation
        current = {'version': '0.7.1', 'installation_id': 'current', 'completed_at': 'now'}
        denied = PermissionError(errno.EACCES, 'receipt temporarily inaccessible')

        class Receipt:
            def __init__(self, outcomes):
                self.outcomes = iter(outcomes)

            def read_text(self, **_kwargs):
                outcome = next(self.outcomes)
                if isinstance(outcome, BaseException):
                    raise outcome
                return json.dumps(outcome)

        with patch.object(update_smoke.sys, 'platform', 'win32'), \
                patch.object(update_smoke.time, 'monotonic', side_effect=[0, .1]), \
                patch.object(update_smoke.time, 'sleep'):
            self.assertEqual(wait(Receipt([denied, current]), '0.7.1', 'current', deadline=.2), current)

        persistent = PermissionError(errno.EACCES, 'receipt remained inaccessible')
        with patch.object(update_smoke.sys, 'platform', 'win32'), \
                patch.object(update_smoke.time, 'monotonic', side_effect=[0, .1, .2]), \
                patch.object(update_smoke.time, 'sleep'):
            with self.assertRaisesRegex(TimeoutError, 'GUI-health') as raised:
                wait(Receipt([persistent, persistent]), '0.7.1', 'current', deadline=.2)
        self.assertIs(raised.exception.__cause__, persistent)

        with patch.object(update_smoke.sys, 'platform', 'darwin'), \
                patch.object(update_smoke.time, 'monotonic', return_value=0):
            with self.assertRaises(PermissionError):
                wait(Receipt([denied]), '0.7.1', 'current', deadline=.2)

        wrong_code = PermissionError(errno.EPERM, 'not a Windows sharing-style read denial')
        with patch.object(update_smoke.sys, 'platform', 'win32'), \
                patch.object(update_smoke.time, 'monotonic', return_value=0):
            with self.assertRaises(PermissionError):
                wait(Receipt([wrong_code]), '0.7.1', 'current', deadline=.2)

    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows file-sharing semantics')
    def test_windows_restart_confirmation_survives_exclusive_receipt_publish_window(self):
        import ctypes
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'software-update-history.json'
            current = {'version': '0.7.1', 'installation_id': 'current', 'completed_at': 'now'}
            path.write_text(json.dumps(current), encoding='utf-8')
            kernel32 = ctypes.windll.kernel32
            create_file = kernel32.CreateFileW
            create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create_file.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            generic_read = 0x80000000
            open_existing = 3
            file_attribute_normal = 0x80
            handle = create_file(
                str(path), generic_read, 0, None, open_existing, file_attribute_normal, None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError()

            observed_denial = threading.Event()

            class ObservedReceipt:
                def read_text(self, **kwargs):
                    try:
                        return path.read_text(**kwargs)
                    except PermissionError as exc:
                        if exc.errno == errno.EACCES:
                            observed_denial.set()
                        raise

            stop_release = threading.Event()
            closed = []
            def release():
                while not observed_denial.wait(.05):
                    if stop_release.is_set():
                        return
                closed.append(bool(close_handle(handle)))

            releaser = threading.Thread(target=release)
            releaser.start()
            try:
                receipt = update_smoke.wait_restart_confirmation(
                    ObservedReceipt(), '0.7.1', 'current', deadline=time.monotonic() + 2,
                )
            finally:
                stop_release.set()
                releaser.join(2)
                if not closed:
                    close_handle(handle)
            self.assertFalse(releaser.is_alive())
            self.assertTrue(observed_denial.is_set())
            self.assertEqual(closed, [True])
            self.assertEqual(receipt, current)

    def test_missing_health_reports_retained_target_failure_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory)
            failed = events / 'target-result.json'
            failed.write_text(json.dumps({'status': 'failed', 'error': 'confirmation rejected'}))
            with self.assertRaisesRegex(RuntimeError, 'confirmation rejected'):
                update_smoke.wait_event(events / 'target-health.json', timeout=.1, failure_path=failed)

    def test_private_acceptance_failure_diagnostic_retains_stack_and_system_codes(self):
        failure = PermissionError(errno.EACCES, 'receipt inaccessible')
        failure.winerror = 32
        try:
            raise failure
        except PermissionError as exc:
            diagnostic = update_smoke.failure_diagnostic(exc)
        self.assertEqual(diagnostic['error'], str(failure))
        self.assertEqual(diagnostic['exception_type'], 'PermissionError')
        self.assertEqual(diagnostic['errno'], errno.EACCES)
        self.assertEqual(diagnostic['winerror'], 32)
        self.assertIn('raise failure', diagnostic['traceback'])
        self.assertIn('PermissionError', diagnostic['traceback'])

    def test_dirty_and_saved_gui_wait_for_real_work_admission_without_resetting_budget(self):
        wait = getattr(update_smoke, 'wait_automatic_admission', None)
        self.assertIsNotNone(wait, 'Dirty/saved GUI can coexist with legitimate backend work')
        from test_update_runtime import UpdateRuntimeTests
        from shaq_daily_oracle.update_admission import AdmissionGate
        from shaq_daily_oracle.update_gui import UnsavedEdits
        fixture = UpdateRuntimeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        for dirty in (True, False):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                runtime, manager, selected, feed = fixture.runtime(directory)
                manager.download_updates = lambda info, progress: None
                applied = []
                manager.apply_updates_and_restart = lambda info: applied.append(info)
                def quiesce():
                    if dirty: raise UnsavedEdits()
                runtime.gui_session = SimpleNamespace(quiesce=quiesce, cancel=lambda: None)
                runtime.set_automatic(True); fixture.check(runtime, selected, feed)
                runtime.download(); runtime._download_thread.join(3)
                lease = AdmissionGate(Path(directory)).work(); lease.__enter__()
                now = [.9]; released = [False]
                def sleep(seconds):
                    self.assertFalse(applied)
                    now[0] += seconds
                    lease.__exit__(None, None, None); released[0] = True
                clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
                try:
                    with patch.object(update_smoke, 'time', clock), \
                            patch.object(update_smoke, 'program_hashes', return_value={'program': 'same'}):
                        state = wait(runtime, {'program': 'same'}, SimpleNamespace(poll=lambda: None),
                                     dirty=dirty, deadline=1.1)
                finally:
                    if not released[0]: lease.__exit__(None, None, None)
                self.assertTrue(released[0])
                self.assertLess(now[0], 1.1)
                self.assertEqual(state['status'], 'ready' if dirty else 'applying')
                self.assertEqual(len(applied), 0 if dirty else 1)
                if dirty: self.assertTrue(state['waiting_for_edits'])

    def test_admission_wait_rejects_persistent_busy_changed_program_dead_peer_and_unsaved_final(self):
        wait = getattr(update_smoke, 'wait_automatic_admission', None)
        self.assertIsNotNone(wait)
        busy = {'status': 'ready', 'waiting_for_idle': True, 'message': '已下载，等待本地任务运行完更新'}
        edits = {'status': 'ready', 'waiting_for_edits': True,
                 'message': '请先保存或放弃所有窗口中尚未保存的方法或连接修改，再更新。'}
        cases = [(busy, {'program': 'same'}, None, True, TimeoutError),
                 (busy, {'program': 'changed'}, None, True, RuntimeError),
                 (busy, {'program': 'same'}, 2, True, RuntimeError),
                 ({**busy, 'message': 'incorrect'}, {'program': 'same'}, None, True, RuntimeError),
                 (edits, {'program': 'same'}, None, False, RuntimeError)]
        for state, program, peer_exit, dirty, error in cases:
            with self.subTest(state=state, program=program, peer_exit=peer_exit, dirty=dirty):
                now = [.9]
                clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds))
                with patch.object(update_smoke, 'time', clock), \
                        patch.object(update_smoke, 'program_hashes', return_value=program):
                    with self.assertRaises(error):
                        wait(SimpleNamespace(automatic_step=lambda: state), {'program': 'same'},
                             SimpleNamespace(poll=lambda: peer_exit), dirty=dirty, deadline=1.0)
                self.assertLessEqual(now[0], 1.0)

    def test_feed_transition_never_offers_future_version(self):
        driver = self.driver()
        select = getattr(driver, 'transition_feed', None)
        self.assertIsNotNone(select, 'Consecutive native runs need separate target feeds')
        assets = [{'Version': v, 'Type': kind, 'FileName': v + kind}
                  for v in ('0.6.98', '0.6.99', '0.7.0') for kind in ('Full', 'Delta')]
        self.assertEqual(select({'Assets': assets}, '0.6.99')['Assets'], assets[2:4])
        with self.assertRaises(ValueError):
            select({'Assets': assets}, '0.8.0')

    def test_cache_acceptance_rejects_accumulated_owned_packages(self):
        check = getattr(self.driver(), 'verify_cache', None)
        self.assertIsNotNone(check, 'Repeated update must assert native cache cleanup')
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'current-full.nupkg').write_bytes(b'current')
            self.assertEqual(check(cache, 'current-full.nupkg'), ['current-full.nupkg'])
            (cache / 'obsolete-full.nupkg').write_bytes(b'obsolete')
            with self.assertRaises(RuntimeError):
                check(cache, 'current-full.nupkg')

    def test_program_copy_acceptance_rejects_obsolete_installation_directories(self):
        check=getattr(self.driver(),'verify_program_copies',None)
        self.assertIsNotNone(check, 'Native replacement must not accumulate old programs')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=root/'Example.app'
            app.mkdir()
            self.assertEqual(check(app,'darwin'), ['Example.app'])
            (root/'Example-old.app').mkdir()
            with self.assertRaises(RuntimeError):check(app,'darwin')
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)
            (app/'current').mkdir()
            self.assertEqual(check(app,'win32'), ['current'])
            (app/'app-0.6.98').mkdir()
            with self.assertRaises(RuntimeError):check(app,'win32')
