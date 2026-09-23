import copy
import unittest
import tempfile
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

from shaq_daily_oracle.public_history_recovery import parse_chart, parse_nasdaq, merge_daily


ET = ZoneInfo('America/New_York')


class PublicHistoryRecoveryTests(unittest.TestCase):
    def test_recovery_identity_ignores_reference_annotations(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
        p = YFinanceProvider(DataProfile('test', 'none'))
        a = PublicHistoryRecovery(p, {'enabled': True})
        b = PublicHistoryRecovery(p, {'enabled': True, 'parameter_bindings': {'enabled': ['ref']}})
        self.assertEqual(a._request('AAA', date(2026,9,21), date(2026,9,23), '1d'),
                         b._request('AAA', date(2026,9,21), date(2026,9,23), '1d'))

    def test_missing_interior_session_is_recovered(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
        calls = []
        def bar(day):
            return {'timestamp': day+'T00:00:00', 'open':100,'high':102,'low':99,'close':101,'volume':10}
        class Provider(PublicHistoryRecovery):
            def _chart(self, *args, **kwargs):
                return [bar('2026-09-21'),bar('2026-09-23')]
            def _nasdaq(self, symbol, **kwargs):
                calls.append(symbol);return [bar('2026-09-22')]
        p=Provider(YFinanceProvider(DataProfile('test','none')), {})
        rows,_=p._one('AAA',start=date(2026,9,21),end=date(2026,9,24),interval='1d',prepost=False)
        self.assertEqual(len(rows),3);self.assertEqual(calls,['AAA'])

    def test_recover_uses_new_checkpoint_without_network(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
        from shaq_daily_oracle.collection_checkpoint import HistoryCheckpoint
        with tempfile.TemporaryDirectory() as name:
            p=PublicHistoryRecovery(YFinanceProvider(DataProfile('test','none')),{},checkpoint_root=Path(name))
            rows=[{'timestamp':'2026-09-22T00:00:00','open':100,'high':102,'low':99,'close':101,'volume':1}]
            HistoryCheckpoint(Path(name),p._request('AAA',date(2026,9,22),date(2026,9,23),'1d')).save(rows)
            self.assertEqual(p.recover_history(['AAA'],start=date(2026,9,22),end=date(2026,9,23)),{'AAA':rows})

    def test_completed_intraday_bars_only(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile,YFinanceProvider
        from unittest.mock import patch
        p=PublicHistoryRecovery(YFinanceProvider(DataProfile('test','none',intraday_interval='5m')), {})
        rows={'AAA':[{'timestamp':'2026-09-23T08:45:00-04:00'}, {'timestamp':'2026-09-23T08:50:00-04:00'}]}
        with patch.object(p,'history',return_value=rows):
            result=p.recent_intraday(['AAA'],cutoff=datetime(2026,9,23,8,50,tzinfo=ET))
        self.assertEqual(result['AAA'],rows['AAA'][:1])

    def test_cache_keeps_other_fetch_groups_when_one_symbol_fails(self):
        from shaq_daily_oracle.public_data import DailyBarCache
        from shaq_daily_oracle.data_providers import DataProviderError
        class Provider:
            supports_partial_history=True
            def history(self,symbols,**kwargs):
                if symbols==['BBB']:
                    raise DataProviderError('failed',diagnostic={'kind':'timeout'})
                return {s:[{'timestamp':'2026-09-22T00:00:00','close':101}] for s in symbols}
            def recover_history(self,*args,**kwargs):return {}
        with tempfile.TemporaryDirectory() as name:
            cache=DailyBarCache(Provider(),Path(name),overlap_days=1)
            cache._save('AAA',{},[{'timestamp':'2026-09-21T00:00:00','close':100}],
                        start=date(2026,9,1),end=date(2026,9,22))
            result=cache.history(['AAA','BBB'],start=date(2026,9,1),end=date(2026,9,23))
            self.assertEqual(result['AAA'][-1]['close'],101)
            self.assertEqual(result['BBB'],[])

    def test_nasdaq_share_class_symbol_not_false_mismatch(self):
        body=self.nasdaq();body['data']['symbol']='BRK/B'
        self.assertEqual(len(parse_nasdaq(body,'BRK.B',date(2026,9,22),date(2026,9,23))),1)

    def test_invalid_leading_bar_cannot_be_saved_as_collected(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile,YFinanceProvider,DataProviderError
        class Provider(PublicHistoryRecovery):
            def _chart(self,*args,**kwargs):
                return [{'timestamp':'2026-09-21T00:00:00','open':None,'high':102,'low':99,'close':101,'volume':1},
                        {'timestamp':'2026-09-22T00:00:00','open':100,'high':102,'low':99,'close':101,'volume':1}]
            def _nasdaq(self,*args,**kwargs): return []
        with self.assertRaises(DataProviderError):
            Provider(YFinanceProvider(DataProfile('test','none')),{})._one('AAA',
                start=date(2026,9,21),end=date(2026,9,23),interval='1d',prepost=False)

    def test_recovery_rejects_old_interior_gap_checkpoint(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile,YFinanceProvider
        from shaq_daily_oracle.collection_checkpoint import HistoryCheckpoint
        with tempfile.TemporaryDirectory() as name:
            p=PublicHistoryRecovery(YFinanceProvider(DataProfile('test','none')),{},checkpoint_root=Path(name))
            rows=[{'timestamp':d+'T00:00:00','open':100,'high':102,'low':99,'close':101,'volume':1}
                  for d in ['2026-09-21','2026-09-23']]
            HistoryCheckpoint(Path(name),p._request('AAA',date(2026,9,21),date(2026,9,24),'1d')).save(rows)
            self.assertEqual(p.recover_history(['AAA'],start=date(2026,9,21),end=date(2026,9,24)),{})

    def test_missing_receipt_refetches_and_old_session_receipt_is_retained(self):
        import hashlib,json
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile,YFinanceProvider
        from shaq_daily_oracle.collection_checkpoint import HistoryCheckpoint
        from shaq_daily_oracle.settings import _atomic_json
        raw='{"sample":1}';digest=hashlib.sha256(raw.encode()).hexdigest()
        calls=[]
        rows=[{'timestamp':'2026-09-22T00:00:00','open':100,'high':102,'low':99,'close':101,'volume':1}]
        class Provider(PublicHistoryRecovery):
            def _chart(self,*args,**kwargs):calls.append(1);return rows
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)/'2026-09-23'/'profile'
            p=Provider(YFinanceProvider(DataProfile('test','none')),{},checkpoint_root=root)
            HistoryCheckpoint(root,p._request('AAA',date(2026,9,22),date(2026,9,23),'1d')).save(
                [{**rows[0],'source_response_sha256':digest}])
            actual,status=p._one('AAA',start=date(2026,9,22),end=date(2026,9,23),interval='1d',prepost=False)
            self.assertEqual(calls,[1]);self.assertEqual(status,'collected')
            _atomic_json(Path(name)/'2026-09-22'/'profile'/'sources'/(digest+'.json'),
                         {'raw':raw,'response_sha256':digest})
            p.retain_history_sources([{**rows[0],'source_response_sha256':digest}])
            self.assertEqual(p.source_documents[digest]['raw'],raw)

    def chart(self):
        return {'chart': {'error': None, 'result': [{
            'meta': {'symbol': 'AAA', 'exchangeTimezoneName': 'America/New_York'},
            'timestamp': [int(datetime(2026, 9, 22, 9, 30, tzinfo=ET).timestamp())],
            'indicators': {'quote': [{'open': [100], 'high': [104], 'low': [99],
                                      'close': [None], 'volume': [800]}]},
        }]}}

    def nasdaq(self):
        return {'status': {'rCode': 200}, 'data': {'symbol': 'AAA', 'totalRecords': 1,
            'tradesTable': {'rows': [{'date': '09/22/2026', 'open': '$100.50',
                'high': '$104.00', 'low': '$99.00', 'close': '$102.00', 'volume': '1,200'}]}}}

    def test_missing_close_is_not_replaced_with_metadata_current_price(self):
        body = self.chart()
        body['chart']['result'][0]['meta']['regularMarketPrice'] = 999
        rows = parse_chart(body, 'AAA', date(2026, 9, 22), date(2026, 9, 23), '1d')
        self.assertIsNone(rows[0]['close'])
        self.assertEqual(rows[0]['timestamp'], '2026-09-22T00:00:00')

    def test_backup_replaces_entire_invalid_bar_not_only_close(self):
        primary = parse_chart(self.chart(), 'AAA', date(2026, 9, 22), date(2026, 9, 23), '1d')
        backup = parse_nasdaq(self.nasdaq(), 'AAA', date(2026, 9, 22), date(2026, 9, 23))
        result = merge_daily(primary, backup)
        self.assertEqual(result[0]['open'], 100.5)
        self.assertEqual(result[0]['close'], 102)
        self.assertEqual(result[0]['volume'], 1200)
        self.assertEqual(result[0]['source_provider'], 'nasdaq-public-history')

    def test_backup_does_not_overwrite_valid_primary(self):
        primary = [{'timestamp': '2026-09-22T00:00:00', 'open': 100, 'high': 105,
                    'low': 99, 'close': 103, 'volume': 1000}]
        backup = parse_nasdaq(self.nasdaq(), 'AAA', date(2026, 9, 22), date(2026, 9, 23))
        self.assertEqual(merge_daily(primary, backup), primary)

    def test_wrong_symbol_invalid_ohlc_and_incomplete_pagination_rejected(self):
        from shaq_daily_oracle.data_providers import DataProviderError
        for mutate in (lambda b: b['data'].update(symbol='BBB'),
                       lambda b: b['data'].update(totalRecords=2),
                       lambda b: b['data']['tradesTable']['rows'][0].update(low='$500')):
            b = self.nasdaq(); mutate(b)
            with self.assertRaises(DataProviderError):
                parse_nasdaq(b, 'AAA', date(2026, 9, 22), date(2026, 9, 23))

    def test_chart_rejects_mismatched_lengths_and_symbol(self):
        from shaq_daily_oracle.data_providers import DataProviderError
        for mutate in (lambda b: b['chart']['result'][0]['meta'].update(symbol='BBB'),
                       lambda b: b['chart']['result'][0]['indicators']['quote'][0].update(close=[])):
            b = self.chart(); mutate(b)
            with self.assertRaises(DataProviderError):
                parse_chart(b, 'AAA', date(2026, 9, 22), date(2026, 9, 23), '1d')

    def test_chart_does_not_keep_out_of_request_dates(self):
        rows = parse_chart(self.chart(), 'AAA', date(2026, 9, 23), date(2026, 9, 24), '5m')
        self.assertEqual(rows, [])

    def test_transport_failure_does_not_discard_other_symbol_and_resume_uses_checkpoint(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider, DataProviderError
        calls = []
        failed = True
        class Provider(PublicHistoryRecovery):
            def _chart(self, symbol, **kwargs):
                calls.append(symbol)
                if symbol == 'BBB' and failed:
                    raise DataProviderError('timeout', diagnostic={'kind': 'timeout'})
                return [{'timestamp': '2026-09-22T00:00:00', 'open': 100, 'high': 103,
                         'low': 99, 'close': 102, 'volume': 1000}]
            def _nasdaq(self, symbol, **kwargs):
                raise DataProviderError('timeout', diagnostic={'kind': 'timeout'})
        with tempfile.TemporaryDirectory() as name:
            p = Provider(YFinanceProvider(DataProfile('test', 'none')), {}, checkpoint_root=Path(name))
            rows = p._history_inline(['AAA', 'BBB'], start=date(2026, 9, 21), end=date(2026, 9, 23))
            self.assertEqual(rows['AAA'][0]['close'], 102)
            self.assertEqual(rows['BBB'], [])
            self.assertEqual(next(r for r in p.diagnostics if r['symbol'] == 'BBB')['status'], 'provider_error')
            failed = False
            rows = p._history_inline(['BBB', 'AAA'], start=date(2026, 9, 21), end=date(2026, 9, 23))
            self.assertEqual(rows['BBB'][0]['close'], 102)
            self.assertEqual(calls.count('AAA'), 1)
            self.assertEqual(calls.count('BBB'), 2)

    def test_no_valid_data_is_not_success_and_auth_failure_does_not_switch_transport(self):
        from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider, DataProviderError
        backups = []
        class Provider(PublicHistoryRecovery):
            def _chart(self, symbol, **kwargs):
                raise DataProviderError('denied', diagnostic={'kind': 'auth_error'})
            def _nasdaq(self, symbol, **kwargs):
                backups.append(symbol)
                return []
        p = Provider(YFinanceProvider(DataProfile('test', 'none')), {})
        with self.assertRaises(DataProviderError):
            p._history_inline(['AAA'], start=date(2026, 9, 21), end=date(2026, 9, 23))
        self.assertEqual(backups, [])
