import importlib.util
from pathlib import Path
import tempfile
import unittest


class ExternalTestsIdentityTests(unittest.TestCase):
    def test_test_only_change_allowed_but_runtime_change_or_extra_file_rejected(self):
        path=Path(__file__).resolve().parents[1]/'packaging/validation_tests.py'
        spec=importlib.util.spec_from_file_location('validation_tests',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            roots=[Path(directory)/name for name in ('application','validation')]
            for root in roots:
                for name,content in {'src/app.py':'same runtime','tests/test_app.py':'original',
                                     'pyproject.toml':'same metadata','packaging/requirements.lock.txt':'same pins'}.items():
                    file=root/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(content)
            application,validation=roots
            (validation/'tests/test_app.py').write_text('corrected platform fixture')
            self.assertTrue(module.verify_runtime(application,validation))
            (validation/'src/app.py').write_text('changed runtime')
            with self.assertRaises(ValueError):module.verify_runtime(application,validation)
            (validation/'src/app.py').write_text('same runtime')
            (validation/'src/extra.py').write_text('extra runtime')
            with self.assertRaises(ValueError):module.verify_runtime(application,validation)
