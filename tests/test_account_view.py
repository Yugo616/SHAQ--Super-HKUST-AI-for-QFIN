import json
import unittest
from pathlib import Path
import subprocess


class AccountViewTests(unittest.TestCase):
    def render(self, name, value):
        path = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/accounts.js'
        self.assertTrue(path.exists(), 'Virtual account view must render actual settlement data')
        script = path.read_text(encoding="utf-8") + f'\nconsole.log(SHAQAccounts.{name}({json.dumps(value)}));'
        return subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8")

    def test_pending_shows_waiting_not_fake_fills(self):
        html = self.render('dayHtml', {'status':'pending','scope':'historical','orders':[], 'trades':[]})
        self.assertIn('等待', html)
        self.assertIn('规则在预测后配置', html)
        self.assertNotIn('<tbody><tr>', html)

    def test_empty_and_failed_are_explicit_without_invented_trades(self):
        empty = self.render('dayHtml', {
            'status':'empty','scope':'practice','orders':[], 'trades':[],
        })
        failed = self.render('dayHtml', {
            'status':'error','scope':'practice','orders':[], 'trades':[],
            'error':'duplicate frozen prediction',
        })
        self.assertIn('空榜', empty)
        self.assertIn('回放失败', failed)
        self.assertIn('duplicate frozen prediction', failed)
        self.assertNotIn('<tbody><tr>', empty)
        self.assertNotIn('<tbody><tr>', failed)

    def test_partially_unavailable_day_shows_backend_fills_costs_and_unfilled_rows(self):
        from test_virtual_accounts import VirtualAccountTests
        from shaq_daily_oracle.virtual_accounts import replay_day, AccountRules
        fixture = VirtualAccountTests()
        minute = fixture.minute(); minute['records']['AAA'] = []
        entry = replay_day('2026-09-09', fixture.predictions(), fixture.labels(), AccountRules(), minute=minute)
        self.assertEqual(entry['status'], 'unavailable')
        html = self.render('dayHtml', entry)
        for text in ['AAA', 'BBB', '0 股整数数量', '9 股整数数量', 'unavailable_entry', 'closed',
                     '$99.95', '$90.05', '$88.29', '$0.85', 'trade-detail', 'Completed', '开仓', '平仓']:
            self.assertIn(text, html)
        self.assertNotIn('资料不可用 · 未成交', html)
        self.assertIn('部分', html)
        self.assertIn('尚未计入持续账户净值', html)
        self.assertNotIn('最终确认', html)

    def test_entirely_unavailable_day_has_no_fabricated_fills(self):
        html = self.render('dayHtml', {'status':'unavailable', 'scope':'forward', 'orders':[],
            'trades':[{'symbol':'AAA', 'quantity':0, 'status':'unavailable_entry'}]})
        self.assertIn('AAA', html)
        self.assertIn('0 股整数数量', html)
        self.assertIn('无模拟订单', html)
        self.assertIn('尚未计入持续账户净值', html)
        self.assertNotIn('Completed', html)

    def test_final_execution_keeps_confirmation_but_shows_failed_refresh_receipt(self):
        html = self.render('dayHtml', {'status':'final', 'scope':'forward', 'orders':[], 'trades':[],
            'latest_refresh':{'status':'unavailable', 'captured_at_et':'2026-09-21T09:00:00-04:00',
                              'missing_targets':{'AAA':['entry', 'exit']}}})
        self.assertIn('已复核', html)
        self.assertIn('刷新', html)
        self.assertIn('2026-09-21', html)
        self.assertIn('AAA', html)

    def test_old_saved_aliases_normalize_to_visible_canonical_methods(self):
        versions = [
            {'author':'team','version_id':'independent-gate-1','method_name':'独立证据门禁版',
             'status_badge':'正式基准','aliases':['main']},
            {'author':'team','version_id':'cross-domain-synthesis-1','method_name':'跨域综合研判版',
             'status_badge':'Shadow','aliases':['synthesis-1']},
        ]
        selections = [
            {'author':'team','version_id':'main'},
            {'author':'team','version_id':'synthesis-1'},
            {'author':'team','version_id':'independent-gate-1'},
        ]
        path = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/accounts.js'
        script = path.read_text(encoding="utf-8") + (
            '\nconsole.log(JSON.stringify(SHAQAccounts.normalizeSelections('
            f'{json.dumps(versions)},{json.dumps(selections)})));'
        )
        value = json.loads(subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8"))
        self.assertEqual(value, [
            {'author':'team','version_id':'independent-gate-1'},
            {'author':'team','version_id':'cross-domain-synthesis-1'},
        ])

    def test_history_identity_uses_canonical_filter_without_merging_series(self):
        versions = [{
            'author':'team','version_id':'independent-gate-1',
            'method_name':'独立证据门禁版','status_badge':'正式基准',
            'aliases':['main'],
        }]
        rows = [
            {'variant_key':'team/main','series_key':'team/main:old-hash:model:rules',
             'label':'旧 main 名称'},
            {'variant_key':'team/independent-gate-1',
             'series_key':'team/independent-gate-1:new-hash:model:rules',
             'label':'不应显示的新别名'},
            {'variant_key':'guest/removed-1','series_key':'guest/removed-1:hash:model:rules',
             'label':'冻结的已移除版本'},
        ]
        path = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/accounts.js'
        script = path.read_text(encoding="utf-8") + (
            '\nconsole.log(JSON.stringify('
            f'{json.dumps(rows)}.map(row=>SHAQAccounts.historyIdentity(row,{json.dumps(versions)}))));'
        )
        identities = json.loads(subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8"))

        self.assertEqual(identities[0]['filter_key'], 'team/independent-gate-1')
        self.assertEqual(identities[1]['filter_key'], 'team/independent-gate-1')
        self.assertNotEqual(identities[0]['series_key'], identities[1]['series_key'])
        self.assertEqual(identities[0]['method_name'], '独立证据门禁版')
        self.assertEqual(identities[0]['status_badge'], '正式基准')
        self.assertEqual(identities[2]['filter_key'], 'guest/removed-1')
        self.assertEqual(identities[2]['method_name'], '冻结的已移除版本')
        self.assertEqual(identities[2]['status_badge'], '历史记录')

    def test_history_chart_uses_metadata_badges_and_keeps_account_series_distinct(self):
        root = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop'
        accounts = (root / 'accounts.js').read_text(encoding="utf-8")
        workbench = (root / 'workbench.js').read_text(encoding="utf-8")
        versions = [{
            'author':'team','version_id':'independent-gate-1',
            'method_name':'独立证据门禁版','status_badge':'正式基准',
            'aliases':['main'],
        }]
        rows = [
            {'variant_key':'team/main','series_key':'team/main:old-hash:model:rules',
             'label':'旧 main 名称','trade_date':'2026-09-08','daily_pnl':1,
             'cumulative_pnl':1,'score_eligible':True},
            {'variant_key':'team/independent-gate-1',
             'series_key':'team/independent-gate-1:new-hash:model:rules',
             'label':'另一个旧名称','trade_date':'2026-09-09','daily_pnl':2,
             'cumulative_pnl':2,'score_eligible':True},
        ]
        harness = f'''
const window={{showCandidate(){{}}}};
const state={{data:{{versions:{json.dumps(versions)},skill_explanations:{{}}}},page:"history"}};
const esc=x=>String(x??"");
const q=()=>({{parentElement:{{prepend(){{}}}}}}); const qa=()=>[];
let renderEditor=()=>{{}},renderHistory=()=>{{}},renderBatch=()=>{{}},loadSkill=async()=>{{}},
 renderRun=()=>{{}},showPage=()=>{{}},saveDraft=async()=>{{}},estimate=async()=>{{}};
const setInterval=()=>{{}};
'''
        script = accounts + '\n' + harness + workbench + f'\nconsole.log(plotResults({json.dumps(rows)}));'
        output = subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8")

        self.assertNotIn('旧 main 名称', output)
        self.assertNotIn('另一个旧名称', output)
        self.assertEqual(output.count('独立证据门禁版'), 2)
        self.assertEqual(output.count('正式基准'), 2)

    def test_old_label_filter_uses_canonical_key_but_keeps_separate_accounts(self):
        versions = [{
            'author':'team','version_id':'independent-gate-1',
            'method_name':'独立证据门禁版','status_badge':'正式基准',
            'aliases':['main'],
        }]
        account = lambda key, series, equity: {
            'variant_key':key, 'series_key':series, 'label':'旧冻结名称',
            'model':'fixture', 'engine':'zipline-reloaded','engine_version':'3.1.1',
            'equity':equity,'gross_equity':equity,'fees':0,'slippage_cost':0,
            'max_drawdown':0,'curve':[{'date':'2026-09-09','equity':equity}],
            'rules':{'initial_cash':10000},
        }
        data = {
            'rules':{'initial_cash':10000,'per_prediction_budget':1000,
                     'commission_rate':0.0005,'slippage_rate':0.0005},
            'accounts':[
                account('team/main','team/main:old-hash:model:rules',10001),
                account('team/independent-gate-1','team/independent-gate-1:new-hash:model:rules',20002),
                account('guest/removed-1','guest/removed-1:hash:model:rules',30003),
            ],
            'results':[], 'legacy':{'status':'unavailable','results':[]},
        }
        path = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/accounts.js'
        script = path.read_text(encoding="utf-8") + (
            '\nconsole.log(SHAQAccounts.overviewHtml('
            f'{json.dumps(data)},{json.dumps(versions)},'
            '{version:"team/independent-gate-1"}));'
        )
        output = subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8")

        self.assertIn('$10,001.00', output)
        self.assertIn('$20,002.00', output)
        self.assertNotIn('$30,003.00', output)
        self.assertNotIn('旧冻结名称', output)

    def test_trade_details_escape_text_and_separate_costs(self):
        html = self.render('dayHtml', {'status':'final','scope':'forward','net_pnl':8,
            'gross_pnl':10,'fees':1,'slippage_cost':1,'closing_cash':10008,
            'entry_reference_at_et':'2026-09-09T09:31:00-04:00',
            'exit_reference_at_et':'2026-09-09T15:55:00-04:00',
            'processed_at_et':'2026-09-09T16:11:12-04:00',
            'trades':[{'symbol':'<script>','direction':'bearish','quantity':2,
                'entry_reference_open':100,'exit_reference_open':95,
                'entry_price':99.95,'exit_price':95.0475,'fees':1,
                'slippage_cost':1,'net_pnl':8,'gross_pnl':10,
                'official_open':101,'official_close':99,'direction_correct':True}]})
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('卖空', html)
        self.assertIn('手续费', html)
        self.assertIn('零成本', html)
        self.assertIn('10,008.00', html)
        self.assertIn('09:31', html)
        self.assertIn('参考价', html)
        self.assertIn('模拟成交价', html)
        self.assertIn('官方 O→C 方向成绩', html)
        self.assertIn('正确', html)
        self.assertIn('收盘后模拟回放', html)

    def test_provisional_and_incomplete_are_explicit_and_never_look_final(self):
        provisional = self.render('dayHtml', {
            'status':'provisional','scope':'forward','trades':[], 'orders':[],
            'captured_at_et':'2026-09-09T16:10:00-04:00',
        })
        incomplete = self.render('dayHtml', {
            'status':'incomplete','scope':'forward','trades':[{
                'symbol':'AAA','direction':'bullish','quantity':3,
                'entry_reference_open':100,'entry_price':100.05,
                'status':'open_incomplete','net_pnl':None,
            }], 'orders':[], 'closing_positions':{'AAA':3},
        })
        self.assertIn('初步', provisional)
        self.assertIn('已计入持续账户净值', provisional)
        self.assertNotIn('最终确认', provisional)
        self.assertIn('未完成', incomplete)
        self.assertIn('仍有模拟持仓', incomplete)

    def test_historical_provisional_never_claims_forward_account_entry(self):
        html = self.render('dayHtml', {
            'status':'provisional','scope':'historical','trades':[], 'orders':[],
            'closing_cash':10023.4,
        })
        self.assertIn('初步', html)
        self.assertIn('历史回放余额', html)
        self.assertNotIn('已计入持续账户净值', html)

    def test_daily_account_row_shows_score_net_cumulative_and_balance(self):
        html = self.render('overviewHtml', {
            'rules':{'initial_cash':10000,'per_prediction_budget':1000,
                     'commission_rate':0.0005,'slippage_rate':0.0005},
            'accounts':[], 'legacy':{'results':[]},
            'results':[{'batch_id':'b','variant_key':'team/main','scope':'forward',
                'trade_date':'2026-09-09','status':'provisional','net_pnl':8,
                'account_cumulative_net_pnl':8,'account_balance':10008,
                'gross_pnl':10,'fees':1,'slippage_cost':1,
                'trades':[{'direction_correct':True},{'direction_correct':False}]}],
        })
        self.assertIn('正确 / 错误', html)
        self.assertIn('$8.00', html)
        self.assertIn('$10,008.00', html)

    def test_pending_score_is_dash_and_historical_table_matches_row_width(self):
        html = self.render('overviewHtml', {
            'rules':{'initial_cash':10000,'per_prediction_budget':1000,
                     'commission_rate':0.0005,'slippage_rate':0.0005},
            'accounts':[], 'legacy':{'results':[]},
            'results':[{'batch_id':'b','variant_key':'team/main','scope':'historical',
                'trade_date':'2026-09-09','status':'pending','trades':[]}],
        })
        self.assertIn('<td>等待收盘后的分钟资料</td><td>—</td>', html)
        self.assertEqual(html.count('<th>账户余额</th>'), 2)

    def test_complete_minute_settlement_waits_for_complete_daily_direction_score(self):
        base = {'variant_key':'team/main','scope':'forward','trade_date':'2026-09-09',
                'net_pnl':1,'gross_pnl':1,'fees':0,'slippage_cost':0}
        html = self.render('overviewHtml', {
            'rules':{'initial_cash':10000,'per_prediction_budget':1000,
                     'commission_rate':0.0005,'slippage_rate':0.0005},
            'accounts':[], 'legacy':{'results':[]},
            'results':[
                {**base,'batch_id':'p','status':'provisional',
                 'trades':[{'direction_correct':None}]},
                {**base,'batch_id':'f','status':'final',
                 'trades':[{'direction_correct':True},{'direction_correct':None}]},
                {**base,'batch_id':'e','status':'empty','trades':[]},
            ],
        })
        self.assertEqual(html.count('<td>0 / 0</td>'), 1, 'only an empty forecast is zero trades')
        self.assertEqual(html.count('<td>—</td>'), 8)
        self.assertNotIn('<td>1 / 0</td>', html)

    def test_account_overview_separates_forward_research_and_legacy(self):
        versions = [
            {'author':'team','version_id':'independent-gate-1','method_name':'独立证据门禁版',
             'status_badge':'正式基准','aliases':['main']},
            {'author':'team','version_id':'cross-domain-synthesis-1','method_name':'跨域综合研判版',
             'status_badge':'Shadow','aliases':['synthesis-1']},
        ]
        value = {
            'rules':{'initial_cash':10000,'per_prediction_budget':1000,
                     'commission_rate':0.0005,'slippage_rate':0.0005},
            'accounts':[{'series_key':'team/independent-gate-1:hash:model','label':'legacy alias',
                         'model':'fixture-model','engine':'zipline-reloaded','engine_version':'3.1.1',
                         'equity':10008,'gross_equity':10010,'fees':1,'slippage_cost':1,
                         'max_drawdown':0.02,'curve':[]}],
            'results':[
                {'variant_key':'team/independent-gate-1','scope':'forward','status':'final',
                 'trade_date':'2026-09-09','net_pnl':8,'gross_pnl':10,'fees':1,'slippage_cost':1},
                {'variant_key':'team/cross-domain-synthesis-1','scope':'practice','status':'empty',
                 'trade_date':'2026-09-09','net_pnl':0,'gross_pnl':0,'fees':0,'slippage_cost':0},
            ],
            'legacy':{'read_only':True,'status':'saved_only','results':[{
                'trade_date':'2026-09-08','engine':'backtrader','status':'settled','net_pnl':42,
            }]},
        }
        path = Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/accounts.js'
        script = path.read_text(encoding="utf-8") + (
            '\nconsole.log(SHAQAccounts.overviewHtml('
            f'{json.dumps(value)},{json.dumps(versions)},{{}}));'
        )
        html = subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8")
        self.assertIn('独立证据门禁版', html)
        self.assertIn('正式基准', html)
        self.assertIn('Zipline-reloaded 3.1.1', html)
        self.assertIn('持续账户', html)
        self.assertIn('历史 / 练习（不入账）', html)
        self.assertIn('旧版已保存结果（只读）', html)
        self.assertIn('Backtrader', html)
        self.assertIn('saved-only', html)

    def test_equity_plot_has_no_fake_intraday_path(self):
        html = self.render('plot', [{'label':'main','curve':[{'date':'2026-09-09','equity':9990},
            {'date':'2026-09-10','equity':10020}]}])
        self.assertIn('收盘净值', html)
        self.assertIn('2026-09-09', html)
        self.assertIn('main', html)


if __name__ == '__main__':
    unittest.main()
