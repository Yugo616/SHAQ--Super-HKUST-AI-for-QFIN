import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import test_research_lab_foundation as foundation
import test_review_view as review
from shaq_daily_oracle.lab_service import LabService


class DataConnectionTests(unittest.TestCase):
    def test_success_enables_only_verified_feed_and_never_saves_secrets_in_receipt(self):
        receipt = {'checked_at': '2026-10-03T08:00:00-04:00', 'premarket_available': True,
                   'orderflow_available': False, 'feeds': {'sip': {'status': 'available'},
                   'iex': {'status': 'unavailable', 'message': '此行情权限未开通'}}}
        with tempfile.TemporaryDirectory() as directory:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(directory)))
            saved = {}
            class Keyring:
                def set_password(self, service, name, value): saved[name] = value
                def get_password(self, service, name): return saved.get(name)
            with patch.object(lab.settings, '_keyring', return_value=Keyring()), patch(
                'shaq_daily_oracle.alpaca_bars.probe_data_connection', return_value=receipt):
                result = lab.save_setup({'data_connection_action': 'connect_alpaca',
                    'alpaca_key_id': 'new-key', 'alpaca_secret_key': 'new-secret'})
                self.assertEqual(lab.settings.get_alpaca_credentials(), ('new-key', 'new-secret'))
            self.assertTrue(result['data_profile']['alpaca_premarket_enabled'])
            self.assertFalse(result['data_profile']['alpaca_orderflow_enabled'])
            self.assertEqual(result['alpaca_connection'], receipt)
            self.assertNotIn('new-secret', lab.paths.research_settings_file.read_text())

    def test_failed_probe_does_not_replace_credentials_or_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(directory)))
            before = lab.settings.load()
            with patch('shaq_daily_oracle.alpaca_bars.probe_data_connection',
                       side_effect=ValueError('测试失败')), patch.object(lab.settings, 'set_alpaca_credentials') as save:
                with self.assertRaisesRegex(ValueError, '测试失败'):
                    lab.save_setup({'data_connection_action': 'connect_alpaca',
                                    'alpaca_key_id': 'new-key', 'alpaca_secret_key': 'new-secret'})
            save.assert_not_called()
            self.assertEqual(lab.settings.load(), before)

    def test_probe_checks_both_feeds_and_rejects_bad_quotes(self):
        from shaq_daily_oracle.alpaca_bars import probe_data_connection
        now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        calls = []
        def respond(request):
            calls.append(request)
            name = 'bars' if request.url.path.endswith('/bars') else 'quotes'
            row = {'t': '2026-10-02T19:00:00Z', 'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 40}
            if name == 'quotes': row = {'t': row['t'], 'bp': 102, 'ap': 100, 'bs': 1, 'as': 1}
            return httpx.Response(200, json={name: {'TEST': [row]}})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = probe_data_connection(key_id='key', secret_key='secret', symbol='TEST',
                                           now=now, client=client, timeout_seconds=1,
                                           delay_seconds=960)
        self.assertTrue(result['premarket_available'])
        self.assertFalse(result['orderflow_available'])
        self.assertEqual([r.url.params['feed'] for r in calls], ['sip', 'iex', 'sip'])
        self.assertTrue(all(r.method == 'GET' and r.url.host == 'data.alpaca.markets' for r in calls))
        self.assertNotIn('secret', json.dumps(result))

    def test_old_quote_sample_is_not_reported_as_today_coverage(self):
        from shaq_daily_oracle.alpaca_bars import probe_data_connection
        def respond(request):
            name='bars' if request.url.path.endswith('/bars') else 'quotes'
            row=({'t':'2026-10-02T19:00:00Z','o':100,'h':101,'l':99,'c':100,'v':40}
                 if name=='bars' else {'t':'2026-10-02T19:00:00Z','bp':100,'ap':101,'bs':100,'as':100})
            return httpx.Response(200,json={name:{'TEST':[row]}})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result=probe_data_connection(key_id='k',secret_key='s',symbol='TEST',
                now=datetime(2026,10,5,12,38,tzinfo=timezone.utc),client=client,
                timeout_seconds=1,delay_seconds=960)
        self.assertTrue(result['orderflow_available'])
        self.assertFalse(result['feeds']['iex']['current_session_sample'])
        self.assertEqual(result['coverage_scope'],'historical_connection_test_not_live_readiness')
        self.assertIn('历史',result['feeds']['iex']['message'])

    def test_probe_authentication_error_is_chinese_and_redacted(self):
        from shaq_daily_oracle.alpaca_bars import probe_data_connection
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text='private-secret'))) as client:
            with self.assertRaises(ValueError) as error:
                probe_data_connection(key_id='key', secret_key='private-secret', symbol='TEST',
                    now=datetime(2026, 10, 3, 12, tzinfo=timezone.utc), client=client,
                    timeout_seconds=1, delay_seconds=960)
        self.assertIn('密钥', str(error.exception))
        self.assertNotIn('private-secret', str(error.exception))


