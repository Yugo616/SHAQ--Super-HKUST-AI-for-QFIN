import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / 'src/shaq_daily_oracle/desktop/method_transfer.js'


class MethodTransferUiTests(unittest.TestCase):
    def test_api_connection_is_a_named_equal_peer(self):
        from html.parser import HTMLParser
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__(); self.buttons={}; self.active=None
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if tag=='button' and 'connection-choice' in attrs.get('class',''):
                    self.active=attrs['id'];self.buttons[self.active]={'class':attrs['class'],'text':''}
            def handle_data(self, data):
                if self.active:self.buttons[self.active]['text']+=data
            def handle_endtag(self,tag):
                if tag=='button':self.active=None
        parser=Buttons();parser.feed((ROOT/'src/shaq_daily_oracle/desktop/index.html').read_text())
        self.assertEqual(parser.buttons['show-api-form'], {'class':'connection-choice','text':'连接 API官方 API 或兼容中转站'})

    def test_native_transfer_fixture_is_read_only_and_never_uses_user_github(self):
        from shaq_daily_oracle.desktop import DesktopBridge, _bind_gui_smoke_fixture
        bridge=object.__new__(DesktopBridge)
        _bind_gui_smoke_fixture(bridge, {}, {})
        for direction in ('download','upload'):
            value=bridge.open_method_transfer(direction)
            self.assertTrue(value['ok'], value)
            self.assertEqual(len(value['value']['rows']),2)
        self.assertFalse(bridge.transfer_methods('fixture',['new'])['ok'])
        completed=bridge.transfer_methods('fixture-download',['new'])
        self.assertTrue(completed['ok'],completed)
        self.assertEqual(completed['value'][0]['status'],'complete')
        self.assertFalse(bridge.open_method_transfer('download')['value']['rows'][1]['eligible'])
        self.assertTrue(bridge.open_method_transfer('upload')['value']['rows'][1]['eligible'])

    def node(self, body):
        self.assertTrue(SOURCE.is_file(), 'two-dialog transfer implementation missing')
        return json.loads(subprocess.check_output(['node', '-'], text=True,
            input=SOURCE.read_text()+'\n'+body))

    def test_open_select_all_partial_failure_retry_and_no_implicit_write(self):
        value = self.node('''
(async()=>{
const calls=[];let fail=true;
const c=SHAQTransfer.controller('download',async(name,...args)=>{
 calls.push([name,...args]);
 if(name==='open_method_transfer')return {operation_id:'pinned',rows:[{key:'a',eligible:true},{key:'b',eligible:true},{key:'c',eligible:false}],destination:'team/repo',note:'note'};
 return [{key:args[1][0],status:args[1][0]==='b'&&fail?'failed':'complete',message:'item'}];
},()=>{});
await c.open();const before=calls.slice();c.selectAll();c.select('c',true);
await c.submit();const first=[...c.results.values()];fail=false;await c.submit();
console.log(JSON.stringify({before,calls,first,selected:[...c.selected],final:[...c.results.values()]}));
})();''')
        self.assertEqual(value['before'], [['open_method_transfer','download']])
        writes = [c for c in value['calls'] if c[0]=='transfer_methods']
        self.assertEqual([c[2] for c in writes], [['a'],['b'],['b']])
        self.assertEqual([r['status'] for r in value['first']], ['complete','failed'])
        self.assertEqual(value['selected'], [])

    def test_read_error_retry_refresh_uses_current_state_and_preserves_other_selection(self):
        value = self.node('''
(async()=>{
let fail=true,count=0;const events=[];
const call=async(name,direction)=>{count++;if(fail)throw new Error('401 login');return {operation_id:direction+count,rows:[{key:'a',eligible:true},{key:'b',eligible:count<4}]}};
const download=SHAQTransfer.controller('download',call,v=>events.push(v.status));
await download.open();const error=download.error;fail=false;await download.open();download.select('a',true);
const upload=SHAQTransfer.controller('upload',call,()=>{});await upload.open();upload.select('b',true);
await download.open();console.log(JSON.stringify({error,events,download:[...download.selected],upload:[...upload.selected],rows:download.rows}));
})();''')
        self.assertIn('401', value['error'])
        self.assertIn('failed', value['events'])
        self.assertEqual(value['download'], ['a'])
        self.assertEqual(value['upload'], ['b'])
        self.assertFalse(value['rows'][1]['eligible'])

    def test_dialog_handlers_disable_close_retry_and_show_accurate_time(self):
        value = self.node('''
(async()=>{
const nodes={},boxes=[];let reads=0;
const make=()=>({innerHTML:'',textContent:'',disabled:false,open:false,dataset:{},showModal(){this.open=true},close(){this.open=false}});
globalThis.q=s=>nodes[s]||=(make());globalThis.qa=()=>boxes;
globalThis.esc=x=>String(x??'').replaceAll('<','&lt;');
globalThis.api=async()=>{reads++;return {operation_id:'op',rows:[{key:'owned',eligible:false,author:'alice',version_id:'old',created_at:'2026-01-01',local_status:'本地已有相同内容'},{key:'new',eligible:true,author:'bob',version_id:'new',content_sha256:'f'.repeat(64)}]}};
globalThis.state={data:{},page:'editor'};
await openMethodTransfer('download');const opened=q('#method-transfer-modal').open;
const html=q('#method-transfer-detail').innerHTML;
q('#transfer-select-all').onclick();const enabled=!q('#transfer-confirm').disabled;
q('#method-transfer-close').onclick();const closed=!q('#method-transfer-modal').open;
await openMethodTransfer('upload');q('#method-transfer-modal').oncancel();
console.log(JSON.stringify({opened,closed,enabled,html,reads,cancelled:!q('#method-transfer-modal').open}));
})();''')
        self.assertTrue(value['opened'])
        self.assertTrue(value['closed'])
        self.assertTrue(value['cancelled'])
        self.assertTrue(value['enabled'])
        self.assertEqual(value['reads'], 2)
        self.assertIn('disabled', value['html'])
        self.assertIn('创建时间', value['html'])
        self.assertNotIn('上传时间', value['html'])

    def test_editor_rerender_keeps_same_input_objects_and_never_polls_catalog(self):
        value = self.node('''
const fields={value:'unsaved text'},version={value:'bob/old',options:[{value:'bob/old'}],insertAdjacentHTML(){}};
globalThis.q=s=>s==='#edit-version'?version:fields;globalThis.qa=()=>[];
globalThis.state={data:{versions:[]}};globalThis.api=()=>{throw Error('unexpected poll')};
let builds=0;const render=SHAQTransfer.preserveEditor(()=>builds++);
render();render();console.log(JSON.stringify({builds,value:fields.value,version:version.value}));
''')
        self.assertEqual(value, {'builds':0,'value':'unsaved text','version':'bob/old'})

    def test_editor_save_keeps_governed_role_policy_in_payload(self):
        value=self.node('''
(async()=>{
const nodes={'#draft-id':{value:'draft'},'#edit-skill':{value:'market-common-shock'}};
globalThis.q=s=>nodes[s]||{value:'edited'};globalThis.wb={};
globalThis.state={editorDocument:{package:{agent_profile:{allow_implicit_invocation:false}}}};
globalThis.notice=()=>{};let sent;
globalThis.api=async(...args)=>{sent=args};
await saveMethodDraft();console.log(JSON.stringify(sent));
})();''')
        self.assertEqual(value[0], 'save_skill_package_draft')
        self.assertIs(value[4]['allow_implicit_invocation'], False)

    def test_reopening_during_catalog_load_does_not_race_pinned_operation(self):
        value=self.node('''
(async()=>{let calls=0;const releases=[];
const c=SHAQTransfer.controller('download',async()=>{calls++;return new Promise(resolve=>releases.push(resolve))},()=>{});
const first=c.open();const second=c.open();
releases.forEach(release=>release({operation_id:'pinned',rows:[]}));await Promise.all([first,second]);
console.log(JSON.stringify({calls,operation:c.operation_id}));})();''')
        self.assertEqual(value, {'calls':1,'operation':'pinned'})
