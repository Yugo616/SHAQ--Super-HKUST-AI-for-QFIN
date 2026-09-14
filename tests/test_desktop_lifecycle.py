import tempfile
import threading
import unittest
from pathlib import Path

from shaq_daily_oracle.desktop import desktop_api


class DesktopLifecycleTests(unittest.TestCase):
    def test_api_retains_bound_method_discovery_and_rpc_parameter_names(self):
        import inspect
        from shaq_daily_oracle.desktop import GuiRequestLifetime
        class Bridge:
            def resume(self, batch_id):
                return batch_id
        api = desktop_api(Bridge(), lifetime=GuiRequestLifetime())
        self.assertTrue(inspect.ismethod(api.resume), 'pywebview exports bound methods only')
        self.assertEqual(inspect.getfullargspec(api.resume).args[1:], ['batch_id'])
        self.assertEqual(api.resume('frozen-batch'), 'frozen-batch')

    def test_disposable_api_drains_admitted_writer_and_rejects_late_requests(self):
        from shaq_daily_oracle.desktop import GuiRequestLifetime
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'data'
            class Bridge:
                def write(self):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('test writer not released')
                    root.mkdir(exist_ok=True)
                    (root / 'admission.lock').touch()
                    return {'ok': True}
            lifetime = GuiRequestLifetime()
            api = desktop_api(Bridge(), lifetime=lifetime)
            writer = threading.Thread(target=api.write)
            writer.start()
            self.assertTrue(entered.wait(2))
            def close():
                lifetime.close()
                closed.set()
            closer = threading.Thread(target=close)
            closer.start()
            try:
                self.assertFalse(closed.wait(.05), 'cleanup overtook an admitted RPC')
            finally:
                release.set()
                writer.join(3)
                closer.join(3)
            self.assertTrue(closed.is_set())
            (root / 'admission.lock').unlink()
            root.rmdir()
            self.assertFalse(api.write()['ok'])
            self.assertFalse(root.exists(), 'late RPC recreated disposable data')

    def test_request_exception_releases_lifetime_without_exposing_private_methods(self):
        from shaq_daily_oracle.desktop import GuiRequestLifetime
        class Bridge:
            def fail(self):
                raise ValueError('fixture')
            def _private(self):
                pass
        lifetime = GuiRequestLifetime()
        api = desktop_api(Bridge(), lifetime=lifetime)
        with self.assertRaisesRegex(ValueError, 'fixture'):
            api.fail()
        self.assertFalse(hasattr(api, '_private'))
        lifetime.close()
        self.assertFalse(api.fail()['ok'])