class RunCaptionTests(unittest.TestCase):
    def test_connection_summary_distinguishes_iex_from_delayed_sip_quotes(self):
        value = review.ReviewViewTests().bundle(r'''
console.log(JSON.stringify(vm.runInContext(`dataConnectionSummary({
 sip:{status:'available'},iex:{status:'unavailable',message:'无样本'},
 sip_quotes:{status:'available',message:'历史报价连接成功'}
})`,ctx)));
''')
        self.assertIn('盘前成交量：已连接', value)
        self.assertIn('IEX 报价：无样本', value)
        self.assertIn('延迟 SIP 报价：历史报价连接成功', value)
        self.assertNotIn('IEX 买卖盘：已连接', value)

    def test_data_form_hides_secrets_until_user_opens_connection(self):
        value = review.ReviewViewTests().bundle(r'''
let formHtml='',mounted=false;
const previousQuery=ctx.document.querySelector;
ctx.document.querySelector=s=>s==='#supplemental-data-form'&&!mounted?null:previousQuery(s);
nodes['#data-connections']={append:node=>{formHtml=node.innerHTML;mounted=true}};
vm.runInContext(`state.data={settings:{data_profile:{occ_open_interest_enabled:true},alpaca_credentials_saved:false}};renderDataConnections()`,ctx);
console.log(JSON.stringify({html:formHtml}));
''')
        self.assertIn('id="supplemental-data-form" class="form-grid compact-form hidden"', value['html'])
        self.assertIn('连接 Alpaca', value['html'])
        self.assertNotIn('type="checkbox"', value['html'])

    def test_real_completion_time_converts_timezone_without_hash(self):
        value = review.ReviewViewTests().bundle(r'''
console.log(JSON.stringify(vm.runInContext(`SHAQRunCaption.describe({
 trade_date:'2026-10-02',completed_at_et:'2026-10-02T08:48:32-04:00',
 batch_id:'LAB-2026-10-02-secret-hash'},'Asia/Hong_Kong')`,ctx)));
''')
        self.assertEqual(value['title'], '2026年10月2日运行')
        self.assertIn('20:48:32', value['completion'])
        self.assertNotIn('hash', json.dumps(value))

    def test_missing_completion_does_not_use_updated_time(self):
        value = review.ReviewViewTests().bundle(r'''
console.log(JSON.stringify(vm.runInContext(`SHAQRunCaption.describe({
 trade_date:'2026-10-02',updated_at:'2026-10-03T08:00:00Z',status:'running'})`,ctx)));
''')
        self.assertEqual(value['completion'], '运行中')

    def test_detail_renders_friendly_title_and_removes_explanatory_copy(self):
        value = review.ReviewViewTests().bundle(r'''
vm.runInContext(`renderBatch({batch_id:'LAB-2026-secret-hash',manifest:{trade_date:'2026-10-02'},
 evidence:{as_of_et:'2026-10-02T08:50:00-04:00',candidates:[]},
 variants:{v:{variant:{label:'跨域综合研判版'},completed_at_et:'2026-10-02T08:48:32-04:00'}}},'v')`,ctx);
console.log(JSON.stringify({html:nodes['#batch-detail'].innerHTML}));
''')
        self.assertIn('2026年10月2日运行', value['html'])
        self.assertNotIn('secret-hash', value['html'])
        self.assertNotIn('这一步只决定', value['html'])


if __name__ == '__main__': unittest.main()
