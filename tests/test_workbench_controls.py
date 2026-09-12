import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class WorkbenchControlsTests(unittest.TestCase):
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
