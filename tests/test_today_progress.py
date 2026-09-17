import json
import subprocess
import unittest
from pathlib import Path


class TodayProgressTests(unittest.TestCase):
    def test_terminal_legacy_progress_never_uses_loading_animation(self):
        self.run_js(r'''
const assert=require('node:assert/strict');
const render=(status,value,events=[])=>ui.progressHtml([{job_id:'j',started_at_et:'2026-09-15T08:00:00-04:00',
 status,variant_progress:{v:value},research_progress:events}],[],'2026-09-15T08:30:00-04:00');
const bar=html=>html.match(/<progress\b[^>]*>/)?.[0];
const completed=render('complete','complete');
assert.match(bar(completed),/max="1" value="1"/,'completed legacy work must be a static full bar');
assert.doesNotMatch(completed,/任务总量未记录/,'finished work should show completion, not an unresolved loading label');
for(const state of ['failed','partial_failure','cancelled','stopped']){
 assert.match(bar(render(state,'running')),/value="0"/,'a stopped job cannot animate a stale running child');
 assert.doesNotMatch(render(state,'running'),/分析中|进行中/);
}
assert.match(bar(render('running','complete')),/value="1"/,'one completed variant stops while its sibling runs');
assert.match(bar(render('running','failed')),/value="0"/);
assert.match(bar(render('queued','queued')),/value="0"/,'queued work has not started');
assert.doesNotMatch(bar(render('running','running')),/value=/,'only genuinely running work without a plan is indeterminate');
''')

    def test_terminal_status_overrides_incomplete_display_events_without_fake_counts(self):
        self.run_js(r'''
const assert=require('node:assert/strict');
const events=[{stage:'tasks_planned',variant_key:'v',tasks:[{task_id:'report:AAA:market',symbol:'AAA',domain:'market'},{task_id:'decision'}]},
 {stage:'model_started',variant_key:'v',symbol:'AAA',domain:'market',status:'running'}];
const render=status=>ui.progressHtml([{job_id:'j',started_at_et:'2026-09-15T08:00:00-04:00',status,
 variant_progress:{v:status==='complete'?'complete':'running'},research_progress:events}],[],'2026-09-15T08:30:00-04:00');
assert.match(render('complete'),/<progress[^>]*max="1" value="1"/);
assert.doesNotMatch(render('complete'),/2 \/ 2 项|进行中|等待/,'do not fabricate task events or leave active child labels');
assert.doesNotMatch(render('failed'),/进行中|等待/,'failed parent stops unfinished domain labels');
assert.match(render('failed'),/<progress[^>]*max="2" value="0"/);
''')

    def test_failed_version_has_short_actionable_reason_without_raw_tail(self):
        self.run_js(r'''
const assert=require('node:assert/strict');
const html=ui.progressHtml([{job_id:'j',started_at_et:'2026-09-15T08:00:00-04:00',status:'partial_failure',variant_progress:{good:'complete',bad:'failed'},
 variant_errors:{bad:{message:'模型额度已用完，请恢复额度后继续分析。'+ 'X'.repeat(1000)}}}],[],'2026-09-15T08:00:00-04:00');
assert.match(html,/模型额度已用完/);
assert.doesNotMatch(html,/X{121}/);
assert.match(html,/已完成/);
''')
    def test_variant_bar_uses_declared_tasks_and_never_exceeds_unique_completions(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const events=[{stage:'tasks_planned',variant_key:'v',tasks:[{task_id:'report:AAA:market',symbol:'AAA',domain:'market'},{task_id:'decision'}]},
 {stage:'report_validated',variant_key:'v',symbol:'AAA',domain:'market',status:'validated',report:{thesis:'saved conclusion'}},
 {stage:'report_validated',variant_key:'v',symbol:'AAA',domain:'market',status:'validated',report:{thesis:'saved conclusion'}}];
const html=ui.progressHtml([{job_id:'j',status:'running',variant_progress:{v:'running'},research_progress:events}],[],'2026-09-15T08:00:00-04:00');
assert.match(html,/<progress[^>]*max="2"[^>]*value="1"/);
assert.match(html,/saved conclusion/);
assert.doesNotMatch(html,/执行时间线|call_requested|report_validated/);
const old=ui.progressHtml([{job_id:'old',status:'running',variant_progress:{v:'running'},research_progress:events.slice(1)}],[],'2026-09-15T08:00:00-04:00');
assert.doesNotMatch(old,/<progress[^>]*value=/,'old records must not invent a denominator');
""")
    def test_calls_are_counted_per_version_without_report_or_stage_inflation(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const events=[
 {variant_key:'team/main',stage:'call_requested',call_id:'a',attempt:1},
 {variant_key:'team/main',stage:'model_started',call_id:'a',attempt:1},
 {variant_key:'team/main',stage:'model_returned',call_id:'a',attempt:1,status:'complete'},
 {variant_key:'team/main',stage:'report_validated',call_id:'a',symbol:'AAA',domain:'market',status:'validated'},
 {variant_key:'team/main',stage:'report_validated',call_id:'a',symbol:'BBB',domain:'market',status:'validated'},
 {variant_key:'team/main',stage:'decision_complete',status:'complete'},
 {variant_key:'team/main',stage:'call_requested',call_id:'b',attempt:1},
 {variant_key:'team/main',stage:'failure',call_id:'b',attempt:1,status:'failed'},
 {variant_key:'team/main',stage:'call_requested',call_id:'b',attempt:2},
 {variant_key:'team/other',stage:'call_requested',call_id:'a',attempt:1}
];
assert.equal(typeof ui.callSummary,'function','call counters must be independent of report events');
assert.deepEqual(ui.callSummary(events,'team/main'),{total:2,complete:1,running:1,failed:0,reused:0,attempts:3});
assert.deepEqual(ui.callSummary(events,'team/other'),{total:1,complete:0,running:1,failed:0,reused:0,attempts:1});
""")

    def test_completed_versions_stay_visible_and_old_failures_are_not_today(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const jobs=[{job_id:'old',batch_id:'old-batch',status:'partial_failure',started_at_et:'2026-09-01T08:00:00-04:00',variant_progress:{'team/main':'failed'}},
 {job_id:'today',status:'complete',started_at_et:'2026-09-15T08:00:00-04:00',variant_progress:{'team/main':'complete'}}];
const html=ui.progressHtml(jobs,[{author:'team',version_id:'main',method_name:'独立证据门禁版'}],'2026-09-15T09:00:00-04:00');
assert.doesNotMatch(html,/历史未完成|data-progress-job="old"/);
const today=html.slice(html.indexOf('data-progress-job="today"'));
assert.match(today,/独立证据门禁版/,'a completed batch must still name its versions');
assert.match(today,/<progress/);
""")

    def test_old_failed_batch_is_not_today_but_can_be_rendered_for_history(self):
        self.run_js(r"""
const assert=require('node:assert/strict');
const job={job_id:'old',batch_id:'LAB-old',status:'partial_failure',
 started_at_et:'2026-09-01T08:00:00-04:00',variant_progress:{'team/main':'failed'}};
const html=ui.progressHtml([job],[],'2026-09-15T08:00:00-04:00');
assert.doesNotMatch(html,/data-progress-retry="old"/);
const historical=ui.progressHtml([job],[],'2026-09-01T12:00:00-04:00');
assert.match(historical,/data-progress-retry="old"/);
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
        self.assertLess(html.index('data-progress-job="one"'), html.index('&lt;unsafe&gt;'))
        self.assertLess(html.index('&lt;unsafe&gt;'), html.index('data-progress-job="two"'))
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
assert.match(html,/实际调用 1/);
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
