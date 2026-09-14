import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class LayoutDependenciesTests(unittest.TestCase):
    def test_official_artifact_and_all_wheel_hashes_bind_to_application_commit(self):
        path = Path(__file__).resolve().parents[1] / 'packaging/windows_layout_dependencies.py'
        self.assertTrue(path.is_file())
        spec = importlib.util.spec_from_file_location('windows_layout_dependencies', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root/'config').mkdir(); (root/'packaging').mkdir()
            (root/'config/team-repository.json').write_text('{"owner":"owner","repository":"repo"}')
            (root/'packaging/requirements.lock.txt').write_text('tables==3.11.1\nquickjs==1.19.4\nbcolz-zipline==1.13.0\n')
            wheels = root/'artifact/build/native-dependencies/wheels'; wheels.mkdir(parents=True)
            hashes = {}
            for wheel in ('tables-3.11.1-cp311-abi3-win_amd64.whl', 'quickjs-1.19.4-cp313-cp313-win_amd64.whl',
                          'bcolz_zipline-1.13.0-cp313-cp313-win_amd64.whl'):
                (wheels/wheel).write_bytes(wheel.encode()); hashes[wheel]=hashlib.sha256(wheel.encode()).hexdigest()
            (wheels.parent/'wheel-sha256.json').write_text(json.dumps(hashes))
            run = {'id':12,'head_sha':'a'*40,'path':'.github/workflows/build-desktop.yml',
                   'repository':{'id':34,'full_name':'owner/repo'}}
            artifact = {'id':56,'expired':False,'name':'SHAQ-Daily-Oracle-Lab-Windows-x64',
                        'workflow_run':{'id':12,'head_sha':'a'*40,'repository_id':34,'head_repository_id':34}}
            with patch.object(module.subprocess,'check_output',return_value='a'*40+'\n'):
                self.assertEqual(len(module.verify(root,root/'artifact',run,artifact,12,56)),3)
                for altered in ({**artifact,'expired':True}, {**artifact,'id':57},
                    {**artifact,'workflow_run':{**artifact['workflow_run'],'head_sha':'b'*40}},
                    {**artifact,'workflow_run':{**artifact['workflow_run'],'head_repository_id':35}}):
                    with self.assertRaises(ValueError): module.verify(root,root/'artifact',run,altered,12,56)
                with self.assertRaises(ValueError):
                    module.verify(root,root/'artifact',{**run,'path':'.github/workflows/other.yml'},artifact,12,56)
                (wheels/next(iter(hashes))).write_bytes(b'corrupt')
                with self.assertRaises(ValueError): module.verify(root,root/'artifact',run,artifact,12,56)
