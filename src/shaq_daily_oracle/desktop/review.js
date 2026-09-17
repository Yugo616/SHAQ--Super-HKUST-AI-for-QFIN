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
    const signed=value=>value==null?'—':`${value>=0?'+':'-'}${usd(Math.abs(value))}`;
    const body=rows.flatMap(row=>{
      const predictions=row.predictions?.length?row.predictions:[{}];
      const account=accounts.find(value=>value.batch_id===row.batch_id&&value.variant_key===row.variant_key);
      const meta=SHAQAccounts.historyIdentity(row,versions),model=SHAQAccounts.modelCaption([],row.model);
      const phase=row.status==='engineering_failure'?'运行失败':row.score_eligible===false?'过时结果，仅供参考':row.status==='empty'?'空榜':'已正常运行';
      const complete=['final','provisional','empty'].includes(account?.status);
      const total=complete?signed(account.net_pnl):'—';
      const retry=account?.reconciliation_pending?'<span>上次结算 · 待重新核对</span> <button class="text-button" data-reconcile-results>重新核对</button>':['unavailable','incomplete','error'].includes(account?.status)?` <button class="text-button" data-retry-minute-date="${esc(row.trade_date)}">补取缺失行情</button>`:'';
      const header=`<tr class="result-group" data-comparison-group="true" data-batch="${esc(row.batch_id)}" data-variant-key="${esc(row.variant_key)}"><td colspan="9"><div class="result-group-heading"><span class="result-group-choice"></span><b>${esc(row.trade_date)} · ${esc(meta.method_name)}</b><span>${esc(model?model+' · ':'')}${esc(phase)}</span><span class="result-group-total">当日合计 ${total} · 余额 ${usd(account?.account_balance)}</span>${retry}</div></td></tr>`;
      return [header,...predictions.map(prediction=>{
        const value=outcome(row.labels?.[prediction.symbol],prediction);
        const trade=account?.trades?.find(item=>item.symbol===prediction.symbol);
        const missing=trade?.status==='unavailable_entry'||trade?.status==='open_incomplete';
        const pnl=trade?.status==='closed'?signed(trade.net_pnl):missing?'缺少行情':!prediction.symbol&&row.status==='empty'?'无交易':'—';
        const exception=trade?.entry_exception?` <small>本次使用${esc(trade.entry_reference_at_et?.slice(11,16)||'替代分钟')}</small>`:'';
        return `<tr class="clickable" data-batch="${esc(row.batch_id)}" data-variant-key="${esc(row.variant_key)}" data-result-symbol="${esc(prediction.symbol||'')}"><td></td><td></td><td>${esc(prediction.symbol||'—')}${exception}</td><td>${direction(prediction.direction)}</td><td>${usd(value.opening)}</td><td>${usd(value.closing)}</td><td>${value.change===null?'—':value.change.toFixed(2)+'%'}</td><td>${value.correct===null?'—':value.correct?'正确':'错误'}</td><td>${pnl}</td></tr>`;
      })];
    }).join('');
    return `<div class="account-scroll"><table class="table result-table"><thead><tr><th></th><th></th><th>股票</th><th>预测方向</th><th>开盘价</th><th>收盘价</th><th>开收涨跌幅</th><th>对错</th><th>本股净盈亏</th></tr></thead><tbody>${body||'<tr><td colspan="9">尚无本地研究记录</td></tr>'}</tbody></table></div>`;
  }
  return {dailyHtml,outcome};
})();

if(typeof document!=='undefined'){
  const beforeBatch=renderBatch;
  renderBatch=function(batch,key,symbol){
    beforeBatch(batch,key,symbol);
    const selected=key&&batch.variants?.[key]?key:Object.keys(batch.variants||{})[0];
    const modelLine=q('#batch-detail .batch-head > p:not(.eyebrow)');
    if(modelLine){
      const name=batch.model_display?.[selected]?.name||SHAQAccounts.modelCaption(batch.model_calls||[],batch.variants?.[selected]?.model_name);
      modelLine.textContent=name?`运行模型：${name}`:'';
      modelLine.hidden=!name;
    }
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
    q('#history').innerHTML=`<div class="history-filters"><label>开始日期<input id="history-from" type="date" value="${esc(filters.from||'')}"></label><label>结束日期<input id="history-to" type="date" value="${esc(filters.to||'')}"></label><label>版本<select id="history-version"><option value="">全部版本</option>${[...identities].map(([key,name])=>`<option value="${esc(key)}"${filters.version===key?' selected':''}>${esc(name)}</option>`).join('')}</select></label><label>模型<select id="history-model"><option value="">全部模型</option>${models.map(model=>`<option${filters.model===model?' selected':''}>${esc(model)}</option>`).join('')}</select></label></div><section class="sheet balance-overview">${SHAQAccounts.compactOverviewHtml(accounts,versions,filters)}</section><section class="sheet"><h2>每日结果</h2>${SHAQResults.dailyHtml(rows,accounts.results||[],versions)}</section>`;
    for(const [id,key] of [['history-from','from'],['history-to','to'],['history-version','version'],['history-model','model']])q('#'+id).onchange=event=>{filters[key]=event.target.value;renderHistory()};
    const jobs=state.data.jobs||[], unfinished=new Map();
    for(const job of jobs)if(job.batch_id&&['failed','partial_failure'].includes(job.status)&&
      !jobs.some(other=>other.batch_id===job.batch_id&&other.status==='complete'))unfinished.set(job.batch_id,job);
    if(unfinished.size)q('#history').innerHTML+=`<details class="sheet" data-view-key="unfinished-history"><summary>未完成分析（${unfinished.size}）</summary>${[...unfinished.values()].map(job=>`<p>${esc(SHAQProgress.etDay(job.started_at_et)||'日期未记录')} <button class="secondary" data-history-resume="${esc(job.batch_id)}">继续未完成分析</button></p>`).join('')}</details>`;
    qa('[data-history-resume]').forEach(button=>button.onclick=async()=>{
      button.disabled=true;try{await api('resume_shadow_batch',button.dataset.historyResume);notice('正在恢复原批次，仅补未完成分析');await load(false)}catch(error){notice(error.message,true)}finally{button.disabled=false}
    });
    qa('[data-result-symbol]').forEach(row=>row.onclick=()=>loadBatch(row.dataset.batch,row.dataset.variantKey,row.dataset.resultSymbol||undefined));
    qa('[data-retry-minute-date]').forEach(button=>{
      button.disabled=['running','already_running'].includes(state.data.result_refresh?.status);
      button.onclick=async event=>{event.stopPropagation();button.disabled=true;button.textContent='正在补取…';try{await api('retry_missing_minutes',button.dataset.retryMinuteDate);await load(false)}catch(error){notice(error.message,true);button.disabled=false;button.textContent='补取缺失行情'}};
    });
    qa('[data-reconcile-results]').forEach(button=>button.onclick=async event=>{
      event.stopPropagation();button.disabled=true;
      try{await api('refresh_prices_and_results');await load(false)}
      catch(error){notice(error.message,true)}finally{button.disabled=false}
    });
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
