import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


class ReleaseDependenciesTests(unittest.TestCase):
    def module(self):
        packaging=Path(__file__).resolve().parents[1]/'packaging'
        self.assertTrue((packaging/'windows_release_dependencies.py').exists(), 'Release provenance verifier missing')
        with patch.object(sys,'path',[str(packaging),*sys.path]):
            spec=importlib.util.spec_from_file_location('release_dependencies',packaging/'windows_release_dependencies.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        app=root/'app';artifact=root/'artifact';native=artifact/'build/native-dependencies';third=artifact/'build/third-party'
        for directory in (app/'config',app/'packaging',app/'bundled_versions',native/'sources',native/'wheels',third/'MinGW-W64',third/'hdf5',artifact/'dist/diagnostic'):
            directory.mkdir(parents=True)
        def write(path,value):path.write_text(json.dumps(value))
        write(app/'config/team-repository.json',{'owner':'owner','repository':'repo'})
        (app/'packaging/requirements.lock.txt').write_text('quickjs==1.19.4\ntables==3.11.1\nbcolz-zipline==1.13.0\n')
        hashes={}
        for name in ('quickjs-1.19.4-cp313-cp313-win_amd64.whl','tables-3.11.1-cp311-abi3-win_amd64.whl','bcolz_zipline-1.13.0-cp313-cp313-win_amd64.whl'):
            (native/'wheels'/name).write_bytes(name.encode());hashes[name]=hashlib.sha256(name.encode()).hexdigest()
        write(native/'wheel-sha256.json',hashes)
        sources={}
        for name in ('quickjs','tables','bcolz-zipline','hdf5'):
            path=native/'sources'/(name+'.tar.gz')
            with tarfile.open(path,'w:gz') as tar:
                entry=tarfile.TarInfo(name+'/src/example.c');entry.size=1;tar.addfile(entry,io.BytesIO(b'x'))
            sources[name]={'url':'https://example.org/'+path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        write(app/'packaging/native-sources.json',sources);write(third/'source-manifest.json',sources)
        write(third/'manifest.json',{'source_sha':'a'*40,'source_dirty':False,'python':'3.13.15','architecture':'AMD64','methods':{},
            'distributions':[{'name':n,'version':v} for n,v in [('quickjs','1.19.4'),('tables','3.11.1'),('bcolz-zipline','1.13.0')]]})
        mingw={'compiler_sha256':'e'*64,'packages':{'compiler':{'version':'1'}}}
        write(native/'mingw-toolchain.json',mingw);write(third/'MinGW-W64/toolchain.json',mingw)
        mapping={'source_sha256':sources['hdf5']['sha256'],'recipe':'packaging/build_native.py:map_hdf_source_locations',
                 'transformation':'Prepend #line 1 with source-relative filename to extracted .c/.h build copies; original following bytes unchanged.', 'files':['src/example.c']}
        write(native/'hdf5-diagnostic-map.json',mapping);write(third/'hdf5/diagnostic-map.json',mapping)
        write(artifact/'dist/diagnostic/external-layout-source.json',{'application_sha':'a'*40,'validation_sha':'b'*40,
            'layout_sha256':'c'*64,'geometry_sha256':'d'*64,'display_preflight_sha256':'e'*64})
        run={'id':12,'head_sha':'b'*40,'path':'.github/workflows/build-desktop.yml','repository':{'id':34,'full_name':'owner/repo'}}
        meta={'id':56,'name':'SHAQ-Daily-Oracle-Lab-Windows-x64','expired':False,
              'workflow_run':{'id':12,'head_sha':'b'*40,'repository_id':34,'head_repository_id':34}}
        return app,artifact,run,meta

    def test_split_identity_requires_official_clean_app_manifest_and_all_native_metadata(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as name:
            app,artifact,run,meta=self.fixture(Path(name))
            with patch.object(module.subprocess,'check_output',side_effect=lambda cmd,**kw:'a'*40 if 'rev-parse' in cmd else ''):
                result=module.verify_release(app,artifact,run,meta,12,56)
                self.assertEqual(len(result['wheels']),3)
                self.assertEqual(result['application_sha'],'a'*40)
                for relative,key,value in [('dist/diagnostic/external-layout-source.json','validation_sha','f'*40),
                        ('build/third-party/manifest.json','source_dirty',True),
                        ('build/third-party/manifest.json','source_sha','f'*40),
                        ('build/native-dependencies/hdf5-diagnostic-map.json','source_sha256','f'*64),
                        ('build/native-dependencies/hdf5-diagnostic-map.json','files',['wrong.c']),
                        ('build/native-dependencies/mingw-toolchain.json','compiler_sha256','f'*64)]:
                    path=artifact/relative; original=path.read_bytes();data=json.loads(original);data[key]=value;path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):module.verify_release(app,artifact,run,meta,12,56)
                    path.write_bytes(original)
                for changed in ({**meta,'expired':True},{**meta,'workflow_run':{**meta['workflow_run'],'head_repository_id':99}}):
                    with self.assertRaises(ValueError):module.verify_release(app,artifact,run,changed,12,56)
                wheel=result['wheels'][0];wheel.write_bytes(b'corrupt')
                with self.assertRaises(ValueError):module.verify_release(app,artifact,run,meta,12,56)

    def test_restore_requires_identical_current_toolchain_and_copies_only_dependency_evidence(self):
        module=self.module()
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as name:
            app,artifact,run,meta=self.fixture(Path(name))
            with patch.object(module.subprocess,'check_output',side_effect=lambda cmd,**kw:'a'*40 if 'rev-parse' in cmd else ''):
                verified=module.verify_release(app,artifact,run,meta,12,56)
            loader=SimpleNamespace(exec_module=lambda builder:None)
            builder=SimpleNamespace(mingw_toolchain=lambda:(None,None,verified['mingw']))
            with patch.object(module.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=loader)), \
                    patch.object(module.importlib.util,'module_from_spec',return_value=builder), \
                    patch.object(module,'install_dependencies') as install:
                module.restore(app,verified)
                install.assert_called_once_with(app,verified['wheels'])
                self.assertEqual(len(list((app/'build/native-dependencies/wheels').glob('*.whl'))),3)
                self.assertFalse((app/'dist').exists())
                builder.mingw_toolchain=lambda:(None,None,{'different':'compiler'})
                with self.assertRaisesRegex(ValueError,'differs'):module.restore(app,verified)
