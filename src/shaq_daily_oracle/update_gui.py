"""Cooperative cross-process GUI quiescence after update work admission.

Only non-secret booleans cross the filesystem. Each live window freezes input
before acknowledging readiness; a dirty/unresponsive window prevents apply.
"""
import json
import threading
import time
import uuid

from filelock import FileLock, Timeout

from .settings import _atomic_json
from .update_admission import AdmissionGate


class UnsavedEdits(RuntimeError):
    def __init__(self):
        super().__init__('请先保存或放弃所有窗口中尚未保存的方法或连接修改，再更新。')


class GuiSession:
    def __init__(self, root, window, *, admission=None, timeout=10, poll=.05):
        self.root, self.window = root / 'gui-sessions', window
        self.admission = admission or AdmissionGate(root.parent)
        self.timeout, self.poll = timeout, poll
        self.token = uuid.uuid4().hex
        self.stop = threading.Event()
        self.request = self.root / 'request.json'

    def __enter__(self):
        # Registration is work: either it is visible before install admission,
        # or it is rejected. Production also supplies the bridge's immutable
        # generation guard, covering a process paused across a complete update.
        with self.admission.work():
            self.root.mkdir(parents=True, exist_ok=True)
            self.lease = FileLock(str(self.root / (self.token + '.lock')), thread_local=False)
            self.lease.acquire(timeout=0)
        self.thread = threading.Thread(target=self._watch, daemon=True, name='shaq-gui-update')
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(self.timeout)
        self.lease.release()

    def _read(self, path):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}

    def _watch(self):
        seen = None
        while not self.stop.wait(self.poll):
            request = self._read(self.request)
            if self.token not in request.get('participants', []):
                continue
            state = (request.get('id'), request.get('phase'))
            if state == seen:
                continue
            seen = state
            try:
                if state[1] == 'prepare':
                    ready = self.window.evaluate_js('window.SHAQUpdateExit.prepare()') is True
                    _atomic_json(self.root / (self.token + '.json'), {'id': state[0], 'ready': ready})
                elif state[1] == 'close' and request['owner'] != self.token:
                    self.window.destroy()
                    _atomic_json(self.root / (self.token + '.json'), {'id': state[0], 'closed': True})
                elif state[1] == 'cancel':
                    self.window.evaluate_js('window.SHAQUpdateExit.cancel()')
                    _atomic_json(self.root / (self.token + '.json'), {'id': state[0], 'cancelled': True})
            except Exception:
                # A window not yet loaded or no longer responsive cannot admit
                # an update; never assume that it has no unsaved work.
                _atomic_json(self.root / (self.token + '.json'), {'id': state[0], 'ready': False})

    def _live(self):
        live = []
        for path in self.root.glob('*.lock'):
            try:
                with FileLock(str(path), timeout=0):
                    pass
            except Timeout:
                live.append(path.stem)
        return live

    def _wait(self, tokens, request_id, key):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            rows = [self._read(self.root / (token + '.json')) for token in tokens]
            if all(row.get('id') == request_id and row.get(key) is True for row in rows):
                return
            if key == 'ready' and any(row.get('id') == request_id and row.get('ready') is False for row in rows):
                raise UnsavedEdits()
            time.sleep(self.poll)
        raise UnsavedEdits()

    def cancel(self):
        request = self._read(self.request)
        if request.get('owner') == self.token:
            _atomic_json(self.request, {**request, 'phase': 'cancel'})
            self._wait(self._live(), request['id'], 'cancelled')

    def quiesce(self):
        tokens = self._live()
        request = {'id': uuid.uuid4().hex, 'owner': self.token, 'phase': 'prepare', 'participants': tokens}
        _atomic_json(self.request, request)
        try:
            self._wait(tokens, request['id'], 'ready')
        except Exception:
            self.cancel()
            raise
        _atomic_json(self.request, {**request, 'phase': 'close'})
        self._wait([token for token in tokens if token != self.token], request['id'], 'closed')
