import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shaq_daily_oracle.module_rules import execute_rule, select_symbols, test_rule
from shaq_daily_oracle.public_data import DailyBarCache
from shaq_daily_oracle.research_schedule import due_status


class WorkbenchTests(unittest.TestCase):
    def test_screener_changes_candidates_without_access_to_other_symbols(self):
        pool = [{'symbol': 'AAA'}, {'symbol': 'BBB'}]
        self.assertEqual(select_symbols('function compute(x){return {symbols:[x.candidates[1].symbol]}}', pool, 1), ['BBB'])
        with self.assertRaises(ValueError):
            select_symbols('function compute(x){return {symbols:["CCC"]}}', pool, 1)

    def test_module_has_no_file_or_network_access(self):
        result = execute_rule('function compute(x){return {fs:typeof require,net:typeof fetch,proc:typeof process}}', {})
        self.assertEqual(result, {'fs':'undefined','net':'undefined','proc':'undefined'})
        with self.assertRaises(Exception):
            execute_rule('function compute(x){return {next_return:1}}', {})

    def test_module_fixed_cases_reject_incorrect_formula(self):
        cases = json.dumps({'reference':'test arithmetic', 'cases':[{'input':{'x':2}, 'expected':{'value':4}}]})
        with self.assertRaises(ValueError):
            test_rule('function compute(x){return {value:x.x+1}}', cases)
        self.assertEqual(test_rule('function compute(x){return {value:x.x*2}}', cases)['case_count'],1)

    def test_schedule_uses_new_york_time_and_skips_weekends(self):
        for when, expected in [('2026-09-08T12:34:00+00:00','waiting'), ('2026-09-08T12:35:00+00:00','due'), ('2026-09-08T12:50:00+00:00','missed'), ('2026-09-05T12:35:00+00:00','closed'), ('2026-11-09T13:35:00+00:00','due')]:
            self.assertEqual(due_status(datetime.fromisoformat(when), '08:35'), expected)

    def test_missing_symbol_does_not_replace_good_bars_with_stale_data(self):
        class Provider:
            def history(self, symbols, **kwargs):
                return {'AAA':[{'timestamp':'2026-09-04T00:00:00','close':10}], 'BBB':[]}
        with tempfile.TemporaryDirectory() as tmp:
            cache = DailyBarCache(Provider(), Path(tmp), overlap_days=7)
            result = cache.history(['AAA','BBB'], start=date(2026,9,1), end=date(2026,9,8))
            self.assertEqual(result['AAA'][0]['close'],10)
            self.assertEqual(result['BBB'],[])

    def test_nasdaq_calendar_does_not_pass_actual_earnings_as_expectations(self):
        from shaq_daily_oracle.public_data import earnings_expectations
        rows = [{'symbol':'AAA','epsForecast':'$2.1','eps':'$3.5','surprise':'66%','time':'time-after-hours'}]
        self.assertEqual(earnings_expectations(rows), [{'symbol':'AAA','epsForecast':'$2.1','time':'time-after-hours'}])

    def test_research_publication_limit_is_explicit_and_validated(self):
        from shaq_daily_oracle.decision_sandbox import decision_parameters
        self.assertEqual(decision_parameters('{}')['maximum_predictions'],3)
        self.assertEqual(decision_parameters('{"parameters":{"maximum_predictions":5}}')['maximum_predictions'],5)
        for value in [-1,True,2.5]:
            with self.assertRaises(ValueError):
                decision_parameters(json.dumps({'parameters':{'maximum_predictions':value}}))

    def test_market_view_preserves_every_close_and_volume_without_changing_stock_bars(self):
        from shaq_daily_oracle.research_batch import market_input_view
        bars=[{'timestamp':'2026-09-03','close':100,'volume':1000,'open':99}, {'timestamp':'2026-09-04','close':102,'volume':900,'open':101}]
        raw={'daily_and_premarket':{'SPY':{'daily':{'bars':bars,'previous_return':.02},'premarket':{'return':.01}}}}
        view=market_input_view(raw)
        self.assertEqual(view['daily_and_premarket']['SPY']['daily']['path']['close'],[100,102])
        self.assertEqual(view['daily_and_premarket']['SPY']['daily']['path']['volume'],[1000,900])
        self.assertIn('bars',raw['daily_and_premarket']['SPY']['daily'])
        self.assertEqual(market_input_view({'daily':{'bars':bars}}),{'daily':{'bars':bars}})

    def test_shared_prompt_evidence_can_be_reconstructed_without_loss(self):
        from shaq_daily_oracle.research_batch import shared_task_inputs
        tasks=[{'task_id':s,'evidence':[{'evidence_id':'market','content':{'price':12},'provider':'test'}]} for s in ['A','B']]
        pool, compact=shared_task_inputs(tasks)
        self.assertEqual(len(pool),1)
        restored=[{**t,'evidence':[{**e,'content':pool[e['content_ref']]} for e in t['evidence']]} for t in compact]
        for t in restored:
            for e in t['evidence']: del e['content_ref']
        self.assertEqual(restored,tasks)
