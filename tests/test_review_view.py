import json
import subprocess
import unittest
from pathlib import Path


class ReviewViewTests(unittest.TestCase):
    def test_final_history_chain_promotes_matching_account_values_to_primary_table(self):
        source = (Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding="utf-8")
        harness = '''
const window={showCandidate(){}};let renderBatch=()=>{},reference='';
let buttonClicks=0;
const button={onclick:()=>buttonClicks++},details={children:[button]};
const statusCell={_text:'已复核',children:[details],get textContent(){return [this._text,...this.children.map(x=>x.textContent||'')].join('')},set textContent(value){this._text=value;this.children=[]},querySelector(selector){return selector==='.account-result-status'?this.children.find(x=>x.className==='account-result-status')||null:null},appendChild(node){this.children.push(node)}};
const cells=[...Array.from({length:6},()=>({textContent:'',innerHTML:''})),statusCell];
const tr={dataset:{batch:'b',variantKey:'team/main'},children:cells,insertBefore(node,before){this.children.splice(this.children.indexOf(before),0,node)}};
const header={innerHTML:''},table={insertAdjacentHTML:(where,html)=>reference=html};
let renderHistory=()=>{};
const esc=x=>String(x??''),moduleName=x=>x,dir=x=>x,money=x=>x==null?'—':`${Number(x)>=0?'+':''}$${Number(x).toFixed(2)}`;
const document={createElement:tag=>tag==='td'?{textContent:''}:{className:'',textContent:''}},notice=()=>{},api=async()=>{};
const SHAQAccounts={methodMeta:()=>({}),statusName:x=>x,usd:x=>x==null?'—':'$'+Number(x).toFixed(2),scopeName:x=>x==='historical'?'历史回放 · 不入前瞻账户':x};
const wb={filters:{}},historyIdentity=row=>({filter_key:row.variant_key});
const historyMethod=row=>row.variant_key;
const replayStatus=x=>x;
const daily={batch_id:'b',variant_key:'team/main',trade_date:'2026-09-09',model:'m',status:'final',score_eligible:false,correct:1,incorrect:0,daily_pnl:4.23,cumulative_pnl:4.23};
const account={batch_id:'b',variant_key:'team/main',scope:'historical',status:'final',net_pnl:23.40,account_cumulative_net_pnl:23.40,account_balance:10023.40};
const state={data:{dashboard:{daily_results:[daily],virtual_accounts:{results:[account]}}}};
const summary={firstChild:{textContent:''}};
const q=s=>s==='#history .result-summary'?summary:s==='.result-table thead tr'?header:s==='.result-table'?table:null;
const qa=s=>s==='.result-table tr[data-batch]'?[tr]:[];
'''
        output = subprocess.check_output(['node', '-'], input=harness + source + '''
renderHistory();button.onclick();console.log(JSON.stringify({header:header.innerHTML,cells:tr.children.map(x=>x.textContent),reference,detailsKept:statusCell.children.includes(details),buttonClicks}));
''', text=True, encoding='utf-8')
        value = json.loads(output)
        self.assertIn('账户当日净盈亏', value['header'])
        self.assertEqual(value['cells'][4:7], ['$23.40', '$23.40', '$10023.40'])
        self.assertIn('历史回放', value['cells'][-1])
        self.assertIn('方向状态：final', value['cells'][-1])
        self.assertIn('账户状态：final', value['cells'][-1])
        self.assertTrue(value['detailsKept'])
        self.assertEqual(value['buttonClicks'], 1)
        self.assertIn('一股零成本参考', value['reference'])
        self.assertIn('+\u00244.23', value['reference'])

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

    def test_details_compare_frozen_records_without_installed_version_lookup(self):
        source = (Path(__file__).parents[1] / 'src/shaq_daily_oracle/desktop/review.js').read_text(encoding='utf-8')
        harness = '''
const window={showCandidate(){}};
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
''',text=True,encoding='utf-8'))
        self.assertEqual(value['first'], [
            {'batch_id':'frozen-batch','variant_key':'left'},
            {'batch_id':'frozen-batch','variant_key':'right'}])
        self.assertEqual(value['cleared'],'')
