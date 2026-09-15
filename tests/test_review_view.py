import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = r'''
const fs=require('fs'),path=require('path'),vm=require('vm');
const desktop='src/shaq_daily_oracle/desktop';
const html=fs.readFileSync(path.join(desktop,'index.html'),'utf8');
const scripts=[...html.matchAll(/<script src="([^"]+)"/g)].map(row=>row[1]);
const cls={add(){},remove(){},toggle(){}};
const element=()=>new Proxy({classList:cls,dataset:{},parentElement:{prepend(){}},children:[],
 append(){},prepend(){},before(){},after(){},insertBefore(){},insertAdjacentHTML(){},insertAdjacentElement(){},
 addEventListener(){},remove(){},closest(){return null},scrollIntoView(){},focus(){}},
 {get:(o,k)=>k in o?o[k]:'',set:(o,k,v)=>(o[k]=v,true)});
const nodes={};
const ctx={console,window:{addEventListener(){},scrollY:0,scrollTo(){}},
 document:{createElement:element,querySelector:s=>nodes[s]||=(element()),querySelectorAll:()=>[],addEventListener(){}},
 setInterval(){},setTimeout(){},Intl,Date};
vm.createContext(ctx);
for(const file of scripts)vm.runInContext(fs.readFileSync(path.join(desktop,file),'utf8'),ctx,{filename:file});
'''


class ReviewViewTests(unittest.TestCase):
    def bundle(self, body):
        return json.loads(subprocess.check_output(['node', '-'], input=BUNDLE+body,
                          text=True, encoding='utf-8', cwd=ROOT))

    def test_final_history_chain_uses_matching_account_pnl_not_one_share_reference(self):
        value = self.bundle(r'''
vm.runInContext(`state.data={versions:[],dashboard:{daily_results:[
 {batch_id:'b',variant_key:'team/main',trade_date:'2026-09-09',model:'m',status:'final',score_eligible:false,
 predictions:[{symbol:'AAA',direction:'bullish'}],daily_pnl:4.23,cumulative_pnl:4.23}],
 virtual_accounts:{accounts:[],results:[
 {batch_id:'other',variant_key:'team/main',status:'final',net_pnl:999},
 {batch_id:'b',variant_key:'team/main',scope:'historical',status:'final',net_pnl:23.40,account_balance:10023.40}]}}};renderHistory()`,ctx);
console.log(JSON.stringify(nodes['#history'].innerHTML));
''')
        self.assertIn('版本当日净盈亏', value)
        self.assertIn('$23.40', value)
        self.assertIn('不计前瞻成绩', value)
        self.assertNotIn('$999.00', value)
        self.assertNotIn('$4.23', value)
        self.assertNotIn('一股', value)
        self.assertNotIn('<details', value)

    def test_results_filter_dates_method_model_without_dropping_empty_day(self):
        value = self.bundle(r'''
vm.runInContext(`wb.filters={from:'2026-09-09',to:'2026-09-09',version:'team/main',model:'m'};
state.data={versions:[],dashboard:{daily_results:[
 {batch_id:'keep',trade_date:'2026-09-09',variant_key:'team/main',model:'m',status:'provisional',predictions:[{symbol:'KEEP',direction:'bullish'}]},
 {batch_id:'earlier',trade_date:'2026-09-08',variant_key:'team/main',model:'m',status:'final',predictions:[{symbol:'EARLIER'}]},
 {batch_id:'other-model',trade_date:'2026-09-09',variant_key:'team/main',model:'other',status:'final',predictions:[{symbol:'OTHER_MODEL'}]},
 {batch_id:'other-method',trade_date:'2026-09-09',variant_key:'team/other',model:'m',status:'final',predictions:[{symbol:'OTHER_METHOD'}]},
 {batch_id:'empty',trade_date:'2026-09-09',variant_key:'team/main',model:'m',status:'empty',predictions:[]}]}};
renderHistory()`,ctx);
console.log(JSON.stringify(nodes['#history'].innerHTML));
''')
        self.assertIn('KEEP', value)
        self.assertIn('初步', value)
        self.assertIn('空榜', value)
        self.assertIn('data-batch="empty"', value)
        for symbol in ['EARLIER', 'OTHER_MODEL', 'OTHER_METHOD']:
            self.assertNotIn(symbol, value)

    def test_candidate_has_saved_escaped_analysis_and_postclose_direction_not_duplicate_recap(self):
        value = self.bundle(r'''
vm.runInContext(`state.selectedBatch={batch_id:'b',evidence:{candidates:[{symbol:'X'}],catalog:[{evidence_id:'e1',provider:'fixture'}]},
 labels:{labels:{X:{status:'final',official_unadjusted_open:100,official_unadjusted_close:99}}},
 variants:{version:{reports_by_symbol:{X:[{domain:'event',thesis:'<script>bad</script>',antithesis:'可能已吸收',evidence_ids:['e1']}]},
 predictions:[{symbol:'X',direction:'bullish'}],integration_audit:{X:{published:true}},adversary_by_symbol:{}}}};
window.showCandidate('b','version','X')`,ctx);
console.log(JSON.stringify({analysis:nodes['#candidate-analysis'].innerHTML,after:nodes['#candidate-analysis .aftermarket'].innerHTML}));
''')
        self.assertIn('&lt;script&gt;', value['analysis'])
        self.assertNotIn('<script>', value['analysis'])
        self.assertIn('e1', value['analysis'])
        self.assertIn('盘后方向成绩', value['after'])
        self.assertIn('-1.00%', value['after'])
        self.assertIn('错误', value['after'])
        self.assertNotIn('一股', value['after'])
        self.assertNotIn('简短复盘', value['after'])

    def test_details_compare_frozen_records_without_installed_version_lookup(self):
        source = (ROOT / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding='utf-8')
        harness = '''
const window={showCandidate(){}};const document={};
let renderBatch=()=>{},renderHistory=()=>{},called=[];
const target={innerHTML:'old'},selector={value:'right'};
const q=s=>s==='#compare-version'?selector:target;
async function showRunComparison(...args){called=args.slice(0,2)}
const batch={batch_id:'frozen-batch',variants:{left:{},right:{}}};
'''
        value=json.loads(subprocess.check_output(['node','-'],input=harness+source+'''
(async()=>{renderBatch(batch,'left');await selector.onchange();
const first=called;selector.value='';await selector.onchange();
console.log(JSON.stringify({first,cleared:target.innerHTML}));})();
''',text=True,encoding='utf-8',cwd=ROOT))
        self.assertEqual(value['first'], [
            {'batch_id':'frozen-batch','variant_key':'left'},
            {'batch_id':'frozen-batch','variant_key':'right'}])
        self.assertEqual(value['cleared'],'')
