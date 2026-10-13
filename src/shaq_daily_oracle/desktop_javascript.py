"""Keep native rendering waits within the lifetime of their desktop window.

Some webview backends stop delivering JavaScript completions when a window
closes. RPC work must still finish, but its final UI response has no recipient.
Only native rendering runs on daemon threads; API computation stays on its
original thread and is never cancelled by this adapter.
"""
from dataclasses import dataclass, field
import threading
from typing import Any


@dataclass(eq=False)
class _Evaluation:
    ready: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False


class _WindowJavascript:
    def __init__(self, evaluate):
        self._evaluate = evaluate
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[_Evaluation] = set()

    def close(self):
        with self._lock:
            self._closed = True
            for request in self._pending:
                request.cancelled = True
                request.ready.set()
            self._pending.clear()

    def __call__(self, *args, **kwargs):
        request = _Evaluation()
        with self._lock:
            if self._closed:
                return None
            self._pending.add(request)
        try:
            threading.Thread(target=self._render, args=(request, args, kwargs),
                             name='shaq-window-javascript', daemon=True).start()
        except BaseException:
            with self._lock:
                self._pending.discard(request)
            raise
        request.ready.wait()
        with self._lock:
            if request.cancelled:
                return None
            if request.error is not None:
                raise request.error
            return request.result

    def _render(self, request, args, kwargs):
        with self._lock:
            if request not in self._pending:
                return
        result, error = None, None
        try:
            result = self._evaluate(*args, **kwargs)
        except BaseException as exc:
            error = exc
        with self._lock:
            # A native completion arriving after close cannot revive a request.
            if request in self._pending:
                request.result, request.error = result, error
                self._pending.remove(request)
                request.ready.set()


def bind_javascript_lifetime(window):
    """Install before webview.start, so the first ready RPC is also covered."""
    javascript = _WindowJavascript(window.evaluate_js)
    window.evaluate_js = javascript
    window.events.closed += javascript.close
