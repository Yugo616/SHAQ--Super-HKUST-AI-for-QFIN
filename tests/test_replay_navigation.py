import subprocess
import unittest
from pathlib import Path


class ReplayNavigationTests(unittest.TestCase):
    def test_modal_load_failure_race_close_and_candidate_restore(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const fn=source.slice(source.indexOf('async function loadBatch('),source.indexOf('function screeningReason('));
const requests=[];
const back={};
const target={innerHTML:'',isConnected:true,scrollTop:10};
const modal={open:false,showModal(){this.open=true},close(){this.open=false}};
const ctx={state:{replay:null},q:s=>s==='#batch-detail'?target:s==='#replay-close'?back:modal,
 api:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})),
 renderBatch:(batch,key,symbol)=>{target.innerHTML=batch.id+':'+key+':'+(symbol||'first')},
 esc:s=>String(s).replaceAll('<','&lt;'),notice:()=>{}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{
 const loading=ctx.loadBatch('batch','version','BBB');
 assert.ok(modal.open);assert.ok(target.innerHTML.includes('加载'));
 requests.shift().resolve({id:'batch'});await loading;
 assert.equal(target.innerHTML,'batch:version:BBB');
 assert.equal(typeof back.onclick,'function');back.onclick();assert.equal(modal.open,false);
 const failed=ctx.loadBatch('broken','version');requests.shift().reject(new Error('<missing>'));await failed;
 assert.ok(target.innerHTML.includes('&lt;missing>'));
 const stale=ctx.loadBatch('stale','v','AAA'), fresh=ctx.loadBatch('fresh','v','BBB');
 requests[1].resolve({id:'fresh'});await fresh;
 requests[0].resolve({id:'stale'});await stale;
 assert.equal(target.innerHTML,'fresh:v:BBB');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=root, check=True)

    def test_deferred_workspace_refresh_never_restores_over_new_user_navigation(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const start=source.indexOf('async function load(showError');
const fn=source.slice(start,source.indexOf("qa('.nav')",start));
const modal={open:true},detail={scrollTop:19};let resolve;const restored=[];
const ctx={state:{replay:{batchId:'old',key:'v',symbol:'AAA'}},
 q:s=>s==='#replay-modal'?modal:detail,window:{scrollY:7,scrollTo(){}},
 api:()=>new Promise(done=>{resolve=done}),render:()=>{},notice:()=>{},
 loadBatch:(...args)=>{restored.push(args);return Promise.resolve()}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{
 const candidatePending=ctx.load(false);
 ctx.state.replay={batchId:'old',key:'v',symbol:'BBB'};
 resolve({});await candidatePending;
 assert.deepEqual(restored,[],'candidate changed while refresh was pending');
 const modalPending=ctx.load(false);
 modal.open=false;ctx.state.replay=null;
 modal.open=true;ctx.state.replay={batchId:'new',key:'v2',symbol:'CCC'};
 resolve({});await modalPending;
 assert.deepEqual(restored,[],'closed and reopened modal is a new generation');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=root, check=True)

    def test_escape_then_pending_reopen_blocks_old_workspace_restore(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const batchFn=source.slice(source.indexOf('async function loadBatch('),source.indexOf('function screeningReason('));
const loadStart=source.indexOf('async function load(showError');
const loadFn=source.slice(loadStart,source.indexOf("qa('.nav')",loadStart));
const requests=[],renders=[],back={};
const modal={open:true,showModal(){this.open=true},close(){this.open=false;this.onclose?.()}};
const detail={innerHTML:'',scrollTop:0};
const ctx={state:{replay:null,replayGeneration:0},
 q:s=>s==='#replay-modal'?modal:s==='#batch-detail'?detail:s==='#replay-close'?back:detail,
 window:{scrollY:0,scrollTo(){}},render(){},notice(){},esc:String,
 renderBatch:(batch,key,symbol)=>{renders.push([batch.id,key,symbol]);ctx.state.replay={batchId:batch.id,key,symbol}},
 api:name=>new Promise((resolve,reject)=>requests.push({name,resolve,reject}))};
vm.createContext(ctx);vm.runInContext(batchFn,ctx);vm.runInContext(loadFn,ctx);
(async()=>{
 const initial=ctx.loadBatch('old','v','AAA');
 requests.find(r=>r.name==='get_shadow_batch').resolve({id:'old'});await initial;
 assert.equal(typeof modal.oncancel,'function');
 const workspace=ctx.load(false);
 modal.open=false;modal.oncancel?.({preventDefault(){}});
 const pendingNew=ctx.loadBatch('new','v2','BBB');
 requests.find(r=>r.name==='get_lab_state').resolve({});await Promise.resolve();await Promise.resolve();
 assert.equal(requests.filter(r=>r.name==='get_shadow_batch').length,2,'old workspace replay was restored');
 requests.filter(r=>r.name==='get_shadow_batch')[1].resolve({id:'new'});await pendingNew;
 await workspace;
 assert.deepEqual(renders,[['old','v','AAA'],['new','v2','BBB']]);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=root, check=True)

    def test_assembled_renderer_wrapper_chain_forwards_nondefault_candidate(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const desktop='src/shaq_daily_oracle/desktop';
const html=fs.readFileSync(path.join(desktop,'index.html'),'utf8');
const scripts=[...html.matchAll(/<script src="([^"]+)"/g)].map(row=>row[1]);
assert.deepEqual(scripts,['app.js','today_progress.js','workbench.js','accounts.js','review.js']);
const cls={add(){},remove(){},toggle(){}};
const element=()=>new Proxy({classList:cls,dataset:{},parentElement:{prepend(){}},children:[],
 append(){},prepend(){},before(){},insertBefore(){},insertAdjacentHTML(){},insertAdjacentElement(){},
 addEventListener(){},remove(){},closest(){return null},scrollIntoView(){},focus(){}},
 {get:(o,k)=>k in o?o[k]:'',set:(o,k,v)=>(o[k]=v,true)});
const ctx={console,window:{addEventListener(){},scrollY:0,scrollTo(){}},
 document:{createElement:element,querySelector:element,querySelectorAll:()=>[]},
 q:element,qa:()=>[],esc:String,dir:String,money:String,api:async()=>({}),notice(){},
 setInterval(){},setTimeout(){},Intl,Date};
vm.createContext(ctx);
for(const file of scripts)vm.runInContext(fs.readFileSync(path.join(desktop,file),'utf8'),ctx,{filename:file});
const batch={batch_id:'fixture',evidence:{cutoff_status:'on_time',candidates:[{symbol:'AAA'},{symbol:'BBB'}],catalog:[]},labels:{labels:{}},replay_summaries:{},virtual_accounts:{results:[]},variants:{v:{variant:{label:'Fixture'},candidate_intake:{candidates:[{symbol:'AAA'},{symbol:'BBB'}]},reports_by_symbol:{},adversary_by_symbol:{},integration_audit:{},predictions:[]}}};
ctx.renderBatch(batch,'v','BBB');
assert.equal(vm.runInContext('state.replay.symbol',ctx),'BBB');
'''
        subprocess.run(['node', '-'], input=script, text=True, encoding='utf-8',
                       cwd=root, check=True)
