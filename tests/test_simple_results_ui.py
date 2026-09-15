import subprocess
import unittest
from pathlib import Path


class SimpleResultsUiTests(unittest.TestCase):
    def test_daily_rows_use_saved_unadjusted_prices_and_keep_unknowns_empty(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const ctx={window:{showCandidate(){}},renderBatch(){}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/accounts.js','utf8'),ctx);
vm.runInContext(fs.readFileSync('src/shaq_daily_oracle/desktop/review.js','utf8'),ctx);
assert.equal(vm.runInContext('typeof SHAQResults',ctx),'object','saved per-symbol outcomes need a display renderer');
const html=vm.runInContext(`SHAQResults.dailyHtml([
 {batch_id:'b',variant_key:'team/main',trade_date:'2026-09-15',predictions:[{symbol:'AAA',direction:'bearish'},{symbol:'BBB',direction:'bullish'}],
 labels:{AAA:{status:'final',official_unadjusted_open:100,official_unadjusted_close:90},BBB:{status:'pending'}}}
],[],[{author:'team',version_id:'main',method_name:'独立证据门禁版'}])`,ctx);
assert.match(html,/AAA/);assert.match(html,/BBB/);assert.match(html,/\$100\.00/);
assert.match(html,/-10\.00%/);assert.match(html,/正确/);assert.match(html,/data-result-symbol="BBB"/);
assert.doesNotMatch(html,/一股|手续费|滑点|<details/);
const missing=html.slice(html.indexOf('data-result-symbol="BBB"'));
assert.doesNotMatch(missing,/\$0\.00|0\.00%|>错误</);
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8', cwd=root, check=True)
