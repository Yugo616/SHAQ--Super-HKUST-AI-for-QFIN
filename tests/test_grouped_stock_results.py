import subprocess
import unittest
from pathlib import Path


class GroupedStockResultsTests(unittest.TestCase):
    def test_chart_uses_all_counted_balances_and_hides_internal_explanations(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');const ctx={};vm.createContext(ctx);
for(const f of ['accounts.js','review.js'])vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/'+f,'utf8'),ctx);
const html=vm.runInContext(`SHAQAccounts.compactOverviewHtml({rules:{initial_cash:10000},accounts:[{account_id:'a',equity:10049.88,curve:[{date:'2026-09-16',equity:10049.88}]}],results:[
{account_id:'a',scope:'historical',status:'final',trade_date:'2026-09-09',account_balance:10023.4},
{account_id:'a',scope:'historical',status:'final',trade_date:'2026-09-11',account_balance:10022.86},
{account_id:'a',scope:'late',status:'final',trade_date:'2026-09-15',account_balance:99999},
{account_id:'a',scope:'forward',status:'final',trade_date:'2026-09-16',account_balance:10049.88}]} )`,ctx);
for(const v of ['2026-09-09','2026-09-11','2026-09-16','$10,023.40','$10,022.86','$10,049.88','余额变化'])assert.ok(html.includes(v),v);
assert.ok(!html.includes('$99,999.00'));
const row=vm.runInContext(`SHAQResults.dailyHtml([{status:'final',score_eligible:true,model:'gpt6astra',timing_assessment:{reassessed:true}}])`,ctx);
assert.match(row,/gpt6astra · 已正常运行/);assert.doesNotMatch(row,/规则重评/);
assert.ok(!fs.readFileSync('src/shaq_daily_oracle/desktop/review.js','utf8').includes('勾选日期与版本，比较整组结果'));
'''
        subprocess.run(['node','-'],input=script,text=True,check=True,cwd=Path(__file__).resolve().parents[1])

    def test_balance_heading_uses_matching_version_not_old_author_alias(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');const ctx={};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/accounts.js','utf8'),ctx);
const html=vm.runInContext(`SHAQAccounts.compactOverviewHtml({accounts:[{account_id:'id',series_key:'hash:model',label:'old author alias',equity:10010}],results:[{account_id:'id',variant_key:'team/new',trade_date:'2026-09-16',scope:'forward',status:'final',account_balance:10010}]},[{author:'team',version_id:'new',method_name:'跨域综合研判版'}])`,ctx);
assert.match(html,/跨域综合研判版/);assert.doesNotMatch(html,/old author alias/);
'''
        subprocess.run(['node','-'],input=script,text=True,check=True,cwd=Path(__file__).resolve().parents[1])

    def test_group_header_and_independent_stock_pnl(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const ctx={};vm.createContext(ctx);
for(const file of ['accounts.js','review.js'])vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/'+file,'utf8'),ctx);
const html=vm.runInContext(`SHAQResults.dailyHtml([{batch_id:'b',variant_key:'v',trade_date:'2026-09-11',
model:'gpt-test',status:'final',score_eligible:true,predictions:[{symbol:'AAA',direction:'bullish'},{symbol:'BBB',direction:'bearish'},{symbol:'FDS',direction:'bearish'}]}],
[{batch_id:'b',variant_key:'v',status:'unavailable',net_pnl:7,trades:[
{symbol:'AAA',status:'closed',net_pnl:12},{symbol:'BBB',status:'closed',net_pnl:-5},
{symbol:'FDS',status:'unavailable_entry',net_pnl:0}]}])`,ctx);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,1);
assert.match(html,/本股净盈亏/);assert.match(html,/\+\$12\.00/);assert.match(html,/-\$5\.00/);
assert.match(html,/gpt-test · 已正常运行/);
assert.match(html,/缺少行情/);
assert.doesNotMatch(html,/方向已复核|方向空榜|subscription-default/);
const late=vm.runInContext(`SHAQResults.dailyHtml([{batch_id:'b',variant_key:'v',status:'final',score_eligible:false,predictions:[]}])`,ctx);
assert.match(late,/过时结果，仅供参考/);
'''
        subprocess.run(['node', '-'], input=script, text=True, check=True,
                       cwd=Path(__file__).resolve().parents[1])
