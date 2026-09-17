import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from shaq_daily_oracle.lab_service import LabService
from shaq_daily_oracle.research_progress import ResearchProgressLog


class LiveActivityTests(unittest.TestCase):
    def service(self, root):
        lab = LabService.__new__(LabService)
        lab.paths = SimpleNamespace(research_root=root)
        lab.jobs = {}
        lab.jobs_lock = threading.Lock()
        lab.dashboard = Mock()
        lab.settings = Mock()
        lab.start_result_refresh = Mock(side_effect=AssertionError('poll must be read only'))
        return lab

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
const start=source.indexOf('async function pollDesktopActivity(');
assert.ok(start>=0,'missing unconditional lightweight poll');
const code=source.slice(start,source.indexOf('async function startDesktop(',start));
const calls=[], ctx={state:{data:{jobs:[],clock:{}},page:'run'},
 api:async name=>{calls.push(name);return {changed:true,revision:'new',jobs:[{job_id:'auto',status:'running'}],result_refresh:{status:'idle'},observed_at:'2026-09-17T08:35:10-04:00'}},
 q:()=>null,preserveReadingView:()=>()=>{},render(){},load:async()=>{},window:{scrollY:87,scrollTo(x,y){assert.equal(y,87)}}};
vm.createContext(ctx);vm.runInContext(code,ctx);
(async()=>{await ctx.pollDesktopActivity();assert.deepEqual(calls,['get_lab_activity']);
 assert.equal(ctx.state.data.jobs[0].job_id,'auto');assert.equal(ctx.state.activityRevision,'new');
 assert.match(source,/setInterval\([^;]*pollDesktopActivity/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=Path(__file__).resolve().parents[1], check=True)
