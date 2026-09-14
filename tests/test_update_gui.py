import tempfile
import subprocess
import json
import time
import threading
import os
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from shaq_daily_oracle import update_gui
from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy, WorkerAdmission


class Window:
    def __init__(self, dirty=False):
        self.dirty, self.frozen, self.closed = dirty, False, False

    def evaluate_js(self, script):
        if 'prepare' in script:
            self.frozen = True
            return not self.dirty
        self.frozen = False

    def destroy(self):
        self.closed = True


class UpdateGuiTests(unittest.TestCase):
    @staticmethod
    def sharing_error(code=32):
        error = PermissionError(13, 'synthetic Windows file sharing conflict')
        error.winerror = code
        return error

    def test_errno_only_windows_read_uses_native_result_not_guessed_access_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            session = update_gui.GuiSession(AdmissionGate(Path(directory)).root, Window(), timeout=.2, poll=.01)
            session.root.mkdir()
            session._write(session.request, {'ready': True, 'label': '已確認'})
            original = Path.read_text
            attempts = []
            def native_read(path):
                attempts.append(path)
                if len(attempts) == 1:
                    raise self.sharing_error()
                return original(path, encoding='utf-8')
            errno_only = PermissionError(13, 'CRT errno only')
            self.assertIsNone(getattr(errno_only, 'winerror', None))
            with patch.object(sys, 'platform', 'win32'), \
                    patch.object(Path, 'read_text', side_effect=errno_only), \
                    patch.object(update_gui, '_read_windows_text', native_read, create=True):
                self.assertEqual(session._read(session.request), {'ready': True, 'label': '已確認'})
            self.assertEqual(attempts, [session.request, session.request])

    def test_errno_only_windows_read_does_not_retry_native_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            session = update_gui.GuiSession(AdmissionGate(Path(directory)).root, Window(), timeout=.2, poll=.01)
            session.root.mkdir()
            for native_error in (self.sharing_error(5), PermissionError(13, 'unclassified native read denial')):
                with self.subTest(native_error=native_error):
                    attempts = []
                    def native_read(path):
                        attempts.append(path)
                        raise native_error
                    with patch.object(sys, 'platform', 'win32'), \
                            patch.object(Path, 'read_text', side_effect=PermissionError(13, 'CRT errno only')), \
                            patch.object(update_gui, '_read_windows_text', native_read, create=True):
                        with self.assertRaises(PermissionError) as raised:
                            session._read(session.request)
                    self.assertIs(raised.exception, native_error)
                    self.assertEqual(attempts, [session.request])

    def test_transient_request_and_ack_reads_preserve_two_window_protocol(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                root = AdmissionGate(Path(directory)).root
                first, second = Window(), Window(dirty)
                original = Path.read_text
                failed = set()
                def read(path, *args, **kwargs):
                    if path.parent.name == 'gui-sessions' and path.exists():
                        key = (threading.current_thread().ident, path.name)
                        if key not in failed:
                            failed.add(key)
                            raise self.sharing_error()
                    return original(path, *args, **kwargs)
                with patch.object(Path, 'read_text', read):
                    with update_gui.GuiSession(root, first, timeout=2, poll=.01) as owner, \
                            update_gui.GuiSession(root, second, timeout=2, poll=.01) as peer:
                        if dirty:
                            with self.assertRaises(update_gui.UnsavedEdits):
                                owner.quiesce()
                            self.assertFalse(first.frozen or second.frozen)
                            self.assertFalse(first.closed or second.closed)
                        else:
                            owner.quiesce()
                            self.assertTrue(second.closed)
                            self.assertTrue(first.frozen)
                            self.assertFalse(first.closed)
                        self.assertTrue(owner.thread.is_alive() and peer.thread.is_alive())

    def test_transient_replacement_preserves_ack_and_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = AdmissionGate(Path(directory)).root
            first, second = Window(), Window()
            original = os.replace
            failed = set()
            def replace(source, target):
                if target.parent.name == 'gui-sessions' and target not in failed:
                    failed.add(target)
                    raise self.sharing_error(33)
                return original(source, target)
            with patch.object(os, 'replace', replace):
                with update_gui.GuiSession(root, first, timeout=2, poll=.01) as owner, \
                        update_gui.GuiSession(root, second, timeout=2, poll=.01) as peer:
                    owner.quiesce()
                    self.assertTrue(second.closed)
                    self.assertFalse(first.closed)
                    self.assertTrue(owner.thread.is_alive() and peer.thread.is_alive())

    def test_permanent_request_read_failure_is_reported_by_watcher_and_blocks_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = AdmissionGate(Path(directory)).root
            window = Window()
            denied = PermissionError(13, 'synthetic permanent access denial')
            attempted = threading.Event()
            original = Path.read_text
            def read(path, *args, **kwargs):
                if path.name == 'request.json':
                    attempted.set()
                    raise denied
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', read), \
                    patch.object(update_gui, '_read_windows_text', side_effect=denied), \
                    self.assertLogs(update_gui.__name__, 'ERROR'):
                with update_gui.GuiSession(root, window, timeout=.2, poll=.01) as owner:
                    self.assertTrue(attempted.wait(1))
                    owner.thread.join(1)
                    self.assertIs(getattr(owner, 'error', None), denied)
                    with self.assertRaises(PermissionError):
                        owner.quiesce()
                    self.assertFalse(window.closed)

    def test_permanent_and_malformed_reads_are_not_missing_or_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            session = update_gui.GuiSession(AdmissionGate(Path(directory)).root, Window())
            session.root.mkdir()
            for failure in (PermissionError(13, 'denied'), self.sharing_error(5),
                            json.JSONDecodeError('malformed', '{', 1)):
                with self.subTest(failure=failure), patch.object(Path, 'read_text', side_effect=failure), \
                        patch.object(update_gui, '_read_windows_text', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        session._read(session.request)

    def test_permanent_ack_write_failure_is_reported_and_never_closes_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = AdmissionGate(Path(directory)).root
            window = Window()
            original = os.replace
            denied = PermissionError(13, 'synthetic permanent acknowledgement write denial')
            def replace(source, target):
                if target.name == owner.token + '.json':
                    raise denied
                return original(source, target)
            with update_gui.GuiSession(root, window, timeout=.2, poll=.01) as owner:
                with patch.object(os, 'replace', replace), self.assertLogs(update_gui.__name__, 'ERROR'):
                    with self.assertRaises(PermissionError): owner.quiesce()
                    owner.thread.join(1)
                    self.assertIs(owner.error, denied)
                    self.assertFalse(window.closed)

    def test_persistent_sharing_failure_has_bounded_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            session = update_gui.GuiSession(AdmissionGate(Path(directory)).root, Window(), timeout=.03, poll=.005)
            session.root.mkdir()
            failure = self.sharing_error()
            attempted = []
            def read(*args, **kwargs):
                attempted.append(True)
                raise failure
            with patch.object(Path, 'read_text', read):
                with self.assertRaises(PermissionError):
                    session._read(session.request)
            self.assertGreater(len(attempted), 1)
            self.assertLess(len(attempted), 100)

    def test_reader_handle_excludes_concurrent_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = AdmissionGate(Path(directory)).root
            reader = update_gui.GuiSession(root, Window(), timeout=2, poll=.01)
            writer = update_gui.GuiSession(root, Window(), timeout=2, poll=.01)
            reader.root.mkdir()
            update_gui._atomic_json(reader.request, {'phase': 'prepare'})
            opened, release, replacing, writing = (threading.Event() for _ in range(4))
            original_read, original_replace = Path.read_text, os.replace
            failures, values = [], []
            def read(path, *args, **kwargs):
                if path == reader.request:
                    with path.open(encoding='utf-8') as handle:
                        opened.set()
                        if not release.wait(2):
                            raise TimeoutError('reader release missing')
                        return handle.read()
                return original_read(path, *args, **kwargs)
            def replace(source, target):
                replacing.set()
                return original_replace(source, target)
            def run_read():
                try: values.append(reader._read(reader.request))
                except Exception as exc: failures.append(exc)
            def run_write():
                writing.set()
                try: writer._write(writer.request, {'phase': 'close'})
                except Exception as exc: failures.append(exc)
            with patch.object(Path, 'read_text', read), patch.object(os, 'replace', replace):
                r, w = threading.Thread(target=run_read), threading.Thread(target=run_write)
                r.start()
                try:
                    self.assertTrue(opened.wait(1))
                    w.start()
                    self.assertTrue(writing.wait(1))
                    self.assertFalse(replacing.wait(.1), 'replacement overlapped the open reader')
                finally:
                    release.set()
                    r.join(2)
                    if w.ident is not None: w.join(2)
            self.assertEqual(failures, [])
            self.assertEqual(values, [{'phase': 'prepare'}])
            self.assertEqual(reader._read(reader.request), {'phase': 'close'})

    @unittest.skipUnless(sys.platform == 'win32', 'requires real Windows sharing handles')
    def test_real_windows_exclusive_handle_recovers_for_read_and_replace(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            session = update_gui.GuiSession(AdmissionGate(Path(directory)).root, Window(), timeout=2, poll=.01)
            session.root.mkdir()
            for operation in ('read', 'replace'):
                with self.subTest(operation=operation):
                    session._write(session.request, {'phase': 'prepare'})
                    # GENERIC_READ, no sharing, OPEN_EXISTING: another normal
                    # reader or rename must observe a real sharing violation.
                    handle = kernel.CreateFileW(str(session.request), 0x80000000, 0, None, 3, 0, None)
                    self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
                    observed = []
                    original = update_gui._read_windows_text if operation == 'read' else os.replace
                    def attempt(*args, **kwargs):
                        nonlocal handle
                        try:
                            return original(*args, **kwargs)
                        except PermissionError as exc:
                            observed.append(exc.winerror)
                            self.assertTrue(kernel.CloseHandle(handle))
                            handle = None
                            raise
                    try:
                        if operation == 'read':
                            # CPython's CRT-backed read reports errno only. Keep
                            # the exclusive handle until the native fallback has
                            # independently observed its own sharing violation.
                            with self.assertRaises(PermissionError) as crt_failure:
                                session.request.read_text(encoding='utf-8')
                            self.assertEqual(crt_failure.exception.errno, 13)
                            self.assertIsNone(getattr(crt_failure.exception, 'winerror', None))
                        target, name = (update_gui, '_read_windows_text') if operation == 'read' else (os, 'replace')
                        with patch.object(target, name, attempt):
                            if operation == 'read':
                                self.assertEqual(session._read(session.request), {'phase': 'prepare'})
                            else:
                                session._write(session.request, {'phase': 'close'})
                        self.assertEqual(observed, [32])
                        expected = 'prepare' if operation == 'read' else 'close'
                        self.assertEqual(session._read(session.request), {'phase': expected})
                        self.assertEqual(list(session.root.glob('*.tmp')), [])
                    finally:
                        if handle is not None: kernel.CloseHandle(handle)

    def test_paused_old_bridge_cannot_register_after_target_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=SimpleNamespace(data_root=Path(directory),package_root=Path(__file__).parents[1])
            old=WorkerAdmission(paths)
            gate=old.gate
            with gate.install():gate.mark_installing('0.7.0')
            with gate.target_startup('0.7.0'):
                fresh=WorkerAdmission(paths)
                with update_gui.GuiSession(gate.root,Window(),admission=fresh):pass
                gate.finish_restart('0.7.0')
            with self.assertRaises(UpdateBusy):
                with update_gui.GuiSession(gate.root,Window(),admission=old):pass

    def test_registration_after_participant_discovery_is_rejected_during_install(self):
        with tempfile.TemporaryDirectory() as directory:
            gate=AdmissionGate(Path(directory))
            late=update_gui.GuiSession(gate.root,Window())
            with update_gui.GuiSession(gate.root,Window()) as owner:
                discover=owner._live
                def discover_then_register():
                    participants=discover()
                    with self.assertRaises(UpdateBusy):
                        with late:pass
                    return participants
                with gate.install():
                    gate.mark_installing('9.0.0')
                    with patch.object(owner,'_live',side_effect=discover_then_register):owner.quiesce()
                self.assertNotIn(late.token,owner._live())

    def test_target_window_does_not_consume_previous_generations_close_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, Window()) as owner:
                owner.quiesce()
            target=Window()
            with update_gui.GuiSession(root,target):time.sleep(.15)
            self.assertFalse(target.closed)

    def test_frontend_retains_unsaved_other_provider_and_edits_during_save(self):
        source = Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/update_exit.js'
        script = "const window={};const document={addEventListener(){},body:{inert:false},querySelector(){}};\n" + source.read_text() + '''
const e=window.SHAQUpdateExit;
e.dirty('connection:first');e.dirty('connection:second');
const saved=e.beforeSave('save_lab_model_profile',[{protocol:'second'}]);
e.afterSave(saved);
const other=e.prepare();e.cancel();
const first=e.beforeSave('save_lab_model_profile',[{protocol:'first'}]);
e.dirty('connection:first');e.afterSave(first);
const newer=e.prepare();e.cancel();
e.afterSave(e.beforeSave('save_lab_model_profile',[{protocol:'first'}]));
const clean=e.prepare();e.cancel();e.dirty('data');
e.afterSave(e.beforeSave('save_lab_setup',[{sec_identity:'synthetic'}]));
const partial=e.prepare();e.cancel();
e.afterSave(e.beforeSave('save_lab_setup',[{data_profile:{}}]));
console.log(JSON.stringify({other,newer,clean,partial,dataSaved:e.prepare(),frozen:document.body.inert}));
'''
        result = subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),dict(other=False,newer=False,clean=True,partial=False,dataSaved=True,frozen=True))

    def test_dirty_other_window_blocks_close_and_unfreezes_every_window(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Window(), Window(True)
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, first) as owner, update_gui.GuiSession(root, second):
                with self.assertRaises(update_gui.UnsavedEdits):owner.quiesce()
                self.assertFalse(first.closed or second.closed)
                self.assertFalse(first.frozen or second.frozen)

    def test_clean_other_window_closes_but_calling_window_waits_for_sdk_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Window(), Window()
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, first) as owner, update_gui.GuiSession(root, second):
                owner.quiesce()
                self.assertTrue(second.closed)
                self.assertFalse(first.closed)
                self.assertTrue(first.frozen)
