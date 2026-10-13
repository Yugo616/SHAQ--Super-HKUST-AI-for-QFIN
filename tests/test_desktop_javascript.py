import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shaq_daily_oracle.desktop_javascript import bind_javascript_lifetime


class ClosedEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class DesktopJavascriptTests(unittest.TestCase):
    def window(self, evaluate):
        window = SimpleNamespace(evaluate_js=evaluate,
                                 events=SimpleNamespace(closed=ClosedEvent()))
        bind_javascript_lifetime(window)
        return window

    def test_open_window_preserves_arguments_callback_and_result(self):
        calls = []
        result = object()
        callback = lambda value: value

        def evaluate(*args, **kwargs):
            calls.append((args, kwargs))
            return result

        window = self.window(evaluate)
        self.assertIs(window.evaluate_js('script', callback=callback), result)
        self.assertEqual(calls, [(('script',), {'callback': callback})])

    def test_open_window_propagates_original_exception(self):
        error = ValueError('native evaluation failed')

        def evaluate(script):
            raise error

        window = self.window(evaluate)
        with self.assertRaises(ValueError) as caught:
            window.evaluate_js('script')
        self.assertIs(caught.exception, error)

    def test_closed_window_releases_all_native_waits_and_ignores_late_results(self):
        entered = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]
        release = threading.Event()
        results = {}
        renderer_daemons = []

        def evaluate(index):
            renderer_daemons.append(threading.current_thread().daemon)
            entered[index].set()
            try:
                if not release.wait(5):
                    raise RuntimeError('test did not release native rendering')
                return 'late result'
            finally:
                finished[index].set()

        window = self.window(evaluate)
        callers = [threading.Thread(target=lambda i=i: results.update({i: window.evaluate_js(i)}))
                   for i in range(2)]
        try:
            for caller in callers:
                caller.start()
            for event in entered:
                self.assertTrue(event.wait(3))
            window.events.closed.fire()
            window.events.closed.fire()  # Closing more than once is harmless.
            for caller in callers:
                caller.join(3)
                self.assertFalse(caller.is_alive(), 'native rendering blocked process shutdown')
            self.assertEqual(results, {0: None, 1: None})
            self.assertEqual(renderer_daemons, [True, True])
        finally:
            release.set()
            for caller in callers:
                caller.join(3)
        for event in finished:
            self.assertTrue(event.wait(3))
        self.assertEqual(results, {0: None, 1: None})

    def test_call_after_close_never_starts_native_evaluation(self):
        calls = []
        window = self.window(lambda script: calls.append(script))
        window.events.closed.fire()
        self.assertIsNone(window.evaluate_js('script'))
        self.assertEqual(calls, [])

    def test_update_recovery_window_also_stops_evaluating_after_close(self):
        from shaq_daily_oracle.software_updates import launch_update_recovery
        calls = []
        window = SimpleNamespace(evaluate_js=lambda script: calls.append(script),
                                 events=SimpleNamespace(closed=ClosedEvent()))

        def start():
            window.evaluate_js('visible response')
            window.events.closed.fire()
            window.evaluate_js('late response')

        webview = SimpleNamespace(create_window=lambda *args, **kwargs: window, start=start)
        with patch.dict('sys.modules', webview=webview):
            self.assertEqual(launch_update_recovery(SimpleNamespace()), 0)
        self.assertEqual(calls, ['visible response'])

    def test_close_preserves_admitted_rpc_write_before_discarding_its_ui_response(self):
        entered, release = threading.Event(), threading.Event()
        native_calls = []
        results = []
        window = self.window(lambda script: native_calls.append(script))
        with tempfile.TemporaryDirectory() as name:
            sentinel = Path(name) / 'completed.txt'

            def rpc():
                entered.set()
                if not release.wait(5):
                    return
                sentinel.write_text('saved')
                results.append(window.evaluate_js('deliver response'))

            worker = threading.Thread(target=rpc)
            try:
                worker.start()
                self.assertTrue(entered.wait(3))
                window.events.closed.fire()
                self.assertTrue(worker.is_alive())
                self.assertFalse(worker.daemon)
                self.assertFalse(sentinel.exists())
                release.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(sentinel.read_text(), 'saved')
                self.assertEqual(results, [None])
                self.assertEqual(native_calls, [])
            finally:
                release.set()
                worker.join(3)
