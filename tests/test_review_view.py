import json
import subprocess
import unittest
from pathlib import Path


class ReviewViewTests(unittest.TestCase):
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
