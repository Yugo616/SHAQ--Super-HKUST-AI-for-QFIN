"""The live workbench must not reuse a different model or invent progress."""

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "src/shaq_daily_oracle/desktop/today_progress.js"
WORKBENCH = ROOT / "src/shaq_daily_oracle/desktop/workbench.js"


class ProgressActionIdentityTests(unittest.TestCase):
    def run_js(self, body):
        subprocess.check_output(
            ["node", "-"],
            input=f"const ui=require({json.dumps(str(PROGRESS))});\n" + body,
            text=True,
        )

    def test_primary_action_matches_selected_model_and_fails_closed_for_legacy_job(self):
        self.run_js("""
const assert=require('node:assert/strict');
const job={job_id:'other-model',status:'complete',started_at_et:'2026-09-23T08:35:00-04:00',
  model_profile_id:'model-a',variant_progress:{'team/main':'complete'}};
const now='2026-09-23T09:00:00-04:00';
assert.equal(ui.primaryAction([job],['team/main'],now,'model-b').action,'start');
assert.equal(ui.primaryAction([job],['team/main'],now,'model-a').action,'results');
assert.equal(ui.primaryAction([{...job,model_profile_id:undefined}],['team/main'],now,'model-a').action,'start');
""")

    def test_unknown_task_total_waits_for_plan_without_fake_progress_bar(self):
        self.run_js("""
const assert=require('node:assert/strict');
const now='2026-09-23T08:35:00-04:00';
const base={job_id:'j',status:'queued',queued_at_et:now,variant_progress:{'team/main':'queued'}};
const html=ui.progressHtml([base],[],now);
assert.match(html,/等待任务清单/);
assert.doesNotMatch(html,/<progress[^>]*max="1" value="0"/);
const done=ui.progressHtml([{...base,status:'complete',started_at_et:now,
  variant_progress:{'team/main':'complete'}}],[],now);
assert.match(done,/已完成/);
""")

    def test_queued_job_without_start_uses_its_queued_day_not_permanent_active_state(self):
        self.run_js("""
const assert=require('node:assert/strict');
const now='2026-09-23T08:35:00-04:00';
const queued={job_id:'new',status:'queued',started_at_et:null,
  queued_at_et:now,model_profile_id:'p',variant_progress:{'team/main':'queued'}};
assert.deepEqual(ui.currentJobs([queued],now).map(j=>j.job_id),['new']);
assert.equal(ui.primaryAction([queued],['team/main'],now,'p').action,'progress');
const old={...queued,job_id:'old',queued_at_et:'2026-09-22T08:35:00-04:00'};
assert.deepEqual(ui.currentJobs([old],now),[]);
assert.equal(ui.primaryAction([old],['team/main'],now,'p').action,'start');
assert.deepEqual(ui.currentJobs([{...old,queued_at_et:null}],now),[]);
""")

    def test_refreshed_retry_button_selects_failed_versions_instead_of_running_current_selection(self):
        source = WORKBENCH.read_text(encoding="utf-8")
        subprocess.check_output(["node", "-"], input=f"""
const assert=require('node:assert/strict');
const source={json.dumps(source)};
const binding=source.slice(source.indexOf('function bindProgressRetries(){{'),source.indexOf('function updatePrimaryAction(){{'));
const refresh=source.slice(source.indexOf('function refreshRunProgress(){{'),source.indexOf('async function renderAutomatic'));
assert.ok(binding.startsWith('function bindProgressRetries'));
assert.ok(refresh.startsWith('function refreshRunProgress'));
const retry={{dataset:{{progressRetry:'j'}},disabled:false}};
const main={{dataset:{{author:'team',version:'main'}},checked:true}};
const failed={{dataset:{{author:'team',version:'failed'}},checked:false}};
const target={{innerHTML:''}};
let starts=0,estimates=0,updates=0;
const state={{page:'run',runSelections:new Set(['team/main']),data:{{jobs:[{{job_id:'j',status:'failed',variant_progress:{{'team/main':'complete','team/failed':'failed'}}}}],versions:[],clock:{{et:'2026-09-23T08:35:00-04:00'}}}}}};
const q=key=>key==='#today-progress'?target:{{focus(){{}}}};
const qa=key=>key==='[data-progress-retry]'?[retry]:key==='.version-check'?[main,failed]:[];
const SHAQProgress={{progressHtml:()=>'<article>new</article>',retryVersions:()=>[{{author:'team',version_id:'failed'}}],failureText:e=>e.message}};
const preserveReadingView=()=>()=>{{}};
const versionKey=v=>v.author+'/'+v.version_id;
const estimate=()=>{{estimates++}},updatePrimaryAction=()=>{{updates++}},notice=()=>{{}},startBatch=()=>{{starts++}};
const resumeOriginalBatch=()=>{{throw Error('unexpected resume')}};
const showPage=()=>{{}},loadBatch=()=>{{}};
new Function('state','q','qa','SHAQProgress','preserveReadingView','versionKey','estimate','updatePrimaryAction','notice','startBatch','resumeOriginalBatch','showPage','loadBatch',
  binding+refresh+';return refreshRunProgress;')(state,q,qa,SHAQProgress,preserveReadingView,versionKey,estimate,updatePrimaryAction,notice,startBatch,resumeOriginalBatch,showPage,loadBatch)();
assert.equal(typeof retry.onclick,'function');
retry.onclick();
assert.deepEqual([...state.runSelections],['team/failed']);
assert.equal(main.checked,false);assert.equal(failed.checked,true);
assert.equal(starts,0);assert.equal(estimates,1);assert.ok(updates>=1);
""", text=True)


if __name__ == "__main__":
    unittest.main()
