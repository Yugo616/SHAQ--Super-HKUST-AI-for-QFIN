"""Atomic cross-process update admission; work leases do not serialize work."""
from contextlib import contextmanager, ExitStack
from functools import wraps
from pathlib import Path
import uuid
import json
import time
from datetime import datetime, timezone

from filelock import FileLock, Timeout


class UpdateBusy(RuntimeError):
    def __init__(self):
        super().__init__('分析、结算或后台写入正在运行，或正在安装更新；请等待结束后重试。')


class StaleRuntime(UpdateBusy):
    def __init__(self):
        RuntimeError.__init__(self, '软件已更新；此旧窗口不会再执行任务，请关闭后重新打开应用。')


class AdmissionGate:
    def __init__(self, data_root):
        self.data_root = Path(data_root)
        self.root = self.data_root / 'update-admission'
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _admission(self):
        # Wait briefly for another admission/probe, not for its actual work.
        lock = FileLock(str(self.root / 'admission.lock'), timeout=1)
        try:
            lock.acquire()
        except Timeout:
            raise UpdateBusy() from None
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def work(self):
        with self._admission():
            if (self.root / 'installing.json').exists():
                raise UpdateBusy()
            path = self.root / (uuid.uuid4().hex + '.lease')
            lease = FileLock(str(path), thread_local=False)
            lease.acquire(timeout=0)
        try:
            yield
        finally:
            lease.release()
            # Reclaim under admission only: never unlink an inode while an
            # installer may be probing it. A crashed process leaves an unlocked
            # harmless file which install also reclaims.
            try:
                with self._admission():
                    path.unlink(missing_ok=True)
            except UpdateBusy:
                pass

    @contextmanager
    def install(self):
        with self._admission(), ExitStack() as held:
            if (self.root / 'installing.json').exists():
                raise UpdateBusy()
            research = self.data_root / 'research'
            candidates = [*self.root.glob('*.lease'),
                          research / 'schedule.lock', research / 'result_refresh.lock',
                          research / 'minute_refresh.lock', *research.glob('jobs/*.lock')]
            stale = []
            try:
                for path in candidates:
                    if path.exists():
                        if path.suffix == '.lease':
                            with FileLock(str(path), timeout=0):
                                stale.append(path)
                        else:
                            held.enter_context(FileLock(str(path), timeout=0))
            except Timeout:
                raise UpdateBusy() from None
            for path in stale:
                path.unlink(missing_ok=True)
            yield

    def mark_installing(self, version, method='manual'):
        from .settings import _atomic_json
        _atomic_json(self.root / 'installing.json', {'target_version': version, 'method':method})

    def cancel_failed_launch(self):
        (self.root / 'installing.json').unlink(missing_ok=True)

    def finish_restart(self, version):
        with self._admission():
            path = self.root / 'installing.json'
            if not path.exists():
                return False
            pending = json.loads(path.read_text(encoding='utf-8'))
            if pending['target_version'] != version:
                return False
            from .settings import _atomic_json
            _atomic_json(self.data_root / 'software-update-history.json', {
                'version':version, 'method':pending.get('method','manual'),
                'completed_at':datetime.now(timezone.utc).isoformat(),
            })
            path.unlink()
            return True


def gate_for(paths):
    root = getattr(paths, 'data_root', None)
    if root is None:
        root = paths.research_root.parent
    return AdmissionGate(root)


class WorkerAdmission:
    """Shared startup-generation guard for workers, GUI calls and updates."""
    def __init__(self, paths):
        from .app_paths import application_version
        self.package_root = paths.package_root
        self.version = application_version(self.package_root)
        self.gate = gate_for(paths)
        self.history = self._completed_update()
        self.lease = None

    def _completed_update(self):
        try:
            return (self.gate.data_root / 'software-update-history.json').read_text(encoding='utf-8')
        except FileNotFoundError:
            return ''

    def __enter__(self):
        lease = self.gate.work()
        lease.__enter__()
        try:
            self.assert_current()
        except BaseException:
            lease.__exit__(None, None, None)
            raise
        self.lease = lease
        return self

    def assert_current(self):
        from .app_paths import application_version
        if application_version(self.package_root) != self.version or self._completed_update() != self.history:
            raise StaleRuntime()

    @contextmanager
    def work(self):
        # Per-call lease (not self.lease) allows concurrent GUI calls without
        # changing this instance's immutable startup-generation baseline.
        with self.gate.work():
            self.assert_current()
            yield

    def __exit__(self, *exc):
        if self.lease is not None:
            self.lease.__exit__(*exc)
            self.lease = None

    def pause(self, seconds):
        self.__exit__(None, None, None)
        time.sleep(seconds)
        # An installation in progress makes the old worker exit at its next
        # wake, without killing a running analysis or writing a failure record.
        self.__enter__()


def guarded_method(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        paths = self.paths if hasattr(self, 'paths') else (args[0] if args else kwargs.get('paths'))
        if paths is None:
            from .app_paths import app_paths
            paths = app_paths()
        with gate_for(paths).work():
            return method(self, *args, **kwargs)
    return guarded


def guarded_worker(worker):
    @wraps(worker)
    def guarded(*args, **kwargs):
        paths = args[0] if args else kwargs['paths']
        with gate_for(paths).work():
            return worker(*args, **kwargs)
    return guarded


def start_guarded_thread(paths, thread):
    """Acquire before starting, transfer the lease across the thread boundary."""
    lease = gate_for(paths).work()
    lease.__enter__()
    original = thread.run
    def run():
        try:
            original()
        finally:
            lease.__exit__(None, None, None)
    thread.run = run
    try:
        thread.start()
    except BaseException:
        lease.__exit__(None, None, None)
        raise
