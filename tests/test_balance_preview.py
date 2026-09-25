import copy
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.balance_preview import project_saved_balances


class BalancePreviewTests(unittest.TestCase):
    def test_dashboard_preview_is_opt_in_and_formal_payload_is_unchanged(self):
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        source=dict(rules={'initial_cash':10000},results=[self.row('2026-09-09',5)],accounts=[])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            dashboard=ResearchDashboardIndex(batches_root=root/'batches',database=root/'index.sqlite')
            with patch('shaq_daily_oracle.virtual_accounts.AccountStore.view',return_value=source):
                disabled=dashboard.overview()
                self.assertNotIn('balance_preview',disabled)
                (root/'balance_preview.json').write_text(json.dumps({'enabled':True}))
                enabled=dashboard.overview()
        self.assertIsNone(source['results'][0]['account_balance'])
        self.assertEqual(enabled['virtual_accounts'],disabled['virtual_accounts'])
        self.assertEqual(enabled['balance_preview']['results'][0]['account_balance'],10005)

    def row(self, day, pnl, *, batch=None, method='one', status='final', scope='late'):
        return dict(trade_date=day, batch_id=batch or day, variant_key=method,
                    method_identity=method, label=method, status=status, scope=scope,
                    net_pnl=pnl, account_balance=None, trades=[])

    def project(self, rows, daily=None):
        return project_saved_balances(dict(rules={'initial_cash':10000}, results=rows), daily or [])

    def test_all_scopes_accumulate_without_changing_source(self):
        rows=[self.row('2026-09-09',20,scope='historical'),
              self.row('2026-09-10',-5), self.row('2026-09-11',7,scope='forward')]
        before=copy.deepcopy(rows)
        result=self.project(rows)
        self.assertEqual(rows,before)
        self.assertEqual([r['account_balance'] for r in result['results']],[10020,10015,10022])
        self.assertEqual(result['accounts'][0]['equity'],10022)
        self.assertEqual([p['equity'] for p in result['accounts'][0]['curve']],[10020,10015,10022])

    def test_once_per_method_day_uses_completion_not_profit_or_directory(self):
        rows=[self.row('2026-09-09',100,batch='a'),self.row('2026-09-09',-2,batch='z')]
        daily=[dict(batch_id='a',variant_key='one',completed_at_et='2026-09-09T10:00:00-04:00'),
               dict(batch_id='z',variant_key='one',completed_at_et='2026-09-09T09:00:00-04:00')]
        result=self.project(rows,daily)
        self.assertEqual(result['accounts'][0]['equity'],9998)
        self.assertIsNone(result['results'][0]['account_balance'])
        self.assertEqual(result['results'][1]['account_balance'],9998)
        self.assertEqual(self.project(list(reversed(rows)),daily)['accounts'],result['accounts'])

    def test_missing_prices_are_blank_not_zero_and_methods_stay_separate(self):
        result=self.project([self.row('2026-09-09',3),
                             self.row('2026-09-10',None,status='unavailable'),
                             self.row('2026-09-09',8,method='two')])
        self.assertIsNone(result['results'][1]['account_balance'])
        self.assertFalse(result['results'][1]['balance_preview_counted'])
        accounts={r['method_identity']:r for r in result['accounts']}
        self.assertEqual(accounts['one']['equity'],10003)
        self.assertEqual(accounts['two']['equity'],10008)
        self.assertEqual(len(accounts['one']['curve']),1)

    def test_empty_counts_but_nonfinite_or_partial_pnl_does_not(self):
        result=self.project([self.row('2026-09-09',0,status='empty'),
                             self.row('2026-09-10',15,status='incomplete'),
                             self.row('2026-09-11',float('nan'))])
        self.assertEqual([r['account_balance'] for r in result['results']],[10000,None,None])
        self.assertEqual(result['accounts'][0]['sessions'],1)

    def test_aliases_use_existing_method_identity_and_old_duplicates_stay_excluded(self):
        a=self.row('2026-09-09',4);a['variant_key']='old/alias'
        b=self.row('2026-09-10',6)
        duplicate=self.row('2026-09-09',None,status='duplicate')
        result=self.project([a,b,duplicate])
        self.assertEqual(len(result['accounts']),1)
        self.assertEqual(result['accounts'][0]['equity'],10010)
        self.assertFalse(result['results'][2]['balance_preview_counted'])
