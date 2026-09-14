import json
import subprocess
import unittest
from pathlib import Path


class TodayProgressTests(unittest.TestCase):
    def test_failed_original_batch_remains_recoverable_after_its_trading_day(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const job={job_id:'old',batch_id:'LAB-old',status:'partial_failure',
 started_at_et:'2026-09-01T08:00:00-04:00',variant_progress:{'team/main':'failed'}};
const html=ui.progressHtml([job],[],'2026-09-15T08:00:00-04:00');
assert.match(html,/恢复原批次/);
assert.match(html,/data-progress-retry="old"/);
""")
    def test_workbench_render_applies_backend_guard_and_shows_reason(self):
        root = Path(__file__).resolve().parents[1] / 'src/shaq_daily_oracle/desktop'
        source = (root/'today_progress.js').read_text() + '\n' + '''
const window={showCandidate(){}};const document={activeElement:null};
let renderEditor=()=>{},renderHistory=()=>{},renderBatch=()=>{},loadSkill=()=>{},renderRun=()=>{},
    showPage=()=>{},saveDraft=()=>{},estimate=()=>{};
const startBatch=()=>{},setInterval=()=>{},esc=String,qa=()=>[],versionKey=v=>v.author+'/'+v.version_id,selectedVersions=()=>[];
const nodes={};const q=s=>nodes[s]||=( {parentElement:{prepend(){}},classList:{toggle(){}},contains(){return false},replaceWith(){},disabled:false,textContent:'',innerHTML:''} );
const state={runSelections:null,data:{settings:{model_profiles:[{model:'fixture',profile_id:'p'}]},
 versions:[],jobs:[],clock:{today_available:false,is_trading_day:false,next_trade_date:'2026-09-14',today_message:'今日休市；不启动今日研究。'}}};
''' + (root/'workbench.js').read_text() + '''
renderAutomatic=()=>{};renderRun();
console.log(JSON.stringify({disabled:q('#start-batch').disabled,reason:q('.run-toolbar > span').textContent}));
'''
        value = json.loads(subprocess.check_output(['node','-'], input=source, text=True))
        self.assertTrue(value['disabled'])
        self.assertIn('今日休市', value['reason'])
        self.assertIn('2026-09-14', value['reason'])

    def test_today_button_fails_closed_without_backend_calendar_permission(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
for(const clock of [{}, {today_available:false,today_message:'今日休市'},
                   {today_available:false,today_message:'尚未到美东 04:00'}]){
 const button={disabled:false,title:''}; ui.applyTodayAvailability(button,clock,true);
 assert.equal(button.disabled,true);
}
const button={};ui.applyTodayAvailability(button,{today_available:true,today_message:'采集时核验'},true);
assert.equal(button.disabled,false);
ui.applyTodayAvailability(button,{today_available:true},false);assert.equal(button.disabled,true);
""")
    def run_js(self, body):
        module = Path(__file__).resolve().parents[1] / 'src/shaq_daily_oracle/desktop/today_progress.js'
        self.assertTrue(module.exists(), 'The current-day progress view is not implemented')
        return subprocess.check_output(['node', '-'], input=f'const ui=require({json.dumps(str(module))});\n'+body,
                                       text=True, encoding='utf-8')

    def test_new_york_day_hides_yesterday_but_keeps_unfinished_work(self):
        self.run_js(r"""
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
        self.run_js(r"""
const assert=require('node:assert/strict');
assert.deepEqual(ui.retryVersions({variant_progress:{'team/main':'complete','bob/new':'failed'}}), [{author:'bob',version_id:'new'}]);
assert.deepEqual(ui.retryVersions({status:'failed',variant_progress:{'team/main':'queued'}}), [{author:'team',version_id:'main'}]);
assert.deepEqual(ui.retryVersions({status:'complete',variant_progress:{'team/main':'complete'}}), []);
assert.match(ui.scheduleText({enabled:false}), /未开启/);
assert.doesNotMatch(ui.scheduleText({enabled:false,local_start:'下次启动：明天'}), /明天/);
""")

    def test_research_timeline_is_honest_escaped_and_keeps_selection(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const events=[
 {sequence:1,stage:'call_requested',variant_key:'team/main',domain:'market',symbols:['AAPL','MSFT'],call_id:'c',attempt:1,status:'requested',occurred_at_et:'2026-09-11T08:00:00-04:00'},
 {sequence:2,stage:'report_validated',variant_key:'team/main',domain:'market',symbol:'AAPL',status:'complete',occurred_at_et:'2026-09-11T08:00:02-04:00',report:{thesis:'<b>x</b>',antithesis:'counter',unknowns:['u'],invalidation:['i'],evidence:[{provider:'SEC',captured_at:'08:00',source_uri:'https://example.test'}]}}
];
const html=ui.researchHtml(events,[],{variant:'team/main',symbol:'AAPL',open:['timeline','domain-market','original-AAPL-market']});
assert.match(html,/AAPL、MSFT/);
assert.match(html,/实际任务 2/);
assert.match(html,/&lt;b&gt;x&lt;\/b&gt;/);
assert.doesNotMatch(html,/<b>x<\/b>/);
assert.match(html,/data-research-symbol="AAPL"[^>]*selected/);
assert.match(html,/data-research-section="domain-market" open/);
assert.match(html,/data-research-section="timeline" open/);
assert.match(html,/data-research-section="original-AAPL-market" open/);
assert.doesNotMatch(html,/%/);
const legacy=ui.researchHtml([], [{domain:'event',thesis:'saved'}], {});
assert.match(legacy,/旧记录没有执行时间线/);
assert.match(legacy,/saved/);
const failed=ui.researchHtml([
 {stage:'call_requested',variant_key:'team/main',domain:'market',symbols:['AAPL'],call_id:'bad',attempt:1,status:'requested',occurred_at_et:'2026-09-11T08:00:00-04:00'},
 {stage:'failure',variant_key:'team/main',domain:'market',symbols:['AAPL'],call_id:'bad',attempt:1,status:'failed',occurred_at_et:'2026-09-11T08:00:01-04:00'}
],[],{variant:'team/main',symbol:'AAPL'});
assert.match(failed,/进行中 0/);
assert.match(failed,/失败/);
""")
