import json
import subprocess
import unittest
from pathlib import Path


class TodayProgressTests(unittest.TestCase):
    def run_js(self, body):
        module = Path(__file__).resolve().parents[1] / 'src/shaq_daily_oracle/desktop/today_progress.js'
        self.assertTrue(module.exists(), 'The current-day progress view is not implemented')
        return subprocess.check_output(['node', '-'], input=f'const ui=require({json.dumps(str(module))});\n'+body,
                                       text=True, encoding='utf-8')

    def test_new_york_day_hides_yesterday_but_keeps_unfinished_work(self):
        self.run_js("""
const assert=require('node:assert/strict');
const jobs=[
 {job_id:'old',status:'complete',started_at_et:'2026-09-09T08:00:00-04:00'},
 {job_id:'today',status:'complete',started_at_et:'2026-09-10T08:00:00-04:00'},
 {job_id:'overnight',status:'running',started_at_et:'2026-09-09T23:00:00-04:00'},
 {job_id:'queued',status:'queued',started_at_et:null},
 {job_id:'invalid',status:'complete',started_at_et:'bad'}];
assert.deepEqual(ui.currentJobs(jobs,'2026-09-11T00:30:00+08:00').map(j=>j.job_id), ['overnight','queued','today']);
assert.equal(ui.etDay('2026-11-09T04:30:00Z'),'2026-11-08');
assert.equal(ui.etDay('2026-09-10T04:30:00Z'),'2026-09-10');
""")

    def test_render_keeps_versions_under_their_batch_and_no_old_results(self):
        html = self.run_js("""
console.log(ui.progressHtml([
 {job_id:'yesterday',status:'complete',started_at_et:'2026-09-09T08:00:00-04:00',message:'OLD'},
 {job_id:'one',status:'running',started_at_et:'2026-09-10T08:00:00-04:00',message:'COLLECT',variant_progress:{'team/main':'complete','alice/new':'running'}},
 {job_id:'two',status:'partial_failure',started_at_et:'2026-09-10T07:00:00-04:00',batch_id:'b2',message:'PARTIAL',variant_progress:{'team/main':'complete','bob/new':'failed'}}
], [{author:'alice',version_id:'new',method_name:'<unsafe>'}], '2026-09-10T08:20:00-04:00'));
""")
        self.assertNotIn('OLD', html)
        self.assertLess(html.index('COLLECT'), html.index('&lt;unsafe&gt;'))
        self.assertLess(html.index('&lt;unsafe&gt;'), html.index('PARTIAL'))
        self.assertIn('data-progress-result="b2"', html)
        self.assertIn('data-progress-retry="two"', html)
        self.assertNotIn('<unsafe>', html)

    def test_only_failed_versions_are_selected_for_retry(self):
        self.run_js("""
const assert=require('node:assert/strict');
assert.deepEqual(ui.retryVersions({variant_progress:{'team/main':'complete','bob/new':'failed'}}), [{author:'bob',version_id:'new'}]);
assert.deepEqual(ui.retryVersions({status:'failed',variant_progress:{'team/main':'queued'}}), [{author:'team',version_id:'main'}]);
assert.deepEqual(ui.retryVersions({status:'complete',variant_progress:{'team/main':'complete'}}), []);
assert.match(ui.scheduleText({enabled:false}), /未开启/);
assert.doesNotMatch(ui.scheduleText({enabled:false,local_start:'下次启动：明天'}), /明天/);
""")
