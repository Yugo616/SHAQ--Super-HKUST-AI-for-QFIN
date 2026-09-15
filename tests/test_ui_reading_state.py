"""Reading must survive asynchronous desktop updates, not just navigation."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReadingStateTests(unittest.TestCase):
    def run_js(self, body):
        prefix = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('src/shaq_daily_oracle/desktop/app.js','utf8');
const start=source.indexOf('async function load(showError');
const fn=source.slice(start,source.indexOf("qa('.nav')",start));
'''
        subprocess.run(['node', '-'], input=prefix+body, text=True,
                       encoding='utf-8', cwd=ROOT, check=True)

    def test_quiet_modal_refresh_keeps_late_scroll_and_new_candidate(self):
        self.run_js(r'''
const batchFn=source.slice(source.indexOf('async function loadBatch('),source.indexOf('function screeningReason('));
const modal={open:true},detail={scrollTop:20},button={},renders=[];let resolve;
const ctx={state:{replay:{batchId:'b',key:'v',symbol:'AAA'},replayGeneration:0},
 q:s=>s==='#replay-modal'?modal:s==='#replay-close'?button:detail,
 api:()=>new Promise(done=>resolve=done),notice(){},
 renderBatch:(batch,key,symbol)=>{renders.push(symbol);detail.scrollTop=0}};
vm.createContext(ctx);vm.runInContext(fn,ctx);vm.runInContext(batchFn,ctx);
(async()=>{
 const first=ctx.loadBatch('b','v','AAA',true);detail.scrollTop=280;resolve({id:'b'});await first;
 assert.equal(detail.scrollTop,280,'capture modal position immediately before replacing content');
 const second=ctx.loadBatch('b','v','AAA',true);ctx.state.replay={batchId:'b',key:'v',symbol:'BBB'};
 resolve({id:'b'});await second;assert.deepEqual(renders,['AAA'],'pending refresh must not undo a new candidate');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_pending_refresh_keeps_latest_scroll_not_request_time_scroll(self):
        self.run_js(r'''
let resolve;const restored=[],detail={scrollTop:19};
const ctx={state:{page:'run',replay:null},q:s=>s==='#replay-modal'?{open:false}:detail,
 window:{scrollY:7,scrollTo(x,y){restored.push(y)}},
 api:()=>new Promise(done=>resolve=done),render(){},notice(){}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{const pending=ctx.load(false);ctx.window.scrollY=200;resolve({});await pending;
 assert.equal(restored.at(-1),200,'must capture reading position after the response arrives');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_identical_refresh_does_not_replace_current_page(self):
        self.run_js(r'''
let renders=0;const data={settings:{},jobs:[],versions:[],dashboard:{},clock:{et:'2026-09-15T08:00:00-04:00'}};
const ctx={state:{page:'run',data,replay:null},q:()=>({open:false}),
 window:{scrollY:0,scrollTo(){}},api:async()=>({...data,clock:{...data.clock,et:'2026-09-15T08:00:10-04:00'}}),
 render(pageChanged){if(pageChanged!==false)renders++},notice(){}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{await ctx.load(false);assert.equal(renders,0,'clock ticks alone must not rebuild the page')})()
 .catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_changed_refresh_restores_open_details_and_unsaved_filter(self):
        self.run_js(r'''
const oldDetail={tagName:'DETAILS',id:'history-costs',open:true,scrollTop:0,scrollLeft:0};
const oldInput={tagName:'INPUT',id:'history-from',type:'date',value:'2026-09-01',scrollTop:0,scrollLeft:0};
let nodes=[oldDetail,oldInput],resolve;
const root={querySelectorAll:()=>nodes};
const ctx={state:{page:'history',data:{dashboard:{value:1}},replay:null},
 q:s=>s==='#history'?root:s==='#replay-modal'?{open:false}:null,
 window:{scrollY:7,scrollTo(){}},api:()=>new Promise(done=>resolve=done),
 render(){nodes=[{...oldDetail,open:false},{...oldInput,value:''}]},notice(){}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{const pending=ctx.load(false);oldInput.value='2026-09-03';resolve({dashboard:{value:2}});await pending;
 assert.equal(nodes[0].open,true,'refresh must not close an open account disclosure');
 assert.equal(nodes[1].value,'2026-09-03','keep input edited while response was pending');
})().catch(e=>{console.error(e);process.exitCode=1});
''')
