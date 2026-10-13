import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shaq_daily_oracle.lab_service import LabService
from shaq_daily_oracle.research_progress import ResearchProgressLog


class LiveActivityTests(unittest.TestCase):
    def test_reading_state_keeps_manual_and_scheduled_selection_separate(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const fn=source.slice(source.indexOf('function preserveReadingView('),source.indexOf('function applyDesktopActivity('));
const root={scrollTop:0,scrollLeft:0};
const scheduled={id:'automatic-settings',tagName:'DIV',parentElement:root};
const field=(parent,checked)=>({tagName:'INPUT',type:'checkbox',value:'on',checked,
  dataset:{author:'team',version:'main'},parentElement:parent,scrollTop:0,scrollLeft:0});
const manual=field(root,true),automatic=field(scheduled,false);
root.querySelectorAll=()=>[manual,automatic];
const ctx={document:{activeElement:null}};vm.createContext(ctx);vm.runInContext(fn,ctx);
const restore=ctx.preserveReadingView(root);
manual.checked=false;automatic.checked=true;restore();
assert.equal(manual.checked,true,'scheduled selection overwrote the manual control');
assert.equal(automatic.checked,false,'manual selection overwrote the schedule');
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=Path(__file__).resolve().parents[1], check=True)

    def service(self, root):
        lab = LabService.__new__(LabService)
        lab.paths = SimpleNamespace(research_root=root, runtime_root=root/'runtime',
                                    config_root=root/'config', data_root=root)
        lab.jobs = {}
        lab.jobs_lock = threading.Lock()
        lab._activity_lock = threading.Lock()
        lab.dashboard = Mock()
        lab.settings = Mock()
        lab.start_result_refresh = Mock(side_effect=AssertionError('poll must be read only'))
        return lab

    def test_unchanged_activity_reuses_reports_but_detects_liveness_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = self.service(Path(tmp))
            lab.jobs = {'job-live': {'job_id': 'job-live', 'status': 'running'}}
            with patch.object(lab, 'job_statuses', wraps=lab.job_statuses) as read_jobs:
                first = lab.activity_status()
                self.assertFalse(lab.activity_status(first['revision'])['changed'])
                self.assertEqual(read_jobs.call_count, 1, 'idle observation reloaded historical progress')
                with patch('shaq_daily_oracle.job_corrections.observed_job_status',
                           side_effect=lambda root, row: {**row, 'status': 'incomplete', 'live_state': 'interrupted'}):
                    next_value = lab.activity_status(first['revision'])
                self.assertTrue(next_value['changed'])
                self.assertEqual(next_value['jobs'][0]['status'], 'incomplete')

    def test_formal_operator_changes_arrive_without_history_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = self.service(Path(tmp))
            lab.paths.runtime_root.mkdir()
            status = lab.paths.runtime_root/'service_status.json'
            status.write_text(json.dumps({'state':'waiting'}))
            first = lab.activity_status()
            status.write_text(json.dumps({'state':'workflow_complete'}))
            next_value = lab.activity_status(first['revision'])
            self.assertTrue(next_value['changed'])
            self.assertEqual(next_value['formal_operator']['service']['state'], 'workflow_complete')
            self.assertEqual(lab.dashboard.mock_calls, [])

    def test_external_job_discovered_without_index_refresh_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = self.service(root)
            first = lab.activity_status()
            self.assertFalse(lab.activity_status(first['revision'])['changed'])
            jobs = root / 'jobs'
            jobs.mkdir()
            job = dict(job_id='job-external', status='running',
                       started_at_et='2026-09-17T08:35:00-04:00', variant_progress={'team/main': 'running'})
            (jobs / 'job-external.json').write_text(json.dumps(job))
            second = lab.activity_status(first['revision'])
            self.assertTrue(second['changed'])
            self.assertEqual(second['jobs'][0]['job_id'], 'job-external')
            ResearchProgressLog(jobs / 'job-external-research.jsonl').append(
                stage='tasks_planned', variant_key='team/main', tasks=[{'task_id': 'decision'}])
            third = lab.activity_status(second['revision'])
            self.assertTrue(third['changed'])
            self.assertEqual(third['jobs'][0]['progress_summary']['total_tasks'], 1)
            self.assertEqual(lab.dashboard.mock_calls, [])
            self.assertEqual(lab.settings.mock_calls, [])

    def test_progress_counts_unique_validated_tasks_not_model_returns(self):
        from shaq_daily_oracle.research_progress import summarize_job
        event = dict(stage='report_validated', variant_key='a', symbol='AAA', domain='market', status='validated')
        events = [dict(stage='tasks_planned', variant_key=k,
                       tasks=[{'task_id': 'report:AAA:market'}, {'task_id': 'decision'}]) for k in ('a', 'b')]
        events += [event, event, dict(stage='model_returned', variant_key='b', call_id='x', status='complete')]
        job = dict(job_id='j', status='running', variant_progress={'a':'running', 'b':'running'}, research_progress=events)
        summary = summarize_job(job)
        self.assertEqual(summary['total_tasks'], 4)
        self.assertEqual(summary['completed_tasks'], 1)
        self.assertEqual(summary['variants']['a']['completed_tasks'], 1)
        self.assertEqual(summary['variants']['b']['completed_tasks'], 0)
        self.assertEqual(summary['completed_calls'], 1)
        self.assertIsNone(summary['total_calls'])
        self.assertIsNone(summarize_job(dict(job, research_progress=[]))['total_tasks'])
        terminal = summarize_job(dict(job, status='complete'))
        self.assertEqual(terminal['stage'], 'complete')
        self.assertEqual(terminal['completed_tasks'], 1, 'missing events must not be fabricated')

    def test_refresh_failure_keeps_its_scope_separate_from_completed_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);lab=self.service(root)
            lab.jobs={'job-finished':dict(job_id='job-finished',status='complete',variant_progress={'v':'complete'})}
            (root/'label_refresh_status.json').write_text(json.dumps(dict(status='failed',error='database is locked')))
            value=lab.activity_status()
            self.assertEqual(value['jobs'][0]['status'],'complete')
            self.assertEqual(value['result_refresh']['scope'],'result_refresh')
            self.assertEqual(value['result_refresh']['impact'],'price_and_account_refresh_only')

    def test_desktop_poll_discovers_external_run_even_when_local_jobs_empty(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const start=source.indexOf('function applyDesktopActivity(');
assert.ok(start>=0,'missing native notification consumer');
const code=source.slice(start,source.indexOf('async function startDesktop(',start));
const calls=[], ctx={state:{data:{jobs:[],clock:{}},page:'run'},
 api:async name=>{calls.push(name);return {changed:true,revision:'new',jobs:[{job_id:'auto',status:'running'}],result_refresh:{status:'idle'},observed_at:'2026-09-17T08:35:10-04:00'}},
 q:()=>null,preserveReadingView:()=>()=>{},render(){},load:async()=>{},window:{scrollY:87,scrollTo(x,y){assert.equal(y,87)}}};
vm.createContext(ctx);vm.runInContext(code,ctx);
(async()=>{await ctx.pollDesktopActivity();assert.deepEqual(calls,['get_lab_activity']);
 assert.equal(ctx.state.data.jobs[0].job_id,'auto');assert.equal(ctx.state.activityRevision,'new');
 assert.doesNotMatch(source,/setInterval\([^;]*pollDesktopActivity/);
 const before=calls.length;
 ctx.applyDesktopActivity({changed:true,revision:'pushed',jobs:[{job_id:'native',status:'running'}],result_refresh:{status:'idle'}});
 assert.equal(calls.length,before,'pushed progress must not fetch history');
 assert.equal(ctx.state.data.jobs[0].job_id,'native');
 let finish;ctx.api=()=>new Promise(resolve=>finish=resolve);
 const reconnect=ctx.pollDesktopActivity();
 ctx.applyDesktopActivity({changed:true,revision:'newest',jobs:[{job_id:'latest',status:'running'}],result_refresh:{status:'idle'}});
 finish({changed:true,revision:'stale-reconnect',jobs:[{job_id:'old',status:'running'}],result_refresh:{status:'idle'}});
 await reconnect;
 assert.equal(ctx.state.activityRevision,'newest','late reconnect response overwrote a newer notification');
 assert.equal(ctx.state.data.jobs[0].job_id,'latest');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=Path(__file__).resolve().parents[1], check=True)
