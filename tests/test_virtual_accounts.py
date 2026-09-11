import copy
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, timedelta
from pathlib import Path


class VirtualAccountTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('shaq_daily_oracle.virtual_accounts'),
                             'A real local broker replay is required, not the one-share ledger')
        from shaq_daily_oracle import virtual_accounts
        return virtual_accounts

    def labels(self, a=110, b=90):
        return {s: {'status': 'final', 'official_unadjusted_open': 100,
                    'official_unadjusted_close': close}
                for s, close in [('AAA', a), ('BBB', b)]}


    def minute(self, day='2026-09-09', a=110, b=90, status='final'):
        from shaq_daily_oracle.market_calendar import market_session
        session = market_session(date.fromisoformat(day))
        if session is None:
            return {}
        records = {s: [dict(timestamp=stamp.isoformat(), open=price, high=price,
                           low=price, close=price, volume=10000)
                       for stamp, price in [(session.market_open + timedelta(minutes=1), 100),
                           (session.market_close - timedelta(minutes=5), close)]]
                   for s, close in [('AAA', a), ('BBB', b)]}
        return dict(status=status, records=records, observation_hashes=['test-observation'],
                    execution_sha256=str((day, a, b)), correction=False,
                    captured_at_et=day+'T16:10:00-04:00')

    def predictions(self):
        return [{'symbol': 'AAA', 'direction': 'bullish'}, {'symbol': 'BBB', 'direction': 'bearish'}]

    def test_real_engine_multiside_costs_and_cash_reconcile(self):
        api = self.api()
        r = api.replay_day('2026-09-09', self.predictions(), self.labels(), api.AccountRules(), minute=self.minute())
        self.assertEqual(r['engine'], 'zipline-reloaded')
        self.assertEqual([t['quantity'] for t in r['trades']], [9, 9])
        self.assertEqual(len(r['orders']), 4)
        self.assertAlmostEqual(r['gross_pnl'], 180)
        self.assertAlmostEqual(r['slippage_cost'], 1.8)
        # 9*(100.05+109.945+99.95+90.045)*0.0005 = 1.799955
        self.assertAlmostEqual(r['fees'], 1.799955)
        self.assertAlmostEqual(r['net_pnl'], 176.400045)
        self.assertAlmostEqual(r['closing_cash'], 10176.400045)
        self.assertTrue(all(x == 0 for x in r['closing_positions'].values()))
        self.assertTrue(all(o['status'] == 'Completed' for o in r['orders']))
        self.assertEqual({o['executed_at_et'][:10] for o in r['orders']}, {'2026-09-09'})

    def test_open_quantity_does_not_read_close_and_order_does_not_matter(self):
        api = self.api()
        r = api.replay_day('2026-09-09', self.predictions(), self.labels(), api.AccountRules(), cash=1500, minute=self.minute())
        other = api.replay_day('2026-09-09', list(reversed(self.predictions())), self.labels(), api.AccountRules(), cash=1500, minute=self.minute())
        self.assertEqual(r, other)
        self.assertEqual([t['quantity'] for t in r['trades']], [7, 7])
        changed = api.replay_day('2026-09-09', self.predictions(), self.labels(a=200, b=10), api.AccountRules(), cash=1500, minute=self.minute(a=200,b=10))
        self.assertEqual([t['quantity'] for t in changed['trades']], [7, 7])
        self.assertLessEqual(r['opening_capital_used'], 1500)

    def test_correct_direction_can_lose_after_costs_and_flat_is_wrong(self):
        api = self.api()
        r = api.replay_day('2026-09-09', self.predictions(), self.labels(a=100.01, b=100), api.AccountRules(), minute=self.minute(a=100.01,b=100))
        self.assertTrue(r['trades'][0]['direction_correct'])
        self.assertLess(r['trades'][0]['net_pnl'], 0)
        self.assertFalse(r['trades'][1]['direction_correct'])

    def test_missing_label_blocks_whole_day_and_small_budget_does_not_fake_fills(self):
        api = self.api()
        r = api.replay_day('2026-09-09', self.predictions(), {}, api.AccountRules())
        self.assertEqual(r['status'], 'pending')
        self.assertEqual(r['orders'], [])
        r = api.replay_day('2026-09-09', self.predictions(), self.labels(), api.AccountRules(), cash=50, minute=self.minute())
        self.assertEqual(r['orders'], [])
        self.assertEqual(r['net_pnl'], 0)
        self.assertTrue(all(t['quantity'] == 0 for t in r['trades']))
        r = api.replay_day('2026-09-09', [], {}, api.AccountRules())
        self.assertEqual(r['status'], 'empty')
        self.assertEqual(r['closing_cash'], 10000)

    def test_early_close_and_invalid_labels(self):
        api = self.api()
        r = api.replay_day('2026-11-27', self.predictions(), self.labels(), api.AccountRules(), minute=self.minute('2026-11-27'))
        self.assertTrue(all(o['reference_at_et'][11:16] == '12:55' for o in r['orders'] if o['phase'] == 'close'))
        for price in [0, -1, float('nan')]:
            labels = self.labels(); labels['AAA']['official_unadjusted_open'] = price
            with self.assertRaises(ValueError):
                api.replay_day('2026-09-09', self.predictions(), labels, api.AccountRules(), minute=self.minute())
        with self.assertRaises(ValueError):
            api.replay_day('2026-09-12', self.predictions(), self.labels(), api.AccountRules(), minute={})

    def row(self, batch='zzz-first', completed='2026-09-09T08:00:00-04:00', **kw):
        return dict({'batch_id': batch, 'variant_key': 'team/main', 'series_key': 'skill:model',
            'label': 'main', 'model': 'test', 'trade_date': '2026-09-09',
            'completed_at_et': completed, 'source_eligible': True,
            'variant_result_sha256': batch, 'labels': self.labels(),
            'minute': self.minute(kw.get('trade_date', '2026-09-09')),
            'predictions': self.predictions()}, **kw)

    def test_activation_first_timestamp_dedup_restart_and_historical_isolation(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp))
            store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            rows = [self.row('aaa-later', '2026-09-09T08:30:00-04:00'), self.row(),
                    self.row('history', '2026-09-08T08:00:00-04:00', trade_date='2026-09-08')]
            r = store.refresh(rows)
            account = r['accounts'][0]
            self.assertEqual(account['sessions'], 1)
            self.assertAlmostEqual(account['equity'], 10176.400045)
            chosen = [x for x in r['results'] if x['scope'] == 'forward']
            self.assertEqual([x['batch_id'] for x in chosen], ['zzz-first'])
            historical = [x for x in r['results'] if x['scope'] == 'historical']
            self.assertEqual(len(historical), 1)
            self.assertAlmostEqual(historical[0]['account_balance'], 10176.400045)
            self.assertAlmostEqual(historical[0]['account_cumulative_net_pnl'], 176.400045)
            self.assertEqual(account['sessions'], 1, 'historical replay must not enter forward account')
            before = {p.name: p.read_bytes() for p in Path(tmp).rglob('*.json')}
            self.assertEqual(api.AccountStore(Path(tmp)).refresh(list(reversed(rows))), r)
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(tmp).rglob('*.json')})

    def test_versions_and_rules_separate_and_pending_stops_later_cash_chain(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            rows = [self.row(minute={}), self.row('next', '2026-09-10T08:00:00-04:00', trade_date='2026-09-10'),
                    self.row('shadow', series_key='other:model', variant_key='user/shadow')]
            r = store.refresh(rows)
            self.assertEqual(len(r['accounts']), 2)
            self.assertEqual(next(x for x in r['results'] if x['batch_id'] == 'next')['status'], 'blocked_previous')
            self.assertEqual(next(x for x in r['accounts'] if x['series_key']=='skill:model')['equity'], 10000)
            first_id = r['rules_hash']
            store.activate(api.AccountRules(commission_rate=0), '2026-09-10T07:00:00-04:00')
            self.assertNotEqual(store.refresh(rows)['rules_hash'], first_id)

    def test_same_version_and_model_inherits_next_day_opening_balance(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            first = self.row()
            second = self.row('day-two', '2026-09-10T08:00:00-04:00', trade_date='2026-09-10')
            result = store.refresh([first, second])
            rows = {row['batch_id']: row for row in result['results']}
            self.assertAlmostEqual(rows['day-two']['opening_cash'], rows['zzz-first']['closing_cash'])
            self.assertAlmostEqual(rows['day-two']['account_balance'], rows['day-two']['closing_cash'])
            self.assertEqual(len(result['accounts']), 1)

    def test_future_completion_and_practice_never_enter_forward_account(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            r = store.refresh([self.row(source_eligible=False)])
            self.assertEqual(r['accounts'], [])
            self.assertEqual(r['results'][0]['scope'], 'practice')

    def test_existing_versions_show_funded_accounts_without_counting_history(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T10:00:00-04:00')
            r = store.refresh([self.row()])
            self.assertEqual(len(r['accounts']), 1)
            self.assertEqual(r['accounts'][0]['equity'], 10000)
            self.assertEqual(r['accounts'][0]['sessions'], 0)
            self.assertEqual(r['results'][0]['scope'], 'historical')

    def test_legacy_missing_completion_is_replay_only(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            r = store.refresh([self.row(completed=None)])
            self.assertEqual(r['results'][0]['scope'], 'historical')
            self.assertEqual(r['results'][0]['status'], 'final')
            self.assertEqual(r['accounts'][0]['sessions'], 0)

    def test_rule_change_keeps_old_account_curve_and_costs(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp)); store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            old = store.refresh([self.row()])['accounts'][0]
            store.activate(api.AccountRules(commission_rate=0), '2026-09-10T07:00:00-04:00')
            result = store.refresh([self.row()])
            retained = next((a for a in result['accounts'] if a['account_id'] == old['account_id']), None)
            self.assertIsNotNone(retained)
            self.assertEqual(retained['curve'], old['curve'])
            self.assertEqual(retained['fees'], old['fees'])

    def test_explicit_continuity_activation_seeds_saved_history_once_and_links_alias(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp))
            rules = api.experimental_risk_rules()
            store.activate(rules, '2026-09-11T12:00:00-04:00', continuity=True)
            history = self.row('history', '2026-09-09T08:00:00-04:00',
                               method_identity='same-method', model_identity='same-model')
            history['minute'] = self.minute(a=102, b=100)
            pending = self.row('pending', '2026-09-11T08:00:00-04:00', trade_date='2026-09-11',
                               series_key='alias-name:model', method_identity='same-method',
                               model_identity='same-model', minute={})
            first = store.refresh([history, pending])
            account = first['accounts'][0]
            self.assertAlmostEqual(account['opening_simulation_balance'], 10014.3820045)
            self.assertAlmostEqual(account['equity'], 10014.3820045)
            self.assertTrue(account['blocked'])
            self.assertEqual(first['results'][1]['status'], 'pending')
            self.assertEqual(first['results'][1]['source_scope'], 'simulation_cumulative')
            self.assertEqual(store.refresh([history, pending]), first)

    def test_risk_sizing_uses_frozen_prior_volatility_and_rejects_missing(self):
        api = self.api()
        rules = api.experimental_risk_rules(lookback=2)
        base = [{'symbol':'AAA','direction':'bullish'}, {'symbol':'BBB','direction':'bearish'}]
        histories = {'AAA':[{'date':'2026-09-04','open':100,'close':98.5857864376},
                            {'date':'2026-09-08','open':100,'close':101.4142135624}]}
        predictions = api.freeze_risk_sizing(base, histories, trade_date='2026-09-09',
            frozen_at_et='2026-09-09T08:00:00-04:00', lookback=2)
        result = api.replay_day('2026-09-09', predictions, self.labels(), rules,
                                cash=10000, minute=self.minute())
        self.assertEqual(result['trades'][0]['quantity'], 9)
        self.assertEqual(result['trades'][1]['quantity'], 0)
        self.assertEqual(result['trades'][1]['sizing_unavailable_reason'], 'missing_frozen_sigma')
        doubled = api.replay_day('2026-09-09', predictions, self.labels(), rules,
                                 cash=20000, minute=self.minute())
        self.assertEqual(doubled['trades'][0]['quantity'], 19)
        histories['AAA'] = [{'date':'2026-09-04','open':100,'close':97.1715728752},
                            {'date':'2026-09-08','open':100,'close':102.8284271248}]
        predictions = api.freeze_risk_sizing(base, histories, trade_date='2026-09-09',
            frozen_at_et='2026-09-09T08:00:00-04:00', lookback=2)
        half = api.replay_day('2026-09-09', predictions, self.labels(), rules,
                              cash=10000, minute=self.minute())
        self.assertEqual(half['trades'][0]['quantity'], 4)

    def test_activation_preview_is_read_only_and_activation_records_predecessor(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp))
            before = list(Path(tmp).rglob('*'))
            preview = api.reconcile_activation(store, [], activated_at='2026-09-11T12:00:00-04:00', apply=False)
            self.assertEqual(list(Path(tmp).rglob('*')), before)
            self.assertFalse(preview['applied'])
            applied = api.reconcile_activation(store, [], activated_at='2026-09-11T12:00:00-04:00', apply=True)
            self.assertTrue(applied['applied'])
            self.assertEqual(applied['activation']['predecessor_activation_sha256'], None)
            self.assertEqual(applied['activation']['rules']['risk_fraction'], .002)

    def test_freeze_sizing_uses_only_prior_unadjusted_open_close_rows(self):
        api = self.api()
        predictions = [{'symbol':'AAA','direction':'bullish'}]
        history = {'AAA': [
            {'date':'2026-09-04','open':100,'close':101},
            {'date':'2026-09-08','open':100,'close':103},
            {'date':'2026-09-09','open':100,'close':50},
        ]}
        frozen = api.freeze_risk_sizing(predictions, history, trade_date='2026-09-09',
            frozen_at_et='2026-09-09T08:00:00-04:00', lookback=2)
        self.assertAlmostEqual(frozen[0]['risk_sizing']['sigma'], 0.01414213562373095)
        self.assertEqual(frozen[0]['risk_sizing']['latest_observation_date'], '2026-09-08')
        self.assertEqual(predictions, [{'symbol':'AAA','direction':'bullish'}])

    def test_continuity_reads_saved_old_fill_without_replaying_or_resizing(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            store = api.AccountStore(Path(tmp))
            row = self.row(method_identity='same', model_identity='model')
            store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            old = store.refresh([row])['results'][0]
            old_files = {p: p.read_bytes() for p in (store.root/'settlements').glob('*.json')}
            store.activate(api.experimental_risk_rules(), '2026-09-10T07:00:00-04:00', continuity=True)
            with patch('shaq_daily_oracle.virtual_accounts.replay_day',
                       side_effect=AssertionError('old fill replayed')):
                projected = store.refresh([row])['results'][0]
            self.assertEqual([x['quantity'] for x in projected['trades']],
                             [x['quantity'] for x in old['trades']])
            self.assertFalse(projected['replayed'])
            self.assertEqual(projected['source_settlement_hash'], old['settlement_hash'])
            self.assertTrue(all(path.read_bytes() == content for path, content in old_files.items()))

    def test_settlement_tampering_rejected_and_index_independent_rebuild(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = api.AccountStore(root)
            store.activate(api.AccountRules(), '2026-09-09T07:00:00-04:00')
            expected = store.refresh([self.row()])
            settlement = next((store.root / 'settlements').glob('*.json'))
            original = settlement.read_text()
            settlement.unlink()  # Disposable ledger can be rebuilt from frozen source.
            self.assertEqual(store.refresh([self.row()]), expected)
            self.assertEqual(settlement.read_text(), original)
            document = json.loads(original); document['net_pnl'] = 999
            settlement.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                store.refresh([self.row()])

    def test_dashboard_chooses_real_completion_not_directory_name(self):
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            index = ResearchDashboardIndex(batches_root=Path(tmp)/'batches', database=Path(tmp)/'index.db')
            batches = [dict(batch_id=b, trade_date='2026-09-09', source_valid=True) for b in ['aaa-later', 'zzz-first']]
            def detail(batch):
                variant = dict(variant={'version_sha256':'skill', 'label':'main'}, model_profile_sha256='model',
                    score_eligible=True, completed_at_et='2026-09-09T08:'+('30' if batch=='aaa-later' else '00')+':00-04:00', predictions=[])
                return dict(evidence={'cutoff_status':'on_time'}, variants={'team/main':variant}, labels={}, status={})
            with patch.object(index, 'batch_detail', side_effect=detail):
                rows = index._daily_results(batches)
            self.assertEqual([r['batch_id'] for r in rows if r['score_eligible']], ['zzz-first'])

    def test_dashboard_groups_verified_document_aliases_and_preserves_frozen_sizing(self):
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        with tempfile.TemporaryDirectory() as tmp:
            index = ResearchDashboardIndex(batches_root=Path(tmp)/'batches', database=Path(tmp)/'index.db')
            batches = [dict(batch_id=name, trade_date='2026-09-09', source_valid=True)
                       for name in ('first', 'later')]
            sizing = {'sigma':.02, 'lookback':20, 'observation_count':20,
                      'inputs_sha256':'frozen', 'frozen_at_et':'2026-09-09T08:00:00-04:00'}
            def detail(batch):
                key = 'old/alias' if batch == 'first' else 'team/current'
                variant = {'variant':{'version_sha256':batch, 'label':key},
                           'model_profile_sha256':'model', 'score_eligible':True,
                           'completed_at_et':'2026-09-09T08:'+('00' if batch == 'first' else '30')+':00-04:00',
                           'predictions':[{'symbol':'AAA','direction':'bullish','risk_sizing':sizing}]}
                return {'evidence':{'cutoff_status':'on_time'}, 'variants':{key:variant},
                        'skill_snapshots':{key:{'documents':{'skills/daily-oracle/SKILL.md':'same'}}},
                        'labels':{'labels':{}}, 'status':{}}
            with patch.object(index, 'batch_detail', side_effect=detail):
                rows = index._daily_results(batches)
            self.assertEqual(sum(row['score_eligible'] for row in rows), 1)
            self.assertEqual(len({row['series_key'] for row in rows}), 1)
            self.assertEqual(rows[0]['predictions'][0]['risk_sizing'], sizing)


if __name__ == '__main__':
    unittest.main()
