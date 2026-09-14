from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider


class CollectionWorkerTests(unittest.TestCase):
    def test_explicit_deadline_setting_can_pass_existing_readiness_flow(self):
        from shaq_daily_oracle.research_settings import ResearchSettingsStore
        import test_research_lab_foundation as foundation
        with tempfile.TemporaryDirectory() as name:
            store = ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(name)))
            settings = store.load()
            settings.update({'github_login':'fixture','sec_identity':'Fixture fixture@example.invalid',
                'active_model_profile_id':'fixture', 'model_profiles':[{
                    'profile_id':'fixture','protocol':'codex-cli','model':'fixture',
                    'base_url':'codex','auth_style':'bearer'}]})
            settings['credential_state']['github_token_saved'] = True
            settings['data_profile']['yahoo_worker_timeout_seconds'] = 1200
            store._save(settings)
            profile = DataProfile.from_dict(settings['data_profile'])
            receipt = {'status':'ready','data_profile_sha256':profile.identity(),
                'checked_at':'2026-09-14T08:00:00-04:00','universe_members':500,
                'free_bytes':1000000,'storage_writable':True}
            store.save_research_readiness(receipt)
            self.assertTrue(store.load()['setup_complete'])
            self.assertEqual(store.load()['data_profile']['yahoo_worker_timeout_seconds'],1200)

    def test_worker_deadline_does_not_change_source_or_manifest_identity(self):
        from dataclasses import replace
        from zoneinfo import ZoneInfo
        from shaq_daily_oracle.data_providers import provider_manifest
        profile = DataProfile('test','unused')
        longer = replace(profile, yahoo_worker_timeout_seconds=1200)
        self.assertEqual(profile.identity(), longer.identity())
        with tempfile.TemporaryDirectory() as name:
            universe = Path(name)/'universe.csv'; universe.write_text('symbol\nAAA\n')
            kwargs = {'universe_path':universe,
                      'cutoff':datetime(2026,9,14,8,50,tzinfo=ZoneInfo('America/New_York'))}
            self.assertEqual(provider_manifest(profile=profile, **kwargs),
                             provider_manifest(profile=longer, **kwargs))
        with self.assertRaises(DataProviderError):
            replace(profile, yahoo_worker_timeout_seconds=0).validate()

    def test_yahoo_history_does_not_import_dependency_in_parent(self):
        self.assertTrue(hasattr(YFinanceProvider, '_history_inline'),
                        'Yahoo history needs a child-only implementation')
        from shaq_daily_oracle import collection_worker
        captured = []
        def execute(command, **kwargs):
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, '{"result":{"AAA":[]}}', '')
        profile = DataProfile('fixture', 'unused', yahoo_worker_timeout_seconds=17)
        with patch.object(collection_worker, 'run_model_process', side_effect=execute), \
             patch.object(YFinanceProvider, '_module', side_effect=AssertionError('parent Yahoo import')):
            provider = YFinanceProvider(profile)
            for method in [provider.history, provider.fresh_history]:
                self.assertEqual(method(['AAA'], start=date(2026,9,1), end=date(2026,9,2)), {'AAA':[]})
        self.assertEqual(len(captured), 2)
        for command, kwargs in captured:
            self.assertIn('shaq_daily_oracle.collection_worker', command)
            self.assertEqual(kwargs['timeout'], 17)
            self.assertNotIn('SEC_IDENTITY', kwargs['env'])
            request = json.loads(kwargs['input'])
            self.assertEqual(request['payload']['start'], '2026-09-01')

    def test_parent_owns_scratch_cleanup_even_when_child_crashes(self):
        from shaq_daily_oracle import collection_worker
        scratch = []
        def crash(command, **kwargs):
            request = json.loads(kwargs['input'])
            self.assertIn('cache_parent', request['payload'])
            root = Path(request['payload']['cache_parent'])
            self.assertTrue(root.is_dir())
            (root/'partial-cache').write_text('fixture')
            scratch.append(root)
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(collection_worker, 'run_model_process', side_effect=crash):
            with self.assertRaises(DataProviderError):
                YFinanceProvider(DataProfile('test','unused')).history(
                    ['AAA'], start=date(2026,9,1),end=date(2026,9,2))
        self.assertFalse(scratch[0].exists())

    def test_worker_failures_are_sanitized_and_never_empty_success(self):
        self.assertTrue(hasattr(YFinanceProvider, '_history_inline'))
        from shaq_daily_oracle import collection_worker
        cases = [OSError(24, 'secret identity'), subprocess.TimeoutExpired('secret', 1),
                 subprocess.CompletedProcess([], 1, '', 'secret identity'),
                 subprocess.CompletedProcess([], 0, 'garbage secret', ''),
                 subprocess.CompletedProcess([], 0, '{"error":"secret identity"}', '')]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                kwargs = {'side_effect':case} if isinstance(case, Exception) else {'return_value':case}
                with patch.object(collection_worker, 'run_model_process', **kwargs):
                    with self.assertRaises(DataProviderError) as caught:
                        YFinanceProvider(DataProfile('test','unused')).history(
                            ['AAA'],start=date(2026,9,1),end=date(2026,9,2))
                self.assertNotIn('secret', str(caught.exception))

    def test_worker_failure_diagnostic_preserves_resource_reason_not_secrets(self):
        from shaq_daily_oracle import collection_worker
        completed = subprocess.CompletedProcess([], 0, json.dumps({'error': 'collection_failed',
            'diagnostic': {'kind':'resource_exhausted','errno':24,'error_type':'OSError','stage':'history'}}), '')
        with patch.object(collection_worker, 'run_model_process', return_value=completed):
            with self.assertRaises(DataProviderError) as caught:
                YFinanceProvider(DataProfile('test','unused')).history(
                    ['AAA'],start=date(2026,9,1),end=date(2026,9,2))
        self.assertIn('resource', str(caught.exception))
        self.assertEqual(caught.exception.diagnostic['errno'], 24)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX low descriptor limit regression')
    def test_repeated_full_universe_uses_one_thread_real_cache_and_closes_resources(self):
        self.assertTrue(hasattr(YFinanceProvider, '_history_inline'))
        # Only HTTP is replaced. The installed Yahoo/Peewee cache and curl session
        # are real, with a fresh subprocess and no access to the user's cache.
        script = r'''
import resource, fcntl, json, threading
from unittest.mock import patch
import yfinance as yf
import pandas as pd
from shaq_daily_oracle.collection_worker import execute_operation
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(128, hard), hard))
def descriptors():
    count=0
    for fd in range(128):
        try: fcntl.fcntl(fd, fcntl.F_GETFD); count+=1
        except OSError: pass
    return count
baseline=descriptors(); samples=[]; threads=set(); sessions=[]; caches=[]
real_download=yf.download
def history(ticker, *args, **kwargs):
    cache=yf.cache.get_tz_cache()
    cache.store(ticker.ticker, 'UTC')
    caches.append(cache)
    return pd.DataFrame()
def download(**kwargs):
    assert kwargs['threads'] is False
    threads.add(threading.get_ident()); sessions.append(kwargs['session'])
    result=real_download(**kwargs)
    samples.append(descriptors())
    return result
with patch.object(yf, 'download', side_effect=download), patch.object(yf.Ticker, 'history', history):
    result=execute_operation('history', {
        'profile':{'profile_id':'test','universe_file':'unused','batch_size':80},
        'symbols':['FIX'+str(i) for i in range(560)],
        'start':'2026-09-01','end':'2026-09-02','interval':'1d','prepost':False})
assert len(result)==560 and all(rows==[] for rows in result.values())
assert len(samples)==7 and len(threads)==1
assert max(samples)-baseline < 15, (baseline, samples)
assert all(cache.db.is_closed() for cache in caches)
assert all(session._closed for session in sessions)
assert descriptors() <= baseline+2, (baseline, descriptors())
print(json.dumps({'batches':len(samples),'baseline':baseline,'peak':max(samples),'final':descriptors()}))
'''
        completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=40)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['batches'], 7)

    def test_frozen_entry_dispatches_worker_before_gui_or_updater(self):
        self.assertTrue(hasattr(YFinanceProvider, '_history_inline'))
        import runpy
        from types import SimpleNamespace
        entry = Path(__file__).resolve().parents[1] / 'packaging/desktop_entry.py'
        with patch.object(sys, 'argv', ['SHAQ.exe', '--collection-worker']), \
             patch.object(sys, 'frozen', True, create=True), \
             patch.dict(sys.modules, {'shaq_daily_oracle.collection_worker':SimpleNamespace(main=lambda:23),
                                      'velopack':None,'shaq_daily_oracle.desktop':None}):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_path(str(entry), run_name='__main__')
        self.assertEqual(exit.exception.code, 23)

    def test_source_ping_and_windowless_entry_roundtrip_have_no_provider_side_effects(self):
        from shaq_daily_oracle.model_execution import run_model_process
        entry = Path(__file__).resolve().parents[1] / 'packaging/desktop_entry.py'
        commands = [[sys.executable, '-m', 'shaq_daily_oracle.collection_worker'],
                    [sys.executable, '-c',
                     'import sys,runpy;sys.frozen=True;sys.stdin=sys.stdout=sys.stderr=None;'
                     f'sys.argv=[{str(entry)!r},"--collection-worker"];'
                     f'runpy.run_path({str(entry)!r},run_name="__main__")']]
        # Windows frozen GUI inherited handles are covered by native Task3.
        if sys.platform == 'win32':
            commands = commands[:1]
        for command in commands:
            completed = run_model_process(command, input='{"operation":"ping","payload":{}}',
                capture_output=True,text=True,timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {'result':{
                'worker':'yahoo-collection','protocol_version':1}})

    @unittest.skipIf(sys.platform == 'win32', 'POSIX descriptor regression')
    def test_repeated_real_protocol_preserves_rows_and_parent_handles(self):
        import fcntl
        from shaq_daily_oracle import collection_worker
        from shaq_daily_oracle.model_execution import run_model_process
        script = r'''
import os, resource
import yfinance as yf
import pandas as pd
from unittest.mock import patch
from shaq_daily_oracle.collection_worker import main
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(min(128,hard),hard))
def download(**kw):
    assert kw['threads'] is False
    assert kw['session'] is not None
    assert kw['start']=='2026-09-01' and kw['end']=='2026-09-02'
    for ticker in kw['tickers']: yf.cache.get_tz_cache().store(ticker, 'UTC')
    return pd.concat({ticker:pd.DataFrame({'Open':[101.5], 'Close':[float('nan')],
        'Volume':[os.getpid()]}, index=[pd.Timestamp('2026-09-01T16:00:00-04:00')])
        for ticker in kw['tickers']},axis=1)
with patch.object(yf,'download',side_effect=download): raise SystemExit(main())
'''
        def fds():
            result = 0
            for fd in range(1024):
                try: fcntl.fcntl(fd, fcntl.F_GETFD); result += 1
                except OSError: pass
            return result
        def fixture_child(command, **kwargs):
            return run_model_process([sys.executable, '-c', script], **kwargs)
        before = fds(); pids = set()
        provider = YFinanceProvider(DataProfile('test','unused'))
        with patch.object(collection_worker, 'run_model_process', side_effect=fixture_child):
            for method in [provider.history, provider.fresh_history, provider.fresh_history]:
                rows = method(['FIX'+str(i) for i in range(560)], start=date(2026,9,1),end=date(2026,9,2))
                self.assertEqual(len(rows), 560)
                row = rows['FIX0'][0]
                self.assertEqual(row['timestamp'], '2026-09-01T16:00:00-04:00')
                self.assertEqual(row['open'], 101.5)
                self.assertIsNone(row['close'])
                pids.add(row['volume'])
                self.assertLessEqual(fds(), before + 1)
        self.assertEqual(len(pids), 3, 'verification reads must not reuse a Yahoo process cache')


if __name__ == '__main__':
    unittest.main()
