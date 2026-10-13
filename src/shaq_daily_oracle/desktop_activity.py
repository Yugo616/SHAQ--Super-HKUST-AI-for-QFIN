"""Window-owned notifications for local and external research workers."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable


def event_script(name: str, value: dict) -> str:
    # JSON is transported as a string, never interpolated as executable content.
    payload = json.dumps(json.dumps(value, ensure_ascii=True), ensure_ascii=True)
    return f'window.dispatchEvent(new CustomEvent({json.dumps(name)},{{detail:JSON.parse({payload})}}));'


class DesktopNotifications:
    def __init__(self, activity: Callable, updates: Callable, emit: Callable, *, interval: float):
        if interval <= 0:
            raise ValueError('desktop notification interval must be positive')
        self.activity, self.updates, self.emit = activity, updates, emit
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._pending_lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._ready = threading.Event()
        self._delivery: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._threads or self._stop.is_set():
                return
            self._delivery = threading.Thread(target=self._deliver,
                name='desktop-notifications', daemon=True)
            self._delivery.start()
            # Release discovery has a separate reader so network/update locks
            # cannot delay research notifications.
            for name, target in (('activity', self._activity), ('updates', self._updates)):
                thread = threading.Thread(target=target, name=f'desktop-{name}', daemon=True)
                self._threads.append(thread)
                thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._pending_lock:
            self._pending.clear()
        self._ready.set()
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join()
        # pywebview 6.1 can wait forever for a renderer that was just destroyed.
        # The delivery thread owns only copied payloads, never data-file handles.
        # Drain the file readers, but never hold application exit on that renderer.
        if self._delivery is not None:
            self._delivery.join(self.interval)

    def _send(self, name: str, value: dict) -> None:
        with self._pending_lock:
            if not self._stop.is_set():
                self._pending[name] = value
                self._ready.set()

    def _deliver(self) -> None:
        while not self._stop.is_set():
            self._ready.wait()
            with self._pending_lock:
                self._ready.clear()
                if self._stop.is_set():
                    return
                if not self._pending:
                    continue
                name = next(iter(self._pending))
                value = self._pending.pop(name)
            try:
                self.emit(name, value)
            except Exception:
                with self._pending_lock:
                    if not self._stop.is_set():
                        self._pending.setdefault(name, value)
                self._stop.wait(self.interval)
            with self._pending_lock:
                if self._pending:
                    self._ready.set()

    def _activity(self) -> None:
        revision, disconnected = '', False
        while not self._stop.is_set():
            try:
                value = self.activity(revision)
                if value.get('changed'):
                    self._send('shaq:activity', value)
                    revision = value['revision']
                if disconnected:
                    self._send('shaq:activity-status', {'connected': True})
                disconnected = False
            except Exception:
                if not disconnected:
                    try:
                        self._send('shaq:activity-status', {'connected': False})
                    except Exception:
                        pass
                disconnected = True
            self._stop.wait(self.interval)

    def _updates(self) -> None:
        previous = None
        while not self._stop.is_set():
            try:
                value = self.updates()
                if value is not None:
                    signature = json.dumps(value, sort_keys=True)
                    if signature != previous:
                        self._send('shaq:software-update', value)
                        previous = signature
            except Exception:
                # Discovery has its own visible error state and retry policy.
                # Never turn an updater read failure into a research failure.
                pass
            self._stop.wait(self.interval)
