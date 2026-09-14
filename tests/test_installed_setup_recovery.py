import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import threading
import time
from unittest.mock import patch


class InstalledSetupRecoveryTests(unittest.TestCase):
    def test_driver_is_external_opt_in_and_only_first_transition_can_retry(self):
        source=(Path(__file__).resolve().parents[1]/'packaging/installed_update_acceptance.py').read_text()
        self.assertIn("parser.add_argument('--application',type=Path)",source)
        self.assertIn('args.recover_initial_admission and number == 1',source)
        self.assertIn('Path(__file__).resolve().is_relative_to(project)',source)
        self.assertIn("'setup_recoveries':recoveries",source)

    def module(self):
        path = Path(__file__).resolve().parents[1]/'packaging/installed_setup_recovery.py'
        self.assertTrue(path.exists(), 'External bounded setup recovery is missing')
        spec = importlib.util.spec_from_file_location('setup_recovery', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        events=root/'events-1'; events.mkdir()
        values={
            'peer-result.json':{'status':'failed','stage':'peer','exception_type':'UpdateBusy',
                'traceback':'Traceback (most recent call last):\n  File "shaq_daily_oracle\\update_smoke.py", line 241, in inspect\n  File "shaq_daily_oracle\\update_admission.py", line 40, in _admission\nshaq_daily_oracle.update_admission.UpdateBusy: busy\n',
                'pid':22,'preserved_hashes':True,'pages':['run','editor','history'],'actual_version':'0.6.98'},
            'bridge-result.json':{'status':'failed','stage':'bridge','exception_type':'TimeoutError',
                'error':'Missing installed acceptance event: analysis-held.json','waiting_stages':[],
                'pid':11,'peer_pid':22,'preserved_hashes':True,'pages':['run','editor','history'],
                'download_verified':True,'actual_version':'0.6.98'},
            'old-processes.json':{'status':'passed','pids':[11,22]}}
        for name,value in values.items(): (events/name).write_text(json.dumps(value))
        (root/'data/update-admission').mkdir(parents=True)
        (root/'installed').mkdir(); (root/'installed/app.exe').write_bytes(b'actual app')
        return events, values

    def test_exact_preapply_failure_requires_dead_owners_preserved_bytes_and_recovered_gate(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); events,values=self.fixture(root)
            before=module.file_hashes(root/'installed')
            calls=[]
            evidence=module.verify_setup_recovery(root,events,'0.6.98',before,{},
                process_running=lambda pid:False, protected_hashes=lambda:{'record':'hash'},
                expected_hashes={'record':'hash'}, admission_probe=lambda:calls.append('actual probe'))
            self.assertEqual(calls,['actual probe'])
            self.assertTrue(evidence['recovered_admission'])
            for filename,change in [('peer-result.json',{'exception_type':'StaleRuntime'}),
                                    ('peer-result.json',{'traceback':'other UpdateBusy'}),
                                    ('bridge-result.json',{'waiting_stages':['analysis']}),
                                    ('bridge-result.json',{'preserved_hashes':False})]:
                original=values[filename]
                (events/filename).write_text(json.dumps({**original,**change}))
                with self.assertRaises(RuntimeError):
                    module.verify_setup_recovery(root,events,'0.6.98',before,{},process_running=lambda p:False,
                        protected_hashes=lambda:{'record':'hash'},expected_hashes={'record':'hash'},admission_probe=lambda:None)
                (events/filename).write_text(json.dumps(original))

    def test_live_owner_transition_intent_changed_data_and_program_reject(self):
        module=self.module()
        for failure in ('live','intent','event','data','program','probe','history','request'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as name:
                root=Path(name);events,_=self.fixture(root); before=module.file_hashes(root/'installed')
                if failure=='intent': (root/'data/update-admission/installing.json').write_text('{}')
                if failure=='event': (events/'analysis-held.json').write_text('{}')
                if failure=='program': (root/'installed/app.exe').write_bytes(b'changed')
                if failure=='history': (root/'data/software-update-history.json').write_text('{}')
                if failure=='request':
                    request=root/'data/update-admission/gui-sessions';request.mkdir();(request/'request.json').write_text('{}')
                def probe():
                    if failure=='probe': raise TimeoutError('persistent admission')
                with self.assertRaises((RuntimeError,TimeoutError)):
                    module.verify_setup_recovery(root,events,'0.6.98',before,{},
                        process_running=lambda pid:failure=='live', protected_hashes=lambda:{'record':failure if failure=='data' else 'hash'},
                        expected_hashes={'record':'hash'}, admission_probe=probe)

    def test_real_admission_probe_recovers_only_after_lock_release_and_bounds_persistent_intent(self):
        module=self.module()
        from shaq_daily_oracle.update_admission import AdmissionGate
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);gate=AdmissionGate(root/'data');held=threading.Event()
            def hold():
                with gate._admission():held.set();time.sleep(1.25)
            thread=threading.Thread(target=hold);thread.start();held.wait()
            try:
                module.probe_admission(root,timeout=3)
            finally:thread.join()
            (gate.root/'installing.json').write_text('{}')
            with self.assertRaisesRegex(TimeoutError,'did not recover'):
                module.probe_admission(root,timeout=.1)

    def test_one_retry_uses_fresh_events_keeps_first_failure_and_never_retries_second(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);events,_=self.fixture(root)
            config=root/'acceptance.json';config.write_text(json.dumps({'events_directory':'events-1'}))
            calls=[]; recoveries=[]
            def run(label, command, timeout):
                calls.append(label)
                raise RuntimeError('stage failed')
            with self.assertRaisesRegex(RuntimeError,'stage failed'):
                module.run_with_setup_recovery(run,'installed-bridge-1',['actual.exe'],config,
                    lambda:{'recovered_admission':True},recoveries)
            self.assertEqual(calls,['installed-bridge-1','installed-bridge-1-retry-1'])
            self.assertTrue((events/'peer-result.json').exists())
            self.assertEqual(json.loads(config.read_text())['events_directory'],'events-1-retry-1')
            self.assertEqual(len(recoveries),1)
            self.assertTrue((root/'setup-recovery-events-1.json').exists())
            self.assertTrue(all(path.is_dir() for path in root.glob('events-*')))
