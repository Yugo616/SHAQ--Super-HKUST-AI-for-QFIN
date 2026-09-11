import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_virtual_accounts as fixtures
from shaq_daily_oracle.virtual_accounts import AccountStore, AccountRules
from shaq_daily_oracle.minute_settlements import MINUTE_NAMESPACE, MinuteStore, refresh_minute_observations
from shaq_daily_oracle.minute_settlements import settlement_due_dates
from shaq_daily_oracle.data_providers import DataProfile
from shaq_daily_oracle.hashing import sha256_payload


class MinuteAccountIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AccountStore(self.root / 'virtual_accounts')
        self.store.activate(AccountRules(), '2026-09-09T07:00:00-04:00')
        self.fixture = fixtures.VirtualAccountTests()

    def test_missing_exit_keeps_actual_position_and_blocks_only_its_account_then_retry_recovers(self):
        row = self.fixture.row()
        row['minute']['records']['AAA'].pop()
        rows = [row, self.fixture.row('next', '2026-09-10T08:00:00-04:00', trade_date='2026-09-10'),
                self.fixture.row('other', series_key='other:model')]
        first = self.store.refresh(rows)
        entry = next(r for r in first['results'] if r['batch_id'] == 'zzz-first')
        self.assertEqual(entry['status'], 'incomplete')
        self.assertEqual(entry['closing_positions'], {'AAA': 9})
        self.assertEqual(entry['trades'][0]['quantity'], 9)
        self.assertIsNone(entry['net_pnl'])
        self.assertEqual(next(r for r in first['results'] if r['batch_id'] == 'next')['status'], 'blocked_previous')
        self.assertEqual(next(a for a in first['accounts'] if a['series_key'] == 'other:model')['sessions'], 1)
        json.dumps(first, allow_nan=False)
        # Restart/retry while incomplete must not mutate immutable settlement revisions.
        self.assertEqual(self.store.refresh(rows), first)
        rows[0] = self.fixture.row()
        recovered = self.store.refresh(rows)
        self.assertEqual(next(a for a in recovered['accounts'] if a['series_key'] == 'skill:model')['sessions'], 2)

    def test_due_schedule_is_postclose_finite_and_reviewed_rows_never_fetch(self):
        row = self.fixture.row()
        row['minute'] = {}
        self.assertEqual(settlement_due_dates([row], datetime.fromisoformat('2026-09-09T15:59:00-04:00')), [])
        self.assertEqual(settlement_due_dates([row], datetime.fromisoformat('2026-09-09T16:05:00-04:00')), ['2026-09-09'])
        attempts = {'2026-09-09': {'scheduled_offsets':[5,15,30,60]}}
        self.assertEqual(settlement_due_dates([row], datetime.fromisoformat('2026-09-09T17:01:00-04:00'), attempts), [])
        reviewed = self.fixture.row()
        self.assertEqual(settlement_due_dates([reviewed], datetime.fromisoformat('2026-09-10T16:05:00-04:00'), {}, app_open=True), [])

    def test_late_open_consumes_each_retry_slot_once(self):
        from shaq_daily_oracle.minute_settlements import record_settlement_attempt, load_settlement_attempts
        row = self.fixture.row(); row['minute'] = {}
        now = datetime.fromisoformat('2026-09-09T17:00:00-04:00')
        for expected in (5, 15, 30, 60):
            attempts = load_settlement_attempts(self.root)
            self.assertEqual(settlement_due_dates([row], now, attempts), ['2026-09-09'])
            updated = record_settlement_attempt(self.root, ['2026-09-09'], now)
            self.assertIn(expected, updated['2026-09-09']['scheduled_offsets'])
        self.assertEqual(settlement_due_dates([row], now, load_settlement_attempts(self.root)), [])

    def test_minute_refresh_filters_exact_due_dates_before_provider_call(self):
        rows = [self.fixture.row(), self.fixture.row('next', trade_date='2026-09-10')]
        class Provider:
            calls = []
            def history(self, symbols, **kwargs):
                self.calls.append(kwargs['start'].isoformat())
                return self_fixture.minute(kwargs['start'].isoformat())['records']
        self_fixture = self.fixture
        provider = Provider()
        refresh_minute_observations(research_root=self.root, rows=rows,
            profile=DataProfile('test','test.csv'), observed_at=datetime.fromisoformat('2026-09-10T16:10:00-04:00'),
            market_provider=provider, eligible_dates={'2026-09-10'})
        self.assertEqual(provider.calls, ['2026-09-10'])

    def test_confirmation_revision_rebuild_and_duplicate_order_invariance(self):
        minute_store = MinuteStore(self.root / 'minutes')
        row = self.fixture.row()
        data = row['minute']['records']
        def observe(at, values):
            return minute_store.observe('2026-09-09', ['AAA', 'BBB'], values, provider='yfinance',
                                        observed_at=datetime.fromisoformat(at))
        row['minute'] = observe('2026-09-09T16:10:00-04:00', data)
        provisional = self.store.refresh([row])
        self.assertEqual(provisional['accounts'][0]['sessions'], 1)
        self.assertEqual(provisional['results'][0]['status'], 'provisional')
        self.assertAlmostEqual(provisional['results'][0]['account_equity'], 10176.400045)
        row['minute'] = observe('2026-09-10T09:00:00-04:00', data)
        next_row = self.fixture.row('next', '2026-09-10T08:00:00-04:00', trade_date='2026-09-10')
        final = self.store.refresh([row, next_row])
        self.assertAlmostEqual(final['accounts'][0]['equity'], 10352.80009)
        prior_files = {p.name: p.read_bytes() for p in (self.store.root / 'settlements').glob('*.json')}
        data = copy.deepcopy(data); data['AAA'][1]['open'] = 120
        row['minute'] = observe('2026-09-10T10:00:00-04:00', data)
        revised = self.store.refresh([row, next_row])
        self.assertEqual(revised['accounts'][0]['sessions'], 2)
        row['minute'] = observe('2026-09-11T09:00:00-04:00', data)
        duplicate = dict(row, batch_id='later', completed_at_et='2026-09-09T08:30:00-04:00')
        corrected = self.store.refresh([next_row, duplicate, row])
        self.assertAlmostEqual(corrected['accounts'][0]['equity'], 10442.7101125)
        self.assertEqual(self.store.refresh([row, duplicate, next_row]), corrected)
        self.assertTrue(all((self.store.root/'settlements'/name).read_bytes() == contents for name, contents in prior_files.items()))

    def test_empty_and_partial_historical_refresh_preserve_accounts_and_raw_receipts(self):
        minute_store = MinuteStore(self.root / MINUTE_NAMESPACE)
        row = self.fixture.row()
        data = copy.deepcopy(row['minute']['records'])
        for at in ['2026-09-09T16:10:00-04:00', '2026-09-10T09:00:00-04:00']:
            row['minute'] = minute_store.observe(row['trade_date'], ['AAA', 'BBB'], data,
                provider='yfinance', observed_at=datetime.fromisoformat(at))
        other = self.fixture.row('other', series_key='other:model')
        other['minute'] = copy.deepcopy(row['minute'])
        confirmed = self.store.refresh([row, other])
        final_execution = row['minute']['execution_sha256']
        next_row = self.fixture.row('next', '2026-09-10T08:00:00-04:00', trade_date='2026-09-10')
        next_data = copy.deepcopy(next_row['minute']['records'])
        for at in ['2026-09-10T16:10:00-04:00', '2026-09-11T09:00:00-04:00']:
            minute_store.observe(next_row['trade_date'], ['AAA', 'BBB'], next_data,
                provider='yfinance', observed_at=datetime.fromisoformat(at))
        prior_files = {p: p.read_bytes() for p in self.root.rglob('*.json')
                       if p.name != 'summary.json'}
        rows = [row, next_row, other]
        fixture = self.fixture
        class Provider:
            response = {'AAA': [], 'BBB': []}
            def history(self, symbols, *, start, **kwargs):
                return self.response if start.isoformat() == '2026-09-09' else fixture.minute(start.isoformat())['records']
        provider = Provider()
        # Omitted symbol, empty symbol, and missing exit are unavailable retrievals,
        # not authoritative disappearance of previously confirmed bars.
        for offset, response in enumerate([
            {'AAA': [], 'BBB': []}, {'BBB': data['BBB']},
            {'AAA': data['AAA'][:1], 'BBB': data['BBB']},
        ]):
            provider.response = response
            at = datetime.fromisoformat(f'2026-09-21T09:0{offset}:00-04:00')
            receipt = refresh_minute_observations(research_root=self.root, rows=rows,
                profile=DataProfile('test', 'test.csv'), observed_at=at, market_provider=provider)
            self.assertTrue(receipt['failures'], 'Missing target retrieval must be reported, not counted as a successful observation')
            self.assertEqual(receipt['failures'][0]['trade_date'], '2026-09-09')
            self.assertEqual(receipt['failures'][0]['error_type'], 'UnavailableMinuteTargets')
            row['minute'] = MinuteStore(minute_store.root).snapshot(row['trade_date'], ['AAA', 'BBB'])
            other['minute'] = copy.deepcopy(row['minute'])
            next_row['minute'] = minute_store.snapshot(next_row['trade_date'], ['AAA', 'BBB'])
            self.assertEqual(row['minute']['status'], 'final')
            self.assertEqual(row['minute']['execution_sha256'], final_execution)
            self.assertFalse(row['minute']['correction'])
            result = AccountStore(self.root / 'virtual_accounts').refresh(rows)
            account = next(a for a in result['accounts'] if a['series_key'] == 'skill:model')
            self.assertEqual(account['sessions'], 2)
            self.assertAlmostEqual(account['equity'], 10352.80009)
            self.assertFalse(account['blocked'])
            independent = next(a for a in result['accounts'] if a['series_key'] == 'other:model')
            self.assertEqual(independent, next(a for a in confirmed['accounts'] if a['series_key'] == 'other:model'))
            entry = next(r for r in result['results'] if r['batch_id'] == 'zzz-first')
            self.assertEqual(entry['latest_refresh']['captured_at_et'], at.isoformat())
            self.assertEqual(entry['execution_sha256'], final_execution)
            self.assertEqual(entry['status'], 'final')
            self.assertAlmostEqual(entry['account_equity'], 10176.400045)
            raw = json.loads(next(minute_store.root.rglob(
                row['minute']['latest_refresh']['observation_sha256'] + '.json')).read_text())
            self.assertEqual(raw['records'], response)
            self.assertEqual(raw['captured_at_et'], at.isoformat())
            self.assertTrue(all(p.read_bytes() == contents for p, contents in prior_files.items()))
            refresh_minute_observations(research_root=self.root, rows=rows,
                profile=DataProfile('test', 'test.csv'), observed_at=at, market_provider=provider)
            row['minute'] = MinuteStore(minute_store.root).snapshot(row['trade_date'], ['AAA', 'BBB'])
            self.assertEqual(AccountStore(self.root / 'virtual_accounts').refresh(rows), result)

    def test_initial_missing_refresh_stays_unavailable_and_blocks_only_its_account(self):
        row = self.fixture.row()
        row['minute'] = MinuteStore(self.root / 'minutes').observe(row['trade_date'], ['AAA', 'BBB'],
            {'AAA': [], 'BBB': []}, provider='yfinance',
            observed_at=datetime.fromisoformat('2026-09-10T09:00:00-04:00'))
        result = self.store.refresh([row, self.fixture.row('other', series_key='other:model')])
        entry = next(r for r in result['results'] if r['batch_id'] == 'zzz-first')
        self.assertEqual(entry['status'], 'unavailable')
        self.assertEqual(entry['orders'], [])
        self.assertEqual([trade['quantity'] for trade in entry['trades']], [0, 0])
        self.assertEqual(next(a for a in result['accounts'] if a['series_key'] == 'skill:model')['sessions'], 0)
        self.assertEqual(next(a for a in result['accounts'] if a['series_key'] == 'other:model')['sessions'], 1)

    def test_legacy_activation_and_saved_outputs_are_read_only_and_never_replayed(self):
        legacy_root = self.root / 'old'
        legacy_root.mkdir()
        activation = dict(rules={'schema_version': 1}, activated_at='2026-09-01T08:00:00-04:00')
        (legacy_root/'activation.json').write_text(json.dumps(activation))
        document = dict(engine='backtrader', account_id='old-account', status='settled', net_pnl=42,
                        trade_date='2026-09-09', account_equity=10042, scope='forward')
        (legacy_root/'settlements').mkdir()
        (legacy_root/'settlements'/(sha256_payload(document)+'.json')).write_text(json.dumps(document))
        before = {str(p.relative_to(legacy_root)): p.read_bytes() for p in legacy_root.rglob('*.json')}
        store = AccountStore(legacy_root)
        store.activate(AccountRules(), '2026-09-10T08:00:00-04:00')
        value = store.refresh([])['legacy']
        self.assertEqual(value['results'][0]['net_pnl'], 42)
        self.assertTrue(value['read_only'])
        self.assertTrue(all((legacy_root/name).read_bytes() == data for name, data in before.items()))
        self.assertEqual(AccountStore(self.root/'missing').read_legacy()['status'], 'unavailable')

    def test_forward_equity_does_not_depend_on_official_daily_labels(self):
        row = self.fixture.row(labels={})
        result = self.store.refresh([row])
        self.assertAlmostEqual(result['accounts'][0]['equity'], 10176.400045)
        self.assertIsNone(result['results'][0]['trades'][0]['direction_correct'])

    def test_activation_namespace_has_only_supported_schema_and_future_policy_times_rejected(self):
        with self.assertRaises(ValueError):
            self.store.activate(AccountRules(schema_version=1))
        with self.assertRaises(ValueError):
            self.store.activate(AccountRules(commission_rate=0), '2026-09-08T07:00:00-04:00')

    def test_late_result_is_visible_as_late_and_cannot_consume_timely_first_result(self):
        late = self.fixture.row('late', '2026-09-09T09:10:00-04:00', source_eligible=False,
                                cutoff_status='on_time')
        result = self.store.refresh([late, self.fixture.row()])
        self.assertEqual(next(r for r in result['results'] if r['batch_id']=='late')['scope'], 'late')
        self.assertEqual(result['accounts'][0]['sessions'], 1)

    def test_policy_change_invalidates_cached_current_policy_view(self):
        row = self.fixture.row()
        self.store.refresh([row])
        changed = self.store.activate(AccountRules(commission_rate=0), '2026-09-10T07:00:00-04:00')
        view = self.store.view([row])
        self.assertEqual(view['rules'], changed['rules'])
        self.assertTrue(view['reconciliation_pending'])

    def test_postclose_service_hook_collects_minutes_and_reconciles_cached_accounts(self):
        from shaq_daily_oracle.lab_service import LabService
        import test_research_lab_foundation as lab_fixtures
        paths = lab_fixtures.ResearchLabFoundationTests().paths(self.root/'service')
        lab = LabService(paths)
        AccountStore(paths.research_root/'virtual_accounts').activate(
            AccountRules(), '2026-09-09T07:00:00-04:00')
        row = self.fixture.row(predictions=[], labels={})
        with patch.object(lab.dashboard, 'overview', return_value={'daily_results':[row]}):
            result = lab._refresh_minute_accounts(DataProfile('test','test.csv'))
        self.assertEqual(result['refreshed_dates'], [])
        cached = AccountStore(paths.research_root/'virtual_accounts').view([dict(row, minute=lab.dashboard.account_rows([row])[0]['minute'])])
        self.assertEqual(cached['results'][0]['status'], 'empty')
        self.assertEqual(cached['results'][0]['net_pnl'], 0.)

    def test_concurrent_service_refresh_does_not_collect_or_overwrite_active_reconciliation(self):
        from shaq_daily_oracle.lab_service import LabService
        from filelock import FileLock
        import test_research_lab_foundation as lab_fixtures
        paths = lab_fixtures.ResearchLabFoundationTests().paths(self.root/'service-lock')
        lab = LabService(paths)
        with FileLock(str(paths.research_root/'minute_refresh.lock')):
            with patch.object(lab.dashboard, 'overview', side_effect=AssertionError('concurrent refresh')):
                result = lab._refresh_minute_accounts(DataProfile('test','test.csv'))
        self.assertEqual(result['status'], 'already_running')

    def test_execution_processing_receipt_is_persisted_without_changing_replay_identity(self):
        row = self.fixture.row()
        result = self.store.refresh([row])
        entry = result['results'][0]
        self.assertIsNotNone(datetime.fromisoformat(entry['processed_at_et']).tzinfo)
        self.assertEqual(self.store.refresh([row]), result)

    def test_daily_dashboard_stays_one_share_and_never_executes_during_render(self):
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        index = ResearchDashboardIndex(batches_root=self.root/'batches', database=self.root/'index.db')
        row = self.fixture.row()
        self.store.refresh([row])
        with patch('shaq_daily_oracle.virtual_accounts.replay_day', side_effect=AssertionError('view execution')):
            overview = index.overview()
        self.assertIn('virtual_accounts', overview)
        self.assertAlmostEqual(overview['virtual_accounts']['accounts'][0]['equity'], 10176.400045)

    def test_collection_shares_date_symbol_union_and_keeps_ai_profile_and_rows_unchanged(self):
        rows = [self.fixture.row(), self.fixture.row('shadow', series_key='shadow:model'),
                self.fixture.row('empty', predictions=[])]
        original = copy.deepcopy(rows)
        profile = DataProfile('test', 'test.csv', intraday_interval='5m')
        fixture = self.fixture
        class Provider:
            calls = []
            def history(self, symbols, **kwargs):
                self.calls.append((symbols, kwargs))
                return fixture.minute()['records']
        provider = Provider()
        result = refresh_minute_observations(research_root=self.root, rows=rows, profile=profile,
                    observed_at=datetime.fromisoformat('2026-09-09T16:10:00-04:00'), market_provider=provider)
        self.assertEqual(result['refreshed_dates'], ['2026-09-09'])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][0], ['AAA', 'BBB'])
        self.assertEqual(provider.calls[0][1]['interval'], '1m')
        self.assertFalse(provider.calls[0][1]['prepost'])
        self.assertEqual(profile.intraday_interval, '5m')
        self.assertEqual(rows, original)
        self.assertFalse(list(self.root.rglob('labels.json')))

    def test_view_uses_saved_summary_without_engine_or_network_and_shows_pending_new_results(self):
        row = self.fixture.row()
        settled = self.store.refresh([row])
        with patch('shaq_daily_oracle.virtual_accounts.replay_day', side_effect=AssertionError('view must not execute')):
            view = self.store.view([row])
            self.assertEqual(view['accounts'], settled['accounts'])
            new = self.store.view([row, self.fixture.row('new', series_key='new:model')])
            self.assertEqual(len(new['results']), 2)
            self.assertEqual(next(r for r in new['results'] if r['batch_id']=='new')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
