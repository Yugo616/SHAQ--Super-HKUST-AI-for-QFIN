import json
import subprocess
import unittest
from pathlib import Path


class ReviewViewTests(unittest.TestCase):
    def test_immediate_summary_respects_filters_and_preserves_empty_count(self):
        source = (Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding="utf-8")
        harness = '''
const window={showCandidate(){}};let renderBatch=()=>{},renderHistory=()=>{};
const esc=x=>String(x??''),moduleName=x=>x,dir=x=>x,money=x=>String(x);
const document={createElement:()=>({innerHTML:''})},notice=()=>{},api=async()=>{};
const SHAQAccounts={methodMeta:()=>({}),statusName:x=>x,usd:x=>x};
const wb={filters:{from:'2026-09-09',to:'2026-09-09',version:'team/main',model:'m'}};
const historyIdentity=row=>({filter_key:row.variant_key});
const rows=[
 {trade_date:'2026-09-09',variant_key:'team/main',model:'m',status:'provisional',score_eligible:true,correct:1,incorrect:0},
 {trade_date:'2026-09-08',variant_key:'team/main',model:'m',status:'final',score_eligible:true,correct:0,incorrect:1},
 {trade_date:'2026-09-09',variant_key:'team/main',model:'m',status:'empty',score_eligible:true,correct:0,incorrect:0}];
const state={data:{dashboard:{daily_results:rows}}};
const summary={firstChild:{textContent:''}};
const q=s=>s==='#history .result-summary'?summary:null,qa=()=>[];
'''
        output = subprocess.check_output(
            ['node', '-'], input=harness + source +
            '\nrenderHistory();console.log(summary.firstChild.textContent);',
            text=True, encoding='utf-8')
        self.assertIn('正确 1 / 错误 0', output)
        self.assertIn('空榜 1 次', output)

    def test_candidate_summary_renders_as_postclose_and_escapes_source(self):
        source = (Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding="utf-8")
        harness = '''
const window={showCandidate(){}}; let renderBatch=()=>{};
const esc=x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const moduleName=x=>x;
let result=''; const document={createElement:()=>({innerHTML:''})};
const q=()=>({after:e=>result=e.innerHTML});
const state={selectedBatch:{replay_summaries:{version:{X:{correct:false,return_pct:-1,
 explanation:'实际方向未支持预测；不能确定原因',basis:[{domain:'event',thesis:'<script>bad</script>',antithesis:'可能已吸收',evidence_ids:['e1']}]}}}}};
'''
        output = subprocess.check_output(['node', '-'], input=harness + source + "\nwindow.showCandidate('b','version','X');console.log(result);", text=True, encoding="utf-8")
        self.assertIn('盘后查看', output)
        self.assertIn('预测错误', output)
        self.assertIn('&lt;script&gt;', output)
        self.assertNotIn('<script>', output)
        self.assertIn('e1', output)

    def comparison_result(self, error_message):
        source = (Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding="utf-8")
        harness = f'''
const window={{showCandidate(){{}}}}; let renderBatch=()=>{{}};
const esc=x=>String(x??""); const moduleName=x=>x; const dir=x=>x;
const selector={{value:"",onchange:null}}; let rendered="",notices=[];
const document={{createElement:()=>({{innerHTML:""}})}};
const q=s=>s==="#compare-version"?selector:s==="#version-comparison"?{{prepend:e=>rendered=e.innerHTML}}:{{after(){{}}}};
const state={{data:{{versions:[
  {{author:"team",version_id:"left",method_name:"左方法",status_badge:"正式基准"}},
  {{author:"team",version_id:"right",method_name:"右方法",status_badge:"Shadow"}}
]}}}};
const SHAQAccounts={{methodMeta:key=>key.endsWith("left")?{{method_name:"左方法",status_badge:"正式基准"}}:{{method_name:"右方法",status_badge:"Shadow"}},statusName:x=>x,usd:x=>"$"+x}};
const notice=(message,bad)=>notices.push([message,bad]);
const api=async()=>{{throw new Error({json.dumps(error_message)})}};
const batch={{variants:{{"team/left":{{variant:{{}},predictions:[],integration_audit:{{}}}},"team/right":{{variant:{{}},predictions:[],integration_audit:{{}}}}}},version_comparisons:{{"team/left":{{"team/right":{{changed_modules:[],same_model:true,stocks:[]}}}}}},virtual_accounts:{{results:[]}}}};
'''
        script = harness + source + '''
renderBatch(batch,"team/left");selector.value="team/right";
(async()=>{let rejected="";try{await selector.onchange()}catch(error){rejected=error.message}console.log(JSON.stringify({rendered,notices,rejected}))})()
'''
        return json.loads(subprocess.check_output(['node', '-'], input=script, text=True, encoding="utf-8"))

    def test_missing_installed_method_is_explicitly_unavailable(self):
        value = self.comparison_result('Skill version is not installed: team/right')
        self.assertIn('实际安装包比较不可用', value['rendered'])
        self.assertIn('历史版本未安装', value['rendered'])
        self.assertEqual(value['notices'], [])
        self.assertEqual(value['rejected'], '')

    def test_unexpected_installed_comparison_error_is_visible_and_propagates(self):
        value = self.comparison_result(
            'Skill version is not installed: team/right; manifest hash mismatch'
        )
        self.assertEqual(value['rendered'], '')
        self.assertEqual(value['notices'], [
            ['安装包比较失败：Skill version is not installed: '
             'team/right; manifest hash mismatch', True]
        ])
        self.assertEqual(
            value['rejected'],
            'Skill version is not installed: team/right; manifest hash mismatch',
        )
