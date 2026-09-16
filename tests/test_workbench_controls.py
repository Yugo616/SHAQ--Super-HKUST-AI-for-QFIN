import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]


class WorkbenchControlsTests(unittest.TestCase):
    def test_api_prefill_synchronizes_drafts_and_preserves_reopened_fields(self):
        result = self.node('connections.js', '''
const fields=Object.fromEntries(['profile_id','protocol','model','secret','base_url','relay_base_url','auth_style','output_mode','maximum_context_tokens'].map(k=>[k,{value:''}]));
fields.protocol.value='openai-responses';fields.base_url.value='https://api.openai.com/v1';
const form={elements:fields,dataset:{},classList:{remove(){},add(){}}};
const nodes={'#model-form':form};
globalThis.q=s=>nodes[s]||=({dataset:{},classList:{remove(){},add(){},toggle(){}},innerHTML:''});
globalThis.qa=()=>[];
globalThis.state={data:{settings:{active_model_profile_id:'relay',model_profiles:[{profile_id:'relay',protocol:'openai-chat-completions',model:'relay-model',base_url:'https://relay.example/v1',auth_style:'x-api-key',output_mode:'local_validated'}]}}};
globalThis.applyProtocolPreset=()=>{fields.auth_style.value='bearer';fields.output_mode.value='strict';fields.base_url.value=fields.protocol.value==='openai-responses'?'https://api.openai.com/v1':fields.relay_base_url.value;};
bindModelConnections();q('#show-api-form').onclick();fields.secret.value='relay-secret';
q('#show-api-form').onclick();const reopened=fields.auth_style.value;
q('#api-model-options').innerHTML='old-models';fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({reopened,model:fields.model.value,secret:fields.secret.value,url:fields.base_url.value,options:q('#api-model-options').innerHTML}));
''')
        self.assertEqual(result, {'reopened': 'x-api-key', 'model': '', 'secret': '', 'url': 'https://api.openai.com/v1', 'options': ''})

    def node(self, source, body):
        path = ROOT / 'src/shaq_daily_oracle/desktop' / source
        return json.loads(subprocess.check_output(
            ['node', '-'], input=path.read_text(encoding='utf-8') + '\n' + body,
            text=True, encoding='utf-8'))

    def test_connection_failure_can_retry_and_never_falls_back(self):
        result = self.node('connections.js', '''
(async()=>{
const calls=[],states=[];let attempts=0;
const controller=SHAQConnections.controller(async(p,key,test)=>{
 calls.push([p.protocol,key,test]);if(++attempts===1)throw new Error('401: 登录已失效');
 return {ok:true};
},value=>states.push(value));
await controller.test({protocol:'claude-code'},'');
await controller.test({protocol:'claude-code'},'');
console.log(JSON.stringify({calls,states}));
})();''')
        self.assertEqual(result['calls'], [['claude-code', '', True]] * 2)
        self.assertEqual([x['status'] for x in result['states']],
                         ['testing', 'failed', 'testing', 'connected'])
        self.assertIn('401', result['states'][1]['message'])

    def test_connection_blocks_duplicate_probe_while_busy(self):
        result = self.node('connections.js', '''
(async()=>{
let release,calls=0;const controller=SHAQConnections.controller(()=>{
 calls++;return new Promise(resolve=>release=resolve);
},()=>{});
const first=controller.test({protocol:'codex-cli'},'');
await controller.test({protocol:'anthropic-messages'},'secret');
release({});await first;console.log(JSON.stringify({calls,busy:controller.busy}));
})();''')
        self.assertEqual(result, {'calls': 1, 'busy': False})

    def test_api_provider_handler_restores_each_draft_without_forwarding_keys(self):
        result = self.node('connections.js', '''
const fields={protocol:{value:'openai-responses'},model:{value:'gpt-a'},secret:{value:'openai-key'},
 base_url:{value:'https://api.openai.com/v1'},relay_base_url:{value:''},
 auth_style:{value:'bearer'},output_mode:{value:'strict'},maximum_context_tokens:{value:'128000'}};
const form={elements:fields};let applied=[];
const drafts=SHAQConnections.bindProviderDrafts(form,()=>applied.push(fields.protocol.value));
fields.protocol.value='openai-chat-completions';fields.protocol.onchange();
const relayEmpty={model:fields.model.value,secret:fields.secret.value};
fields.model.value='relay-model';fields.secret.value='relay-key';fields.relay_base_url.value='https://relay.test/v1';
fields.protocol.value='anthropic-messages';fields.protocol.onchange();
const anthropicEmpty={model:fields.model.value,secret:fields.secret.value};
fields.model.value='claude-a';fields.secret.value='anthropic-key';
fields.protocol.value='openai-chat-completions';fields.protocol.onchange();
const relayRestored={model:fields.model.value,secret:fields.secret.value,url:fields.relay_base_url.value};
fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({relayEmpty,anthropicEmpty,relayRestored,
 openaiRestored:{model:fields.model.value,secret:fields.secret.value},applied,drafts:[...drafts.keys()]}));''')
        self.assertEqual(result['relayEmpty'], {'model': '', 'secret': ''})
        self.assertEqual(result['anthropicEmpty'], {'model': '', 'secret': ''})
        self.assertEqual(result['relayRestored'], {
            'model': 'relay-model', 'secret': 'relay-key',
            'url': 'https://relay.test/v1',
        })
        self.assertEqual(result['openaiRestored'], {
            'model': 'gpt-a', 'secret': 'openai-key',
        })
        self.assertEqual(result['drafts'], [
            'openai-responses', 'openai-chat-completions', 'anthropic-messages'
        ])

    def test_api_provider_drafts_survive_actual_connection_rebind(self):
        result = self.node('connections.js', '''
const fields={protocol:{value:'openai-responses'},model:{value:'gpt-a'},secret:{value:'openai-key'},
 base_url:{value:'https://api.openai.com/v1'},relay_base_url:{value:''},
 auth_style:{value:'bearer'},output_mode:{value:'strict'},maximum_context_tokens:{value:'128000'}};
const form={elements:fields,classList:{remove(){}},requestSubmit(){}};
const nodes={'#model-form':form};
const q=selector=>nodes[selector]||=( {classList:{remove(){},toggle(){}},focus(){},scrollIntoView(){}} );
const qa=()=>[];const applyProtocolPreset=()=>{};
bindModelConnections();
fields.protocol.value='openai-chat-completions';fields.protocol.onchange();
fields.model.value='relay-model';fields.secret.value='relay-key';
fields.relay_base_url.value='https://relay.test/v1';
bindModelConnections();
fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({model:fields.model.value,secret:fields.secret.value}));''')
        self.assertEqual(result, {'model': 'gpt-a', 'secret': 'openai-key'})

    def test_delayed_success_preserves_new_secret_after_provider_switch(self):
        result = self.node('connections.js', '''
(async()=>{
const fields={protocol:{value:'openai-responses'},model:{value:'gpt-a'},secret:{value:'submitted-key'},
 base_url:{value:'https://api.openai.com/v1'},relay_base_url:{value:'https://unused-relay.test/v1'},
 auth_style:{value:'bearer'},output_mode:{value:'strict'},maximum_context_tokens:{value:'128000'},
 max_concurrency:{value:'2'},rate_limit_per_minute:{value:'30'},input_price_per_million:{value:''},
 output_price_per_million:{value:''}};
const form={elements:fields,classList:{remove(){}},requestSubmit(){}};
const nodes={'#model-form':form,'#model-status':{textContent:'',dataset:{}},
 '#model-error-actions':{classList:{toggle(){}}},'#setup':{classList:{remove(){}}}};
globalThis.q=selector=>nodes[selector]||=( {classList:{remove(){},toggle(){}},focus(){},scrollIntoView(){}} );
globalThis.qa=()=>[];globalThis.applyProtocolPreset=()=>{};
globalThis.FormData=class {constructor(target){this.values=Object.entries(target.elements).map(([name,field])=>[name,field.value])}entries(){return this.values[Symbol.iterator]()}};
let release;const calls=[];
globalThis.api=(name,profile,secret,test)=>new Promise(resolve=>{calls.push({name,profile,secret,test});release=resolve});
globalThis.load=async()=>bindModelConnections();
bindModelConnections();
const pending=form.onsubmit({preventDefault(){}});
fields.protocol.value='openai-chat-completions';fields.protocol.onchange();
fields.model.value='relay-model';fields.secret.value='new-relay-key';fields.secret.oninput?.();
release({ok:true});await pending;
const relay={model:fields.model.value,secret:fields.secret.value};
fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({relay,officialSecret:fields.secret.value,call:calls[0]}));
})();''')
        self.assertEqual(result['relay'], {
            'model': 'relay-model', 'secret': 'new-relay-key',
        })
        self.assertEqual(result['officialSecret'], '')
        self.assertEqual(result['call']['secret'], 'submitted-key')
        self.assertEqual(result['call']['profile']['protocol'], 'openai-responses')
        self.assertEqual(result['call']['profile']['base_url'], 'https://api.openai.com/v1')
        self.assertNotIn('relay_base_url', result['call']['profile'])

    def test_delayed_success_preserves_same_provider_secret_edit(self):
        result = self.node('connections.js', '''
(async()=>{
const fields={protocol:{value:'openai-responses'},model:{value:'gpt-a'},secret:{value:'submitted-key'},
 base_url:{value:'https://api.openai.com/v1'},relay_base_url:{value:''},auth_style:{value:'bearer'},
 output_mode:{value:'strict'},maximum_context_tokens:{value:'128000'},max_concurrency:{value:'2'},
 rate_limit_per_minute:{value:'30'},input_price_per_million:{value:''},output_price_per_million:{value:''}};
const form={elements:fields,classList:{remove(){}},requestSubmit(){}};
const nodes={'#model-form':form,'#model-status':{textContent:'',dataset:{}},
 '#model-error-actions':{classList:{toggle(){}}},'#setup':{classList:{remove(){}}}};
globalThis.q=selector=>nodes[selector]||=( {classList:{remove(){},toggle(){}},focus(){},scrollIntoView(){}} );
globalThis.qa=()=>[];globalThis.applyProtocolPreset=()=>{};
globalThis.FormData=class {constructor(target){this.values=Object.entries(target.elements).map(([name,field])=>[name,field.value])}entries(){return this.values[Symbol.iterator]()}};
let release;globalThis.api=()=>new Promise(resolve=>release=resolve);globalThis.load=async()=>bindModelConnections();
bindModelConnections();
const pending=form.onsubmit({preventDefault(){}});
fields.secret.value='new-official-key';fields.secret.oninput?.();
release({ok:true});await pending;
const activeSecret=fields.secret.value;
fields.protocol.value='anthropic-messages';fields.protocol.onchange();
fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({activeSecret,restoredSecret:fields.secret.value}));
})();''')
        self.assertEqual(result, {
            'activeSecret': 'new-official-key',
            'restoredSecret': 'new-official-key',
        })

    def test_connection_controller_formats_structured_chinese_diagnostic(self):
        result = self.node('connections.js', '''
(async()=>{const states=[];const error=new Error('opaque');error.diagnostic={
 status:429,provider_code:'rate_limit_exceeded',provider_message:'请稍后 retry',request_id:'req_123'};
const controller=SHAQConnections.controller(async()=>{throw error},x=>states.push(x));
await controller.test({protocol:'openai-responses'},'secret');
console.log(JSON.stringify(states.at(-1)));})();''')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('HTTP 429', result['message'])
        self.assertIn('rate_limit_exceeded', result['message'])
        self.assertIn('req_123', result['message'])
        self.assertEqual(result['diagnostic']['status'], 429)

    def test_successful_api_save_clears_provider_draft_secret(self):
        result = self.node('connections.js', '''
const fields={protocol:{value:'openai-responses'},model:{value:'gpt-a'},secret:{value:'saved-key'},
 base_url:{value:'https://api.openai.com/v1'},relay_base_url:{value:''},auth_style:{value:'bearer'},
 output_mode:{value:'strict'},maximum_context_tokens:{value:'128000'}};
const form={elements:fields},drafts=SHAQConnections.bindProviderDrafts(form,()=>{});
SHAQConnections.clearDraftSecret(drafts,'openai-responses');fields.secret.value='';
fields.protocol.value='anthropic-messages';fields.protocol.onchange();
fields.protocol.value='openai-responses';fields.protocol.onchange();
console.log(JSON.stringify({secret:fields.secret.value,draft:drafts.get('openai-responses').secret}));''')
        self.assertEqual(result, {'secret': '', 'draft': ''})

    def test_comparison_renders_unknowns_and_costs_without_false_zero_or_html(self):
        result = self.node('comparison.js', '''
const html=SHAQComparison.html({left:{label:'<img src=x>',trade_date:'2026-09-09'},
right:{label:'Shadow',trade_date:'2026-09-09'},controlled_method_comparison:false,
dimensions:{method:{status:'different'},model:{status:'unknown'},data:{status:'same'},
candidates:{status:'same'},trading_rules:{status:'unknown'},trade_date:{status:'same'}},
changed_files:[{path:'skills/event/SKILL.md',diff:'+<script>bad</script>'}],
stocks:[{symbol:'AAA',left:'bullish',right:'not_published',left_reason:{},right_reason:{rejection_reasons:['证据不足']}}],
outcomes:{left:{net_pnl:null,status:'pending'},right:{net_pnl:-2.5,status:'provisional'}}});
console.log(JSON.stringify(html));''')
        self.assertIn('未记录', result)
        self.assertIn('不能单独归因于方法', result)
        self.assertIn('&lt;img', result)
        self.assertNotIn('<script>', result)
        self.assertIn('-$2.50', result)
        self.assertNotIn('$0.00', result)

    def test_official_install_action_is_fixed_and_rejects_arbitrary_protocol(self):
        from shaq_daily_oracle.desktop import DesktopBridge
        bridge = object.__new__(DesktopBridge)
        with patch('shaq_daily_oracle.desktop.webbrowser.open', return_value=True) as opened:
            self.assertTrue(bridge.open_model_installation('claude-code')['ok'])
            opened.assert_called_once_with('https://code.claude.com/docs/en/setup')
            self.assertFalse(bridge.open_model_installation('https://evil.example')['ok'])
            self.assertEqual(opened.call_count, 1)

    def test_local_login_starts_only_after_explicit_click_then_retests_before_save(self):
        result = self.node('connections.js', '''
(async()=>{
const nodes={
 '#model-status':{textContent:'',dataset:{}},
 '#model-error-actions':{classList:{toggle(){}}},
 '#login-model':{dataset:{},textContent:'',classList:{toggle(name,hidden){this.hidden=hidden}}}
};
globalThis.q=selector=>nodes[selector]||={classList:{toggle(){}}};
globalThis.qa=()=>[];let saveAttempts=0;const calls=[];
globalThis.api=async(name,...args)=>{calls.push([name,...args]);if(name==='save_lab_model_profile'&&++saveAttempts===1){
 const error=new Error('尚未登录');error.diagnostic={kind:'authentication',login_protocol:'codex-cli'};throw error;
}return {ok:true}};
globalThis.load=async()=>{};
globalThis.state={data:{settings:{model_profiles:[]}}};
nodes['#local-model-form']={dataset:{protocol:'codex-cli'},classList:{toggle(){},add(){},remove(){}},elements:{model:{value:'chosen-model'}}};
nodes['#local-model-options']={innerHTML:''};
nodes['#model-form']={classList:{add(){}}};
globalThis.esc=x=>x;
await saveLocalModel();const before=calls.map(x=>x[0]);
const protocol=nodes['#login-model'].dataset.protocol;
await loginLocalModel();
console.log(JSON.stringify({before,after:calls.map(x=>x[0]),protocol}));
})();''')
        self.assertEqual(result['before'], ['save_lab_model_profile'])
        self.assertEqual(result['after'], [
            'save_lab_model_profile', 'begin_local_model_login', 'list_local_models'
        ])
        self.assertEqual(result['protocol'], 'codex-cli')

    def test_model_picker_does_not_infer_or_save_until_user_selects(self):
        result = self.node('connections.js', '''
(async()=>{
const nodes={};globalThis.q=s=>nodes[s]||={textContent:'',innerHTML:'',dataset:{},
 classList:{toggle(){},add(){},remove(){}},elements:{model:{value:''}}};
globalThis.qa=()=>[];globalThis.esc=x=>x;
globalThis.state={data:{settings:{model_profiles:[]}}};globalThis.load=async()=>{};
const calls=[];globalThis.api=async(name,...args)=>{calls.push([name,...args]);
 return {models:[{id:'small-live',label:'Small Live'},{id:'big-live',label:'Big Live'}]}};
await connectLocalModel('codex-cli');
const initially=q('#local-model-form').elements.model.value;
await saveLocalModel();
const before=calls.map(x=>x[0]);
q('#local-model-form').elements.model.value='small-live';
await saveLocalModel();
console.log(JSON.stringify({initially,before,calls}));
})();''')
        self.assertEqual(result['initially'], '')
        self.assertEqual(result['before'], ['list_local_models'])
        self.assertEqual(result['calls'][-1][0], 'save_lab_model_profile')
        self.assertEqual(result['calls'][-1][1]['model'], 'small-live')

    def test_saved_local_model_updates_visible_manual_and_schedule_selectors(self):
        result = self.node('connections.js', '''
(async()=>{
const nodes={};globalThis.q=s=>nodes[s]||={textContent:'',innerHTML:'',dataset:{},
 classList:{toggle(){},add(){},remove(){}},elements:{model:{value:''}}};
globalThis.qa=()=>[];globalThis.esc=x=>x;
globalThis.state={data:{settings:{model_profiles:[{profile_id:'old',protocol:'codex-cli',model:'old-model'}]}}};
q('#run-profile').tagName='SELECT';q('#auto-model').tagName='SELECT';q('#auto-time').value='08:25';
globalThis.load=async()=>{q('#run-profile').value='old';q('#auto-model').value='old';};
globalThis.api=async(name,profile)=>{
 if(name==='save_lab_model_profile')state.data.settings.model_profiles.push(profile);
 return {models:[{id:'new-model',label:'New Model'}]};};
await connectLocalModel('claude-code');q('#local-model-form').elements.model.value='new-model';
await saveLocalModel();
console.log(JSON.stringify({manual:q('#run-profile').value,automatic:q('#auto-model').value,time:q('#auto-time').value}));
})();''')
        self.assertEqual(result, {'manual':'my-claude','automatic':'my-claude','time':'08:25'})

    def test_old_save_refresh_cannot_restore_model_over_a_newer_save(self):
        result = self.node('connections.js', '''
(async()=>{
const nodes={};globalThis.q=s=>nodes[s]||={tagName:'SELECT',value:'old'};
globalThis.esc=x=>x;globalThis.state={data:{settings:{model_profiles:[]}}};
const releases=[];globalThis.load=()=>new Promise(resolve=>releases.push(resolve));
const a=refreshSavedModel({profile_id:'a',model:'A'});
const b=refreshSavedModel({profile_id:'b',model:'B'});
releases[1](true);const latest=await b;
releases[0](undefined);const old=await a;
console.log(JSON.stringify({manual:q('#run-profile').value,automatic:q('#auto-model').value,latest,old}));
})();''')
        self.assertEqual(result, {'manual':'b','automatic':'b','latest':True,'old':False})

    def test_cancelled_local_login_does_not_retry_or_switch_provider(self):
        result = self.node('connections.js', '''
(async()=>{
const nodes={
 '#model-status':{textContent:'',dataset:{}},
 '#model-error-actions':{classList:{toggle(){}}},
 '#login-model':{dataset:{protocol:'claude-code'},textContent:'',classList:{toggle(){}}}
};
globalThis.q=selector=>nodes[selector]||={classList:{toggle(){}}};globalThis.qa=()=>[];
const calls=[];globalThis.api=async(name,...args)=>{calls.push([name,...args]);
 const error=new Error('RAW OAuth output');error.diagnostic={kind:'login_failed',login_protocol:'claude-code'};throw error};
await loginLocalModel();
console.log(JSON.stringify({calls,status:nodes['#model-status'].dataset.status,
 message:nodes['#model-status'].textContent}));
})();''')
        self.assertEqual([row[0] for row in result['calls']], ['begin_local_model_login'])
        self.assertEqual(result['calls'][0][1], 'claude-code')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('取消', result['message'])

    def test_local_cli_failures_render_bounded_messages_without_changing_http_details(self):
        result = self.node('connections.js', '''
const cases={
 missing:{kind:'executable_missing',protocol:'codex-cli'},
 unusable:{kind:'executable_unusable',protocol:'claude-code'},
 unsigned:{kind:'authentication',login_protocol:'codex-cli'},
 invalid:{kind:'status_invalid',protocol:'claude-code'},
 cancelled:{kind:'login_failed',login_protocol:'claude-code'},
 unknownLocal:{login_protocol:'codex-cli'},
 http:{status:401,provider_code:'invalid_api_key',provider_message:'denied',request_id:'req-safe'}
};
console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(
 ([name,value])=>[name,SHAQConnections.diagnosticMessage(value,'RAW CLI OUTPUT https://oauth.example?code=secret')]))));''')
        self.assertIn('未找到', result['missing'])
        self.assertIn('无法使用', result['unusable'])
        self.assertIn('尚未登录', result['unsigned'])
        self.assertIn('无法确认', result['invalid'])
        self.assertIn('取消', result['cancelled'])
        self.assertIn('本机模型连接失败', result['unknownLocal'])
        for name in ('missing','unusable','unsigned','invalid','cancelled','unknownLocal'):
            self.assertNotIn('oauth.example', result[name])
            self.assertNotIn('secret', result[name])
        self.assertEqual(result['http'],
                         'HTTP 401 · 服务代码：invalid_api_key · 服务信息：denied · 请求编号：req-safe')

    def test_bridge_login_action_rejects_api_protocol_without_subprocess(self):
        from shaq_daily_oracle.desktop import DesktopBridge
        bridge = object.__new__(DesktopBridge)
        with patch('shaq_daily_oracle.model_backends.subprocess.run') as run:
            result = bridge.begin_local_model_login('openai-responses')
        self.assertFalse(result['ok'])
        run.assert_not_called()

    def test_failed_local_probe_never_reaches_profile_storage(self):
        from shaq_daily_oracle import lab_service
        from shaq_daily_oracle.lab_service import LabService
        from shaq_daily_oracle.model_backends import ModelBackendError

        lab = object.__new__(LabService)
        lab.settings = Mock()
        profile = {
            'profile_id':'my-codex','protocol':'codex-cli','base_url':'',
            'model':'subscription-default',
        }
        with patch.object(
            lab_service, 'probe_model_profile',
            side_effect=ModelBackendError('用户取消或登录失败'),
        ):
            with self.assertRaises(ModelBackendError):
                lab.save_model_profile(profile, secret='', probe=True)
        lab.settings.save_model_profile.assert_not_called()

    def test_late_comparison_response_cannot_replace_new_selection(self):
        value=self.node('comparison.js', '''
let requests=[];const api=()=>new Promise(resolve=>requests.push(resolve));
const esc=String,target={innerHTML:''};
(async()=>{
const first=showRunComparison({}, {},target),second=showRunComparison({}, {},target);
const result=label=>({left:{label},right:{label:'right'},dimensions:{},stocks:[],outcomes:{}});
requests[1](result('new'));await second;requests[0](result('old'));await first;
console.log(JSON.stringify(target.innerHTML));
})();''')
        self.assertIn('new',value)
        self.assertNotIn('old',value)

    def test_native_fixture_supports_error_retry_without_live_model(self):
        from shaq_daily_oracle.desktop import DesktopBridge, _bind_gui_smoke_fixture
        bridge=object.__new__(DesktopBridge)
        _bind_gui_smoke_fixture(bridge,{}, {})
        first=bridge.save_lab_model_profile({},'',True)
        second=bridge.save_lab_model_profile({},'',True)
        self.assertFalse(first['ok'])
        self.assertIn('fixture',first['error'])
        self.assertTrue(second['ok'])

    def test_repeated_runs_can_each_be_selected_and_survive_rerender(self):
        value=self.node('comparison.js', '''
const inputs=[],controls={};const cell={textContent:'date',prepend:x=>inputs.push(x)};
const row={dataset:{batch:'first',variantKey:'team/main'},children:[cell,{textContent:'main'}]};
const repeats=['first','second'].map(id=>({dataset:{repeatBatch:id,key:'team/main'},before:x=>inputs.push(x)}));
const table={before(){}},document={createElement:()=>({setAttribute(){},dataset:{}})};
const q=selector=>selector==='#history .result-table'?table:(controls[selector] ||= {});
const qa=selector=>selector.includes('data-repeat-batch')?repeats:[row];
const renderHistory=()=>{};
addHistoryComparisonControls();const second=inputs.find(x=>x.dataset.comparisonKey?.includes('second'));
if(second){second.checked=true;second.onchange();}const selected=[...SHAQComparison.selections.values()];
inputs.length=0;addHistoryComparisonControls();
console.log(JSON.stringify({selected,restored:inputs.find(x=>x.dataset.comparisonKey?.includes('second'))?.checked}));
''')
        self.assertEqual(value['selected'],[{'batch_id':'second','variant_key':'team/main'}])
        self.assertTrue(value['restored'])
