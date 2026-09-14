import importlib.util
from pathlib import Path
import sys
import unittest


class WorkerProtocolAcceptanceTests(unittest.TestCase):
    def test_real_entrypoint_roundtrips_both_workers_without_gui_or_provider(self):
        root = Path(__file__).resolve().parents[1]
        script = root / 'packaging/worker_protocol_acceptance.py'
        self.assertTrue(script.is_file(), 'installed acceptance needs actual worker pipe roundtrips')
        spec = importlib.util.spec_from_file_location('worker_protocol_acceptance', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.verify_workers([sys.executable, str(root / 'packaging/desktop_entry.py')])
        self.assertEqual(result, {'model_http_worker': True, 'collection_worker': True})
