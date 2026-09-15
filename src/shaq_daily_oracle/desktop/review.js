// Display only: prices and predictions are read from their saved records.
const SHAQResults=(()=>{
  const esc=value=>SHAQAccounts.escape(value),usd=value=>SHAQAccounts.usd(value);
  const direction=value=>({bullish:'看涨',bearish:'看跌',neutral:'中性'}[value]||'—');
  function outcome(label={},prediction={}){
    const price=value=>typeof value==='number'&&Number.isFinite(value)&&value>0?value:null;
    const opening=price(label.official_unadjusted_open),closing=price(label.official_unadjusted_close);
    const ready=['provisional','final'].includes(label.status)&&opening!==null&&closing!==null;
    const change=ready?(closing/opening-1)*100:null;
    const actual=ready?(closing>opening?'bullish':closing<opening?'bearish':'neutral'):null;
    return {opening,closing,change,correct:actual&&['bullish','bearish','neutral'].includes(prediction.direction)?actual===prediction.direction:null};
  }
  function dailyHtml(rows,accounts=[],versions=[]){
    const body=rows.flatMap(row=>{
      const predictions=row.predictions?.length?row.predictions:[{}];
      const account=accounts.find(value=>value.batch_id===row.batch_id&&value.variant_key===row.variant_key);
      return predictions.map(prediction=>{
        const value=outcome(row.labels?.[prediction.symbol],prediction),meta=SHAQAccounts.historyIdentity(row,versions);
        const phase=({provisional:'初步',final:'已复核',engineering_failure:'分析失败',empty:'空榜'}[row.status]||'等待结果');
        return `<tr class="clickable" data-batch="${esc(row.batch_id)}" data-variant-key="${esc(row.variant_key)}" data-result-symbol="${esc(prediction.symbol||'')}"><td>${esc(row.trade_date)}</td><td>${esc(meta.method_name)}<br><small>${esc(row.model||'未记录模型')} · ${esc(phase)}${row.score_eligible===false?' · 不计前瞻成绩':''}</small></td><td>${esc(prediction.symbol||'—')}</td><td>${direction(prediction.direction)}</td><td>${usd(value.opening)}</td><td>${usd(value.closing)}</td><td>${value.change===null?'—':value.change.toFixed(2)+'%'}</td><td>${value.correct===null?'—':value.correct?'正确':'错误'}</td><td>${usd(account?.status==='unavailable'?null:account?.net_pnl)}</td></tr>`;
      });
    }).join('');
    return `<div class="account-scroll"><table class="table result-table"><thead><tr><th>日期</th><th>版本</th><th>股票</th><th>预测方向</th><th>未复权开盘</th><th>未复权收盘</th><th>开收涨跌幅</th><th>方向成绩</th><th>版本当日净盈亏</th></tr></thead><tbody>${body||'<tr><td colspan="9">尚无本地研究记录</td></tr>'}</tbody></table></div>`;
  }
  return {dailyHtml,outcome};
})();

if(typeof document!=='undefined'){
  const beforeBatch=renderBatch;
  renderBatch=function(batch,key,symbol){
    beforeBatch(batch,key,symbol);
    const selected=key&&batch.variants?.[key]?key:Object.keys(batch.variants||{})[0];
    const selector=q('#compare-version');if(!selector)return;
    selector.onchange=async()=>{
      const target=q('#version-comparison');
      if(!selector.value){target.comparisonRequest=null;target.innerHTML='';return;}
      await showRunComparison({batch_id:batch.batch_id,variant_key:selected},
        {batch_id:batch.batch_id,variant_key:selector.value},target);
    };
  };
  const beforeCandidate=window.showCandidate;
  window.showCandidate=function(batchId,key,symbol){
    beforeCandidate(batchId,key,symbol);
    const batch=state.selectedBatch,variant=batch?.variants?.[key];
    const prediction=variant?.predictions?.find(row=>row.symbol===symbol)||{};
    const value=SHAQResults.outcome(batch?.labels?.labels?.[symbol],prediction);
    const panel=q('#candidate-analysis .aftermarket');
    if(panel)panel.innerHTML=`<b>盘后方向成绩</b><p>未复权开盘 ${SHAQAccounts.usd(value.opening)} · 收盘 ${SHAQAccounts.usd(value.closing)} · 开收涨跌幅 ${value.change===null?'—':value.change.toFixed(2)+'%'} · ${value.correct===null?'—':value.correct?'正确':'错误'}</p>`;
    qa('#candidate-analysis .domain h4').forEach((heading,index)=>{const report=variant?.reports_by_symbol?.[symbol]?.[index];if(report)heading.textContent=moduleName(report.domain)+' · '+dir(report.verdict)});
  };

  renderHistory=function(){
    const all=state.data.dashboard.daily_results||[],versions=state.data.versions||[];
    const accounts=state.data.dashboard.virtual_accounts||{},filters=wb.filters;
    const identities=new Map(all.map(row=>{const meta=historyIdentity(row);return [meta.filter_key,meta.method_name]}));
    const models=[...new Set(all.map(row=>row.model||'未记录模型'))];
    const rows=all.filter(row=>(!filters.from||row.trade_date>=filters.from)&&(!filters.to||row.trade_date<=filters.to)&&(!filters.version||historyIdentity(row).filter_key===filters.version)&&(!filters.model||(row.model||'未记录模型')===filters.model));
    q('#history').innerHTML=`<div class="history-filters"><label>开始日期<input id="history-from" type="date" value="${esc(filters.from||'')}"></label><label>结束日期<input id="history-to" type="date" value="${esc(filters.to||'')}"></label><label>版本<select id="history-version"><option value="">全部版本</option>${[...identities].map(([key,name])=>`<option value="${esc(key)}"${filters.version===key?' selected':''}>${esc(name)}</option>`).join('')}</select></label><label>模型<select id="history-model"><option value="">全部模型</option>${models.map(model=>`<option${filters.model===model?' selected':''}>${esc(model)}</option>`).join('')}</select></label></div><section class="sheet balance-overview">${SHAQAccounts.compactOverviewHtml(accounts,versions,filters)}</section><section class="sheet"><h2>每日结果</h2><p>点击股票查看当时的六领域分析与最终判断。版本当日净盈亏为整版本账户结果，不是单只股票盈亏。</p>${SHAQResults.dailyHtml(rows,accounts.results||[],versions)}</section>`;
    for(const [id,key] of [['history-from','from'],['history-to','to'],['history-version','version'],['history-model','model']])q('#'+id).onchange=event=>{filters[key]=event.target.value;renderHistory()};
    qa('[data-result-symbol]').forEach(row=>row.onclick=()=>loadBatch(row.dataset.batch,row.dataset.variantKey,row.dataset.resultSymbol||undefined));
    addHistoryComparisonControls();
    // Stock rows share one frozen version record. Offer that record once,
    // while keeping different batches of the same version independently selectable.
    const comparisonRecords=new Set();
    qa('#history .compare-record').forEach(input=>{
      const key=input.dataset.comparisonKey;
      if(comparisonRecords.has(key))input.remove();else comparisonRecords.add(key);
    });
  };
}
