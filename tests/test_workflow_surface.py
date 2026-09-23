import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSurfaceTests(unittest.TestCase):
    def node(self, script):
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8', cwd=ROOT, check=True)

    def test_data_retry_is_visible_and_stops_when_preparation_advances(self):
        self.node(r'''
const assert=require('assert/strict'),p=require('./src/shaq_daily_oracle/desktop/today_progress.js');
const now='2026-09-23T08:35:00-04:00', key='team/a';
const job={job_id:'j',status:'running',started_at_et:now,model_profile_id:'p',variant_progress:{[key]:'queued'},research_progress:[
 {stage:'data_preparation',component:'history',status:'running',completed:0,total:2,source:'yfinance'},
 {stage:'data_retry_scheduled',source:'yfinance',request_stage:'history',symbol:'PAYX',attempt:2,max_attempts:2,next_retry_at:'2026-09-23T12:35:02+00:00'}]};
assert.equal(p.primaryAction([job],[key],now,'p').label,'正在自动重试');
let html=p.progressHtml([job],[],now);assert.match(html,/正在自动重试/);assert.match(html,/PAYX/);assert.match(html,/2 \/ 2/);
job.research_progress.push({stage:'data_preparation',component:'history',status:'complete',completed:2,total:2,source:'yfinance'});
html=p.progressHtml([job],[],now);assert.doesNotMatch(html,/正在自动重试/);
assert.equal(p.primaryAction([job],[key],now,'p').action,'progress');
job.research_progress.pop();job.status='failed';html=p.progressHtml([job],[],now);assert.doesNotMatch(html,/正在自动重试/);
''')

    def test_single_action_tracks_selected_methods_and_real_terminal_state(self):
        self.node(r'''
const assert=require('assert/strict'),p=require('./src/shaq_daily_oracle/desktop/today_progress.js');
assert.equal(typeof p.primaryAction,'function');
const now='2026-09-23T08:40:00-04:00', selections=['team/a','team/b'];
const job={job_id:'j',batch_id:'batch',started_at_et:now,status:'running',model_profile_id:'p',variant_progress:{'team/a':'complete','team/b':'running'}};
assert.equal(p.primaryAction([],selections,now,'p').label,'开始今日分析');
assert.equal(p.primaryAction([job],selections,now,'p').action,'progress');
assert.equal(p.primaryAction([{...job,started_at_et:null,queued_at_et:now,status:'queued'}],selections,now,'p').action,'progress');
const failed={...job,status:'partial_failure',variant_progress:{'team/a':'complete','team/b':'failed'}};
assert.equal(p.primaryAction([failed],selections,now,'p').label,'继续未完成分析');
assert.equal(p.primaryAction([{...failed,next_retry_at:'2026-09-23T08:41:00-04:00'}],selections,now,'p').label,'正在自动重试');
assert.equal(p.primaryAction([{...job,status:'complete',variant_progress:{'team/a':'complete','team/b':'complete'}}],selections,now,'p').action,'results');
assert.equal(p.primaryAction([failed],['team/a'],now,'p').action,'results','successful sibling is not rerun');
assert.equal(p.primaryAction([failed],['team/new'],now,'p').action,'start');
assert.equal(p.primaryAction([{...job,status:'failed',started_at_et:'2026-09-22T08:40:00-04:00'}],selections,now,'p').action,'start');
''')

    def test_retry_and_summary_show_actual_events_not_elapsed_estimates(self):
        self.node(r'''
const assert=require('assert/strict'),p=require('./src/shaq_daily_oracle/desktop/today_progress.js');
const now='2026-09-23T08:40:00-04:00', key='team/a';
const job={job_id:'j',status:'running',started_at_et:now,model_profile_id:'p',variant_progress:{[key]:'running'},research_progress:[
 {variant_key:key,stage:'tasks_planned',tasks:[{task_id:'report:PAYX:price_volume',symbol:'PAYX',domain:'price_volume'},{task_id:'adversary'},{task_id:'decision'}]},
 {variant_key:key,stage:'model_retry_scheduled',call_id:'c',domain:'price_volume',symbols:['PAYX'],attempt:1,max_attempts:2,next_retry_at:now,occurred_at_et:now,status:'retrying'}]};
assert.equal(p.primaryAction([job],[key],now,'p').label,'正在自动重试');
let html=p.progressHtml([job],[],now);assert.match(html,/3 · 汇总结果/);assert.match(html,/保存结果/);assert.match(html,/自动重试/);assert.match(html,/PAYX/);
job.research_progress.push({variant_key:key,stage:'model_retry_started',call_id:'c',domain:'price_volume',symbols:['PAYX'],attempt:2,max_attempts:2,occurred_at_et:now,status:'running'});
html=p.progressHtml([job],[],'2026-09-23T08:40:10-04:00');assert.match(html,/已用时 10秒/);assert.match(html,/value="0"/);
job.status='failed';html=p.progressHtml([job],[],'2026-09-23T08:41:00-04:00');assert.doesNotMatch(html,/正在自动重试|正在分析/);
''')

    def test_preparation_progress_is_recorded_units_and_never_timer_percentage(self):
        self.node(r'''
const assert=require('assert/strict'),p=require('./src/shaq_daily_oracle/desktop/today_progress.js');
const job={job_id:'j',status:'running',started_at_et:'2026-09-23T08:35:00-04:00',variant_progress:{},research_progress:[
 {stage:'data_preparation',component:'universe',status:'complete',completed:503,total:503,source:'versioned'},
 {stage:'data_preparation',component:'history',status:'running',completed:200,total:503,source:'Yahoo'}]};
const first=p.progressHtml([job],[], '2026-09-23T08:36:00-04:00');
assert.match(first,/准备数据/);assert.match(first,/200 \/ 503/);assert.match(first,/Yahoo/);
const later=p.progressHtml([job],[], '2026-09-23T08:46:00-04:00');
assert.match(later,/value="200"/);
assert.doesNotMatch(later,/<progress(?![^>]*value=)[^>]*>/);
const finished=p.progressHtml([{...job,status:'failed'}],[], '2026-09-23T08:46:00-04:00');
assert.match(finished,/未完成/);assert.doesNotMatch(finished,/<progress(?![^>]*value=)[^>]*>/);
''')

    def test_clock_tick_does_not_call_backend_or_render_page(self):
        self.node(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const start=source.indexOf('function tickLocalClock(');assert.ok(start>=0);
const end=source.indexOf('\nasync function ',start);
const clock={textContent:''};let renders=0,requests=0;
const context={Date,Intl,q:()=>clock,render:()=>renders++,api:()=>requests++};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
context.tickLocalClock(new Date('2026-09-23T00:00:01Z'));
const first=clock.textContent;
context.tickLocalClock(new Date('2026-09-23T00:00:05Z'));
assert.notEqual(clock.textContent,first);assert.equal(renders,0);assert.equal(requests,0);
''')
