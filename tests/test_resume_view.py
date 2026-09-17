import json
from pathlib import Path
import subprocess
import unittest


class ResumeViewTests(unittest.TestCase):
    def test_resume_renders_acknowledged_progress_before_slow_dashboard_refresh(self):
        source = (Path(__file__).resolve().parents[1]/'src/shaq_daily_oracle/desktop/workbench.js').read_text()
        self.assertIn('async function resumeOriginalBatch(', source)
        function = source[source.index('async function resumeOriginalBatch('):source.index('\nrenderRun=')]
        script = r'''
const assert=require('node:assert/strict');
const calls=[],state={data:{jobs:[{job_id:'original',batch_id:'b',status:'failed'}]}};
const notice=x=>calls.push('notice'),showPage=x=>calls.push('page:'+x);
const renderRun=()=>{assert.equal(state.data.jobs.at(-1).job_id,'resume-b');calls.push('render')};
const api=async()=>({job_id:'resume-b',batch_id:'b',status:'queued',variant_progress:{v:'queued'}});
const load=()=>{calls.push('load');return new Promise(()=>{})};
''' + function + r'''
(async()=>{await resumeOriginalBatch('b');assert(calls.indexOf('render')<calls.indexOf('load'));
assert(calls.includes('page:run'));assert.equal(state.data.jobs.length,2)})().catch(e=>{console.error(e);process.exit(1)});
'''
        subprocess.run(['node','-'], input=script, text=True, check=True)
