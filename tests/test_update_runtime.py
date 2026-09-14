import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from shaq_daily_oracle import software_updates as updates

ROOT = Path(__file__).parents[1]


class AdmissionTests(unittest.TestCase):
    def test_old_idle_worker_exits_even_after_new_app_clears_install_intent(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, WorkerAdmission, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            paths=NS(data_root=Path(directory),package_root=ROOT)
            gate=AdmissionGate(paths.data_root)
            version=['0.6.2']
            def installed(seconds):
                with gate.install():gate.mark_installing('0.7.0')
                version[0]='0.7.0';gate.finish_restart('0.7.0')
            with patch('shaq_daily_oracle.app_paths.application_version',side_effect=lambda root:version[0]):
                with WorkerAdmission(paths) as admission:
                    with patch('time.sleep',side_effect=installed):
                        with self.assertRaises(UpdateBusy):admission.pause(1)

    def test_old_version_directory_worker_detects_completed_update_history(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, WorkerAdmission, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            paths=NS(data_root=Path(directory),package_root=ROOT)
            gate=AdmissionGate(paths.data_root)
            def installed(seconds):
                with gate.install():gate.mark_installing('0.7.0')
                gate.finish_restart('0.7.0')
            with WorkerAdmission(paths) as admission:
                with patch('time.sleep',side_effect=installed):
                    with self.assertRaises(UpdateBusy):admission.pause(1)

    def test_real_worker_busy_blocks_install_but_idle_yields_and_exits_without_writes(self):
        from test_desktop_foundation import DesktopFoundationTests
        from shaq_daily_oracle import service
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        from shaq_daily_oracle.settings import SettingsStore
        from datetime import datetime, date, timezone
        with tempfile.TemporaryDirectory() as directory:
            paths=DesktopFoundationTests().paths(Path(directory));gate=AdmissionGate(paths.data_root)
            calls=[]
            def environment():
                with self.assertRaises(UpdateBusy):
                    with gate.install():pass
                calls.append('busy')
            def idle(seconds):
                with gate.install():gate.mark_installing('0.7.0')
                calls.append('idle-installed')
            following=NS(session_date=date(2026,9,15),market_open=datetime(2026,9,15,13,30,tzinfo=timezone.utc))
            with patch.object(SettingsStore,'load',return_value={'setup_complete':True,'automatic_run_enabled':True}),patch.object(SettingsStore,'apply_to_environment',side_effect=environment),patch.object(service,'market_session',return_value=None),patch.object(service,'next_market_session',return_value=following),patch('time.sleep',side_effect=idle):
                self.assertEqual(service.run_worker(paths=paths),0)
            self.assertEqual(calls,['busy','idle-installed'])
            self.assertEqual(json.loads((paths.runtime_root/'service_status.json').read_text())['state'],'market_closed')

    def test_worker_pause_releases_only_work_lease_and_exits_if_install_started(self):
        from shaq_daily_oracle import update_admission
        self.assertTrue(hasattr(update_admission,'WorkerAdmission'))
        with tempfile.TemporaryDirectory() as directory:
            gate=update_admission.AdmissionGate(Path(directory))
            paths=NS(data_root=Path(directory),package_root=ROOT)
            def install_while_idle(seconds):
                with gate.install():gate.mark_installing('0.7.0')
            with update_admission.WorkerAdmission(paths) as admission:
                with self.assertRaises(update_admission.UpdateBusy):
                    with gate.install():pass
                with patch('time.sleep',side_effect=install_while_idle):
                    with self.assertRaises(update_admission.UpdateBusy):admission.pause(1)

    def test_native_bridge_mutation_and_service_initialization_are_blocked(self):
        from shaq_daily_oracle.desktop import DesktopBridge
        from shaq_daily_oracle.lab_service import LabService
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            paths=NS(data_root=Path(directory),research_root=Path(directory)/'research')
            gate=AdmissionGate(Path(directory)); bridge=DesktopBridge.__new__(DesktopBridge);bridge.paths=paths
            with gate.install():gate.mark_installing('0.7.0')
            writes=[]
            result=bridge._result(lambda:writes.append('unsafe'))
            self.assertFalse(result['ok'])
            self.assertEqual(writes,[])
            with self.assertRaises(UpdateBusy):LabService(paths)

    def test_research_worker_is_blocked_before_loading_service(self):
        from shaq_daily_oracle.research_schedule import run_research_worker
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            paths=NS(data_root=Path(directory),research_root=Path(directory)/'research')
            gate=AdmissionGate(Path(directory))
            with gate.install():gate.mark_installing('0.7.0')
            with self.assertRaises(UpdateBusy):run_research_worker(paths)

    def test_pending_install_blocks_new_process_until_verified_restart(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            gate=AdmissionGate(Path(directory))
            self.assertTrue(hasattr(gate,'mark_installing'))
            with gate.install(): gate.mark_installing('0.7.0')
            with self.assertRaises(UpdateBusy):
                with AdmissionGate(Path(directory)).work():pass
            self.assertFalse(gate.finish_restart('0.6.2'))
            self.assertTrue(gate.finish_restart('0.7.0'))
            with gate.work():pass

    def test_background_thread_is_admitted_before_thread_start(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, start_guarded_thread, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            gate=AdmissionGate(Path(directory)); entered=threading.Event(); release=threading.Event()
            thread=threading.Thread(target=lambda:(entered.set(),release.wait(3)))
            start_guarded_thread(NS(data_root=Path(directory)),thread)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(UpdateBusy):
                    with gate.install():pass
            finally:release.set();thread.join(3)
            with gate.install():pass

    def test_other_process_work_blocks_apply_and_crashed_lease_is_reclaimed(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            gate = AdmissionGate(Path(directory))
            script = 'from pathlib import Path; from shaq_daily_oracle.update_admission import AdmissionGate; import sys; lease=AdmissionGate(Path(sys.argv[1])).work(); lease.__enter__(); print("ready",flush=True); sys.stdin.read()'
            child = subprocess.Popen([sys.executable, '-c', script, directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(UpdateBusy):
                    with gate.install(): pass
                child.terminate(); child.wait(timeout=5)
                with gate.install(): pass
            finally:
                if child.poll() is None: child.kill(); child.wait()
                child.stdin.close(); child.stdout.close()

    def test_install_blocks_new_work_without_serializing_parallel_work(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            first, second = AdmissionGate(Path(directory)), AdmissionGate(Path(directory))
            with first.work(), second.work():
                with self.assertRaises(UpdateBusy):
                    with first.install(): pass
            with first.install():
                errors = []
                def attempt():
                    try:
                        with second.work(): errors.append('unsafe')
                    except UpdateBusy: errors.append('blocked')
                thread = threading.Thread(target=attempt); thread.start(); thread.join(3)
                self.assertEqual(errors, ['blocked'])


def asset(kind='Full', **changes):
    return NS(PackageId='SHAQDailyOracleLab', Version='0.7.0', Type=kind,
              FileName=f'SHAQDailyOracleLab-0.7.0-osx-arm64-stable-{kind.lower()}.nupkg',
              SHA256='a'*64, SHA1='b'*40, Size=123, NotesMarkdown='Fix', NotesHtml='', **changes)


class UpdateRuntimeTests(unittest.TestCase):
    def test_managed_current_version_does_not_claim_legacy_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            selected.update(status='current',latest_version='0.6.2')
            result=self.check(runtime,selected,feed)
            self.assertEqual(result['mode'],'managed')
            self.assertNotIn('旧安装',result['message'])

    def test_feed_retains_prior_full_versions_without_allowing_wrong_target(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            prior={**vars(asset()),'Version':'0.6.2','FileName':'SHAQDailyOracleLab-0.6.2-osx-arm64-stable-full.nupkg'}
            feed['Assets'].append(prior)
            self.assertEqual(self.check(runtime,selected,feed)['latest_version'],'0.7.0')
            runtime,manager,selected,feed=self.runtime(directory)
            info=manager.check_for_updates();info.TargetFullRelease.Version='0.6.2';info.TargetFullRelease.FileName=prior['FileName']
            with self.assertRaises(ValueError):self.check(runtime,selected,feed)

    def test_managed_mac_portable_locator_can_update(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            manager.get_is_portable=lambda:True
            self.assertEqual(self.check(runtime,selected,feed)['mode'],'managed')

    def test_automatic_is_opt_in_and_waits_for_idle_then_records_real_restart(self):
        from shaq_daily_oracle.update_admission import AdmissionGate
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            self.assertTrue(hasattr(runtime,'automatic_step'))
            self.assertFalse(runtime.status()['automatic_enabled'])
            self.assertEqual(runtime.automatic_step()['status'],'unchecked')
            runtime.set_automatic(True)
            manager.download_updates=lambda info,progress:None
            applied=[];manager.apply_updates_and_restart=lambda info:applied.append(info)
            self.check(runtime,selected,feed)
            runtime.automatic_step();runtime._download_thread.join(3)
            with AdmissionGate(Path(directory)).work():
                state=runtime.automatic_step()
                self.assertTrue(state['waiting_for_idle'])
                self.assertEqual(applied,[])
            runtime.automatic_step();self.assertEqual(len(applied),1)
            self.assertIsNone(runtime.status()['last_update'])
            AdmissionGate(Path(directory)).finish_restart('0.7.0')
            history=updates.UpdateRuntime(runtime.paths).status()['last_update']
            self.assertEqual(history['version'],'0.7.0')
            self.assertEqual(history['method'],'automatic')
            self.assertTrue(history['completed_at'])

    def test_ambiguous_install_recovers_read_only_without_initializing_lab(self):
        from shaq_daily_oracle import software_updates
        from shaq_daily_oracle.update_admission import AdmissionGate
        with tempfile.TemporaryDirectory() as directory:
            runtime,_,_,_=self.runtime(directory)
            gate=AdmissionGate(Path(directory))
            with gate.install():gate.mark_installing('0.7.0')
            self.assertTrue(hasattr(software_updates,'UpdateRecovery'))
            recovery=software_updates.UpdateRecovery(runtime.paths)
            with patch.object(updates.platform,'system',return_value='Darwin'),patch.object(updates.platform,'machine',return_value='arm64'):
                status=recovery.status()
            self.assertEqual(status['current_version'],'0.6.2')
            self.assertEqual(status['target_version'],'0.7.0')
            self.assertTrue(status['installer_url'].endswith('/lab-v0.7.0-macos/SHAQ-Daily-Oracle-Lab-macOS-Apple-Silicon.dmg'))
            self.assertTrue((gate.root/'installing.json').exists())

    def test_failed_apply_releases_intent_and_keeps_download_ready(self):
        from shaq_daily_oracle.update_admission import AdmissionGate
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            manager.download_updates=lambda info,progress:None
            def fail(info):raise RuntimeError('launch rejected')
            manager.apply_updates_and_restart=fail
            self.check(runtime,selected,feed);runtime.download();runtime._download_thread.join(3)
            with self.assertRaises(RuntimeError):runtime.apply()
            self.assertEqual(runtime.status()['status'],'ready')
            with AdmissionGate(Path(directory)).work():pass

    def test_entry_calls_sdk_before_desktop_import_and_disables_auto_apply(self):
        import runpy
        events=[]
        class App:
            def set_auto_apply_on_startup(self,value):events.append(('auto',value));return self
            def on_restarted(self,callback):return self
            def run(self):events.append('lifecycle')
        fake=NS(App=App)
        with patch.dict(sys.modules,{'velopack':fake,'shaq_daily_oracle.desktop':NS(main=lambda:events.append('desktop'))}),patch.object(sys,'frozen',True,create=True):
            with self.assertRaises(SystemExit):runpy.run_path(str(ROOT/'packaging/desktop_entry.py'),run_name='__main__')
        self.assertEqual(events,[('auto',False),'lifecycle','desktop'])

    def runtime(self, directory, *, delta=False):
        paths=NS(package_root=ROOT, data_root=Path(directory), research_root=Path(directory)/'research')
        full=asset(); info=NS(TargetFullRelease=full, BaseRelease=None,
                              DeltasToTarget=[asset('Delta')] if delta else [], IsDowngrade=False)
        manager=NS(get_app_id=lambda:'SHAQDailyOracleLab', get_current_version=lambda:'0.6.2',
                   get_is_portable=lambda:False, check_for_updates=lambda:info)
        sdk=NS(UpdateOptions=lambda *args: args, HttpSource=lambda url:url,
               UpdateManager=lambda source,options:manager)
        runtime=updates.UpdateRuntime(paths, sdk=sdk)
        selected={'mode':'installer_only','status':'available','current_version':'0.6.2',
                  'latest_version':'0.7.0','asset_url':'https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/download/lab-v0.7.0-macos/installer.dmg',
                  'release_url':'https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos'}
        feed={'Assets':[vars(full), *[vars(x) for x in info.DeltasToTarget]]}
        return runtime, manager, selected, feed

    def check(self, runtime, selected, feed):
        with patch.object(updates,'check_releases',return_value=selected), patch.object(updates.platform,'system',return_value='Darwin'), patch.object(updates.platform,'machine',return_value='arm64'), patch.object(updates,'read_update_feed',return_value=feed):
            return runtime.check()

    def test_download_then_apply_uses_sdk_and_cross_process_gate(self):
        from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
        with tempfile.TemporaryDirectory() as directory:
            runtime, manager, selected, feed=self.runtime(directory)
            manager.download_updates=lambda info,progress:progress(100)
            applied=[]; manager.apply_updates_and_restart=lambda info:applied.append(info)
            self.assertEqual(self.check(runtime,selected,feed)['mode'],'managed')
            runtime.download(); runtime._download_thread.join(3)
            self.assertEqual(runtime.status()['status'],'ready')
            with AdmissionGate(Path(directory)).work():
                with self.assertRaises(UpdateBusy):runtime.apply()
            self.assertEqual(applied,[])
            runtime.apply(); self.assertEqual(len(applied),1)

    def test_bad_digest_filename_package_version_rejected_before_sdk_download(self):
        with tempfile.TemporaryDirectory() as directory:
            for key,value in [('SHA256','bad'),('FileName','../evil.nupkg'),('PackageId','other'),('Version','0.5.0')]:
                runtime,manager,selected,feed=self.runtime(directory)
                feed['Assets'][0][key]=value
                with self.assertRaises(ValueError):self.check(runtime,selected,feed)

    def test_failed_payload_never_becomes_ready_or_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory,delta=True)
            def broken(info,progress): raise RuntimeError('checksum mismatch')
            manager.download_updates=broken
            self.check(runtime,selected,feed); runtime.download();runtime._download_thread.join(3)
            self.assertEqual(runtime.status()['status'],'download_failed')
            self.assertIn('完整',runtime.status()['message'])
            with self.assertRaises(ValueError): runtime.apply()

    def test_unmanaged_install_is_explicit_bridge_not_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime,manager,selected,feed=self.runtime(directory)
            runtime.sdk=NS(UpdateOptions=lambda *args:args,HttpSource=lambda url:url,
                           UpdateManager=lambda *args:(_ for _ in ()).throw(RuntimeError('not installed')))
            self.assertEqual(self.check(runtime,selected,feed)['mode'],'installer_only')
            with self.assertRaises(ValueError):runtime.download()
