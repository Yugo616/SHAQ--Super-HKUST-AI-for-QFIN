import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_result_refresh
from shaq_daily_oracle.lab_service import LabService, ET


class ResultRecoveryTests(unittest.TestCase):
    service = test_result_refresh.ResultRefreshTests.service
    wait_status = test_result_refresh.ResultRefreshTests.wait_status

    def test_windows_refresh_read_retries_publication_denial_without_fake_idle(self):
        import errno
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            denial = PermissionError(errno.EACCES, 'publication window')
            with patch('shaq_daily_oracle.settings.sys.platform', 'win32'), patch('shaq_daily_oracle.settings.time.sleep'), patch.object(Path, 'read_text', side_effect=[denial, '{"status":"complete"}']) as read:
                self.assertEqual(service.result_refresh_status()['status'], 'complete')
                self.assertEqual(read.call_count, 2)

    def test_refresh_read_permanent_denial_is_bounded_not_success_or_idle(self):
        import errno
        from shaq_daily_oracle.settings import WINDOWS_REPLACE_RETRY_DELAYS
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            with patch('shaq_daily_oracle.settings.sys.platform', 'win32'), patch('shaq_daily_oracle.settings.time.sleep'), patch.object(Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'denied')) as read:
                with self.assertRaises(PermissionError):
                    service.result_refresh_status()
                self.assertEqual(read.call_count, len(WINDOWS_REPLACE_RETRY_DELAYS)+1)

    def test_targeted_minute_retry_does_not_refresh_labels_or_other_dates(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            self.prior(service)
            calls = []
            def minutes(*args, **kwargs):
                calls.append(kwargs['eligible_dates'])
                return {'refreshed_dates':['2026-09-11'], 'failures':[]}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=AssertionError('daily labels must not run')), \
                 patch.object(LabService, '_refresh_minute_accounts', side_effect=minutes):
                service.start_result_refresh(manual=True, minute_only_date='2026-09-11')
                service._owned_result_refresh[1].join(3)
            result = service.result_refresh_status()
            self.assertEqual(calls, [{'2026-09-11'}])
            self.assertEqual(result['result']['minute_settlement']['failures'], [])
            self.assertEqual(result['result']['failures'][0]['batch_id'], 'LAB-failed')

    def test_targeted_minute_retry_rejects_invalid_dates_before_starting(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            with self.assertRaises(ValueError):
                service.start_result_refresh(manual=True, minute_only_date='../bad')
            self.assertFalse(service._result_refresh_receipt.exists())
    def test_one_provider_outage_does_not_send_five_identical_refresh_requests(self):
        from shaq_daily_oracle import lab_service
        from shaq_daily_oracle.data_providers import DataProfile, DataProviderError
        self.assertTrue(hasattr(lab_service, '_RefreshMarket'))
        provider = lab_service._RefreshMarket(DataProfile('fixture', 'unused'))
        calls = []
        def offline(*args, **kwargs):
            calls.append(args)
            raise DataProviderError('连接中断', diagnostic={'kind':'connection_error'})
        with patch.object(provider.provider, 'fresh_history', side_effect=offline):
            for _ in range(5):
                with self.assertRaises(DataProviderError):
                    provider.fresh_history(['AAA'])
        self.assertEqual(len(calls), 1)

    def prior(self, service, *, retryable=False):
        value = {'status': 'partial_failure', 'operation_id': 'old', 'result': {
            'refreshed_batches': ['LAB-good'], 'failures': [{
                'batch_id': 'LAB-failed', 'error_type': 'DataProviderError',
                'message': 'connection failed', 'diagnostic': {
                    'kind': 'connection_error', 'retryable': retryable}}],
            'minute_settlement': {'refreshed_dates': ['2026-09-09'], 'failures': [{
                'trade_date': '2026-09-11', 'error_type': 'UnavailableMinuteTargets',
                'message': 'missing minute'}]}}, 'failure_count': 2}
        service._result_refresh_receipt.write_text(json.dumps(value))
        return value

    def test_retry_selects_failed_batches_and_minutes_not_successful_work(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            self.prior(service)
            calls = []
            def labels(**kwargs):
                calls.append(('daily', kwargs.get('batch_ids')))
                return {'refreshed_batches': ['LAB-failed'], 'failures': []}
            def minutes(*args, **kwargs):
                calls.append(('minute', set(kwargs.get('eligible_dates') or [])))
                return {'refreshed_dates': ['2026-09-11'], 'failures': []}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=labels), \
                 patch.object(LabService, '_refresh_minute_accounts', side_effect=minutes):
                service.start_result_refresh(manual=True, retry_failed_only=True)
                result = self.wait_status(service, {'complete', 'failed', 'partial_failure'})
            self.assertEqual(calls, [('daily', {'LAB-failed'}), ('minute', {'2026-09-11'})])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['failure_count'], 0)

    def test_all_price_requests_failed_is_failed_not_completed(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': [], 'failures': [{'batch_id':'LAB-x','message':'offline'}]}), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                    'refreshed_dates': [], 'failures': []}):
                service.start_result_refresh(manual=True)
                result = self.wait_status(service, {'complete','failed','partial_failure'})
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['success_count'], 0)
            self.assertEqual(result['failure_count'], 1, result)

    def test_transient_failure_schedules_one_retry_and_permanent_failure_does_not(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            failure = {'batch_id':'LAB-x', 'message':'offline', 'diagnostic': {'kind':'connection_error'}}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': [], 'failures': [failure]}), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                    'refreshed_dates': [], 'failures': []}):
                service.start_result_refresh(manual=True)
                first = self.wait_status(service, {'failed','partial_failure'})
                self.assertIsNotNone(first.get('next_retry_at'))
                first['next_retry_at'] = (datetime.now(ET)-timedelta(seconds=1)).isoformat()
                service._result_refresh_receipt.write_text(json.dumps(first))
                service.start_result_refresh(manual=False)
                second = self.wait_status(service, {'failed','partial_failure'})
            self.assertEqual(second['automatic_retry_count'], 1)
            self.assertIsNone(second.get('next_retry_at'))
            failure['diagnostic'] = {'kind':'auth_error', 'http_status':403}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': [], 'failures': [failure]}), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                    'refreshed_dates': [], 'failures': []}):
                service.start_result_refresh(manual=True)
                result = self.wait_status(service, {'failed','partial_failure'})
            self.assertIsNone(result.get('next_retry_at'))

    def test_no_failed_items_does_not_start_new_refresh(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=AssertionError('not due')):
                result = service.start_result_refresh(manual=True, retry_failed_only=True)
            self.assertEqual(result['status'], 'nothing_to_retry')

    def test_retry_button_bridge_only_retries_failed_price_tasks(self):
        from shaq_daily_oracle.desktop import DesktopBridge
        bridge = DesktopBridge.__new__(DesktopBridge)
        calls = []
        bridge.lab = SimpleNamespace(start_result_refresh=lambda **kw: calls.append(kw) or {'status':'running'})
        self.assertTrue(hasattr(bridge, 'retry_failed_results'))
        self.assertTrue(bridge.retry_failed_results()['ok'])
        self.assertEqual(calls, [{'manual':True, 'retry_failed_only':True}])

    def test_wait_timeout_does_not_fail_live_refresh_or_discard_later_success(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            entered, release = threading.Event(), threading.Event()
            def labels(**kwargs):
                entered.set()
                release.wait(2)
                return {'refreshed_batches': ['LAB-good'], 'failures': []}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=labels), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                     'refreshed_dates': [], 'failures': []}):
                started = service.start_result_refresh(manual=True)
                try:
                    self.assertTrue(entered.wait(1))
                    before = service._result_refresh_receipt.read_bytes()
                    waiting = service.wait_result_refresh(started['operation_id'], timeout=0)
                    self.assertEqual(waiting['status'], 'running')
                    self.assertTrue(service._owned_result_refresh[1].is_alive())
                    self.assertEqual(service._result_refresh_receipt.read_bytes(), before)
                finally:
                    release.set()
                    service._owned_result_refresh[1].join(2)
                self.assertEqual(service.result_refresh_status()['status'], 'complete')
                self.assertEqual(service.result_refresh_status()['result']['refreshed_batches'], ['LAB-good'])

    def test_waiting_on_another_instance_never_mutates_its_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            receipt = {'status': 'running', 'operation_id': 'other-instance'}
            service._result_refresh_receipt.write_text(json.dumps(receipt))
            before = service._result_refresh_receipt.read_bytes()
            self.assertEqual(service.wait_result_refresh('other-instance', timeout=0)['status'], 'running')
            self.assertEqual(service._result_refresh_receipt.read_bytes(), before)

    def test_legacy_retry_retains_success_ids_and_partial_status_across_failures(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            prior = self.prior(service)
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': [], 'failures': prior['result']['failures']}), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                    'refreshed_dates': [], 'failures': prior['result']['minute_settlement']['failures']}):
                service.start_result_refresh(manual=True, retry_failed_only=True)
                service._owned_result_refresh[1].join(2)
            result = service.result_refresh_status()
            self.assertEqual(result['status'], 'partial_failure')
            self.assertEqual(result['success_count'], 2)
            self.assertEqual(result['result']['refreshed_batches'], ['LAB-good'])
            self.assertEqual(result['result']['minute_settlement']['refreshed_dates'], ['2026-09-09'])

    def test_legacy_retry_unions_new_and_old_success_ids(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            self.prior(service)
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': ['LAB-failed'], 'failures': []}), \
                 patch.object(LabService, '_refresh_minute_accounts', return_value={
                    'refreshed_dates': ['2026-09-11'], 'failures': []}):
                service.start_result_refresh(manual=True, retry_failed_only=True)
                service._owned_result_refresh[1].join(2)
            result = service.result_refresh_status()
            self.assertEqual(result['success_count'], 4)
            self.assertEqual(set(result['result']['refreshed_batches']), {'LAB-good', 'LAB-failed'})
            self.assertEqual(set(result['result']['minute_settlement']['refreshed_dates']),
                             {'2026-09-09', '2026-09-11'})

    def test_ticker_local_schema_error_does_not_block_next_valid_request(self):
        from shaq_daily_oracle.lab_service import _RefreshMarket
        from shaq_daily_oracle.data_providers import DataProfile, DataProviderError
        provider = _RefreshMarket(DataProfile('fixture', 'unused'))
        def data(symbols, **kwargs):
            if symbols == ['BAD']:
                raise DataProviderError('schema failure', diagnostic={'kind': 'provider_error'})
            return {'GOOD': [{'open': 123}]}
        with patch.object(provider.provider, 'fresh_history', side_effect=data):
            with self.assertRaises(DataProviderError):
                provider.history(['BAD'])
            try:
                result = provider.history(['GOOD'])
            except DataProviderError as exc:
                self.fail(f'A ticker-local error poisoned an unrelated request: {exc.diagnostic}')
        self.assertEqual(result, {'GOOD': [{'open': 123}]})

    def test_later_stage_exception_preserves_prior_targets_and_completed_daily_result(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            self.prior(service)
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': ['LAB-failed'], 'failures': []}), \
                 patch.object(LabService, '_refresh_minute_accounts', side_effect=ValueError('minute stage failed')):
                service.start_result_refresh(manual=True, retry_failed_only=True)
                service._owned_result_refresh[1].join(2)
            failed = service.result_refresh_status()
            self.assertEqual(set(failed.get('result', {}).get('refreshed_batches', [])), {'LAB-good', 'LAB-failed'})
            self.assertTrue(failed['result'].get('stage_failures'))
            calls = []
            def labels(**kwargs):
                calls.append(('daily', kwargs['batch_ids']))
                return {'refreshed_batches': [], 'failures': []}
            def minute(*args, **kwargs):
                calls.append(('minute', set(kwargs['eligible_dates'])))
                return {'refreshed_dates': ['2026-09-11'], 'failures': []}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=labels), \
                 patch.object(LabService, '_refresh_minute_accounts', side_effect=minute):
                retried = service.start_result_refresh(manual=True, retry_failed_only=True)
                self.assertEqual(retried['status'], 'running')
                service._owned_result_refresh[1].join(2)
            self.assertFalse(any(kind == 'daily' and ids for kind, ids in calls))
            self.assertIn(('minute', {'2026-09-11'}), calls)
            self.assertEqual(service.result_refresh_status()['status'], 'complete')

    def test_account_reconciliation_retry_does_not_recollect_successful_minutes(self):
        from shaq_daily_oracle.virtual_accounts import AccountStore
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            service.dashboard = SimpleNamespace(overview=lambda: {'daily_results': []}, account_rows=lambda rows: rows)
            requests = []
            def collect(**kwargs):
                requests.append(kwargs.get('eligible_dates'))
                return {'refreshed_dates': ['2026-09-11'], 'failures': []}
            original_refresh = AccountStore.refresh
            attempts = []
            def reconcile(store, rows):
                attempts.append(True)
                if len(attempts) == 1:
                    raise ValueError('fixture local settlement validation failure')
                return original_refresh(store, rows)
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', return_value={
                    'refreshed_batches': ['LAB-good'], 'failures': []}), \
                 patch('shaq_daily_oracle.minute_settlements.refresh_minute_observations', side_effect=collect), \
                 patch.object(AccountStore, 'refresh', reconcile):
                service.start_result_refresh(manual=True)
                service._owned_result_refresh[1].join(2)
                first = service.result_refresh_status()
                self.assertEqual(first.get('result', {}).get('minute_settlement', {}).get('refreshed_dates'), ['2026-09-11'])
                retry = service.start_result_refresh(manual=True, retry_failed_only=True)
                self.assertEqual(retry['status'], 'running')
                service._owned_result_refresh[1].join(2)
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(service.result_refresh_status()['status'], 'complete')

    def test_failed_daily_stage_retries_original_date_scope_and_unstarted_minutes(self):
        with tempfile.TemporaryDirectory() as name:
            service = self.service(Path(name))
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=OSError('fixture read error')):
                service.start_result_refresh(manual=True, eligible_dates=['2026-09-11'])
                service._owned_result_refresh[1].join(2)
            seen = []
            def labels(**kwargs):
                seen.append(('daily', kwargs['eligible_dates']))
                return {'refreshed_batches': ['LAB-failed'], 'failures': []}
            def minutes(*args, **kwargs):
                seen.append(('minute', kwargs['eligible_dates'], kwargs.get('reconcile_only', False)))
                return {'refreshed_dates': ['2026-09-11'], 'failures': []}
            with patch('shaq_daily_oracle.lab_service.refresh_research_labels', side_effect=labels), \
                 patch.object(LabService, '_refresh_minute_accounts', side_effect=minutes):
                retry = service.start_result_refresh(manual=True, retry_failed_only=True)
                self.assertEqual(retry['status'], 'running')
                service._owned_result_refresh[1].join(2)
            self.assertEqual(seen, [('daily', {'2026-09-11'}), ('minute', {'2026-09-11'}, False)])
            self.assertEqual(service.result_refresh_status()['status'], 'complete')
