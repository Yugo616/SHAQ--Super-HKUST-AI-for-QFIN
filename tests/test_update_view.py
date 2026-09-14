import json
from pathlib import Path
import subprocess
import unittest


class UpdateViewTests(unittest.TestCase):
    def test_startup_ack_only_after_real_load_and_render_succeed(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/app.js').read_text()
        functions=source[source.index('async function load('):source.index("qa('.nav').forEach")]
        script='''
const state={data:null};const calls=[];const window={scrollY:0,scrollTo(){}};
const q=()=>({open:false,scrollTop:0});const notice=()=>{};let broken=true;
const api=async name=>{calls.push(name);return {healthy:true}};
const render=()=>{if(broken)throw Error('render failed')};
'''+functions+'''
(async()=>{await startDesktop();const failed=[...calls];calls.length=0;broken=false;
await startDesktop();console.log(JSON.stringify({failed,success:calls}));})();
'''
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['failed'],['get_lab_state'])
        self.assertEqual(value['success'],['get_lab_state','confirm_desktop_ready'])

    def test_manual_handler_renders_exact_busy_queue_message(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/software_updates.js').read_text()
        script='''
const nodes=new Map();const q=s=>{if(!nodes.has(s))nodes.set(s,{dataset:{},innerHTML:'',open:false,scrollTop:0,contains(){return false},querySelector:q,addEventListener(){}});return nodes.get(s)};
const document={querySelector:q};const esc=x=>String(x||'');const setTimeout=()=>0;const clearTimeout=()=>{};
const api=async()=>({status:'ready',mode:'managed',waiting_for_idle:true,queued_apply_method:'manual',message:'已下载，等待本地任务运行完更新'});
'''+source+'''
(async()=>{renderSoftwareUpdate({status:'ready',mode:'managed'});await q('#apply-software').onclick();
console.log(JSON.stringify({html:q('#software-update-detail').innerHTML}));})();
'''
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertIn('已下载，等待本地任务运行完更新',value['html'])
        self.assertIn('取消本次等待',value['html'])

    def test_network_failure_keeps_local_disable_toggle_and_history(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/software_updates.js').read_text()
        script='''
const nodes=new Map();const calls=[];
const q=s=>{if(!nodes.has(s))nodes.set(s,{dataset:{},innerHTML:'',open:false,scrollTop:0,contains(){return false},querySelector:q,addEventListener(){},showModal(){this.open=true},checked:false});return nodes.get(s)};
const document={querySelector:q};const esc=x=>String(x||'');const setTimeout=()=>0;const clearTimeout=()=>{};
const api=async(method,value)=>{calls.push(method);if(method==='check_software_update')throw Error('offline');
return {status:'check_failed',automatic_enabled:method==='software_update_status',last_update:{version:'0.6.2',method:'manual',completed_at:'2026-09-14T01:00:00Z'}};};
'''+source+'''
(async()=>{await checkSoftwareUpdate();q('#automatic-software-update').checked=false;
await q('#automatic-software-update').onchange();console.log(JSON.stringify({calls,html:q('#software-update-detail').innerHTML}));})();
'''
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertIn('set_automatic_software_update',value['calls'])
        self.assertIn('0.6.2',value['html'])

    def test_real_handlers_download_poll_and_apply_without_browser(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/software_updates.js').read_text()
        script='''
const nodes=new Map();const calls=[];
const q=s=>{if(!nodes.has(s))nodes.set(s,{dataset:{},innerHTML:'',open:false,scrollTop:0,contains(){return false},querySelector:q,addEventListener(){},showModal(){this.open=true}});return nodes.get(s)};
const document={querySelector:q};const esc=x=>String(x||'');const notice=()=>{};
const setTimeout=()=>0;const clearTimeout=()=>{};
async function api(method){calls.push(method);return {mode:'managed',status:method==='check_software_update'?'available':method==='download_software_update'?'downloading':'ready',current_version:'0.6.2',latest_version:'0.7.0',size_bytes:1024,download_size_bytes:100,progress:100,notes:'Fix'};}
'''+source+'''
(async()=>{await checkSoftwareUpdate();await q('#download-software').onclick();
await pollSoftwareUpdate();await q('#apply-software').onclick();
console.log(JSON.stringify({calls,html:q('#software-update-detail').innerHTML}));})();
'''
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['calls'],['check_software_update','download_software_update','software_update_status','apply_software_update'])
        self.assertNotIn('open_software_release',value['calls'])

    def test_toggle_uses_independent_automatic_software_update_api_and_shows_history(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/software_updates.js').read_text()
        script='''
const nodes=new Map();const calls=[];
const q=s=>{if(!nodes.has(s))nodes.set(s,{dataset:{},innerHTML:'',open:false,scrollTop:0,contains(){return false},querySelector:q,addEventListener(){},showModal(){this.open=true},checked:false});return nodes.get(s)};
const document={querySelector:q};const esc=x=>String(x||'');const setTimeout=()=>0;const clearTimeout=()=>{};
const api=async(method,value)=>{calls.push([method,value]);return {status:'ready',mode:'managed',automatic_enabled:value,last_update:{version:'0.6.2',method:'automatic',completed_at:'2026-09-14T01:00:00Z'}};};
'''+source+'''
(async()=>{renderSoftwareUpdate({status:'ready',mode:'managed',waiting_for_idle:true,last_update:{version:'0.6.2',method:'automatic',completed_at:'2026-09-14T01:00:00Z'}});
const html=q('#software-update-detail').innerHTML;q('#automatic-software-update').checked=true;
await q('#automatic-software-update').onchange();console.log(JSON.stringify({html,calls}));})();
'''
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['calls'],[['set_automatic_software_update',True]])
        self.assertIn('已下载，等待本地任务运行完更新',value['html'])
        self.assertIn('2026-09-14T01:00:00Z',value['html'])
