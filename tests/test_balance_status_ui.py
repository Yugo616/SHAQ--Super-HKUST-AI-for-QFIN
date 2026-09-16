import subprocess
import unittest
from pathlib import Path


class BalanceStatusUiTests(unittest.TestCase):
    def test_balance_status_explains_inclusion_missing_minutes_and_late_replay(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const ctx={window:{showCandidate(){}},renderBatch(){}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/accounts.js','utf8'),ctx);
vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/review.js','utf8'),ctx);
vm.runInContext(`
const base={batch_id:'b',variant_key:'team/shadow',trade_date:'2026-09-11',model:'subscription-default',status:'final',predictions:[{symbol:'FDS',direction:'bearish'}]};
const missing={...base,account_id:'a',scope:'historical',status:'unavailable',trades:[{symbol:'FDS',status:'unavailable_entry'}],entry_reference_at_et:'2026-09-11T09:31:00-04:00'};
const paid={...base,trade_date:'2026-09-09',batch_id:'first',account_id:'a',scope:'historical',status:'final',net_pnl:23.4,account_balance:10023.4};
const late={...base,trade_date:'2026-09-15',batch_id:'late',account_id:'a',scope:'late',status:'final',net_pnl:5.11,account_balance:null};
globalThis.missingHtml=SHAQResults.dailyHtml([base],[missing]);
globalThis.lateHtml=SHAQResults.dailyHtml([{...base,...late,score_eligible:false}],[late]);
globalThis.paidHtml=SHAQResults.dailyHtml([{...base,...paid}],[paid]);
globalThis.cards=SHAQAccounts.compactOverviewHtml({accounts:[{account_id:'a',label:'综合',model:'subscription-default',equity:10023.4,blocked:true,curve:[]}],results:[paid,missing,late]});
globalThis.unknown=SHAQResults.dailyHtml([base],[]);
globalThis.duplicate=SHAQResults.dailyHtml([base],[{...paid,trade_date:base.trade_date,batch_id:'b',scope:'duplicate',status:'duplicate'}]);
`,ctx);
assert.match(ctx.missingHtml,/FDS.*09:31/);assert.match(ctx.missingHtml,/未结算/);
assert.match(ctx.missingHtml,/data-retry-minute-date="2026-09-11"/);
assert.match(ctx.lateHtml,/超.*截止|迟到/);assert.match(ctx.lateHtml,/未计入余额/);
assert.match(ctx.paidHtml,/已计入余额/);
assert.match(ctx.cards,/2026-09-09/);assert.match(ctx.cards,/后续.*未计入/);
assert.doesNotMatch(ctx.cards+ctx.missingHtml+ctx.lateHtml,/subscription-default/);
assert.doesNotMatch(ctx.unknown,/已计入余额/);
assert.match(ctx.duplicate,/重复.*不.*入账/);
assert.equal(vm.runInContext(`SHAQAccounts.modelCaption([{response_model:'subscription-default'}])`,ctx),'');
assert.equal(vm.runInContext(`SHAQAccounts.modelCaption([{response_model:'gpt-5.4'},{response_model:'gpt-5.4'},{response_model:'claude-opus-4-6'}])`,ctx),'gpt-5.4 / claude-opus-4-6');
assert.equal(vm.runInContext(`SHAQAccounts.modelCaption([], 'qwen3')`,ctx),'qwen3');
assert.equal(vm.runInContext(`SHAQAccounts.modelCaption([], 'subscription-default')`,ctx),'');
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=Path(__file__).resolve().parents[1], check=True)
