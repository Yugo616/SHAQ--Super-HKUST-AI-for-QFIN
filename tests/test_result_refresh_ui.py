import subprocess
import unittest
from pathlib import Path


class ResultRefreshUiTests(unittest.TestCase):
    def test_refresh_status_distinguishes_success_partial_failure_and_failure(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const start=source.indexOf('function refreshStatusText(');
const section=start<0?'':source.slice(start,source.indexOf('function metric('));
const ctx={};vm.createContext(ctx);vm.runInContext(section,ctx);
assert.equal(typeof ctx.refreshStatusText,'function','UI needs observable refresh states');
assert.match(ctx.refreshStatusText({status:'running'}),/正在/);
assert.match(ctx.refreshStatusText({status:'complete',completed_at:'2026-09-09T17:00:00-04:00'}),/完成/);
assert.match(ctx.refreshStatusText({status:'partial_failure',failure_count:2}),/2/);
assert.match(ctx.refreshStatusText({status:'failed',error:'offline'}),/offline/);
'''
        subprocess.run(['node', '-'], input=script, text=True, cwd=root, check=True)


if __name__ == '__main__':
    unittest.main()
