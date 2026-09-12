/* Read-only comparison of the exact frozen records, not currently installed methods. */
const SHAQComparison = {
  selections: new Map(),
  html(value) {
    const escape=x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;')
      .replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
    const names={method:'方法内容',model:'模型配置',data:'冻结数据',candidates:'候选集合',
      trading_rules:'模拟交易规则',trade_date:'交易日期'};
    const states={same:'相同',different:'不同',unknown:'未记录'};
    const direction={bullish:'看涨',bearish:'看跌',neutral:'中性',not_published:'未发布'};
    const usd=x=>x==null?'—':`${Number(x)<0?'-':''}$${Math.abs(Number(x)).toFixed(2)}`;
    const reason=x=>x?.decision_reason || (x?.rejection_reasons||[]).join('；') || '见原始分析';
    const label=x=>escape(x?.label || x?.variant_key || '未记录版本');
    const outcome=x=>`${escape({final:'已复核',provisional:'初步',pending:'等待行情',empty:'空榜',failed:'失败'}[x?.status]||x?.status||'尚无账户回放')} · 净盈亏 ${usd(x?.net_pnl)}`;
    return `<h3>${label(value.left)} ↔ ${label(value.right)}</h3>
      <p class="muted">左：${escape(value.left?.trade_date||'未记录日期')} · ${escape(value.left?.batch_id||'未记录批次')}<br>右：${escape(value.right?.trade_date||'未记录日期')} · ${escape(value.right?.batch_id||'未记录批次')}</p>
      <p class="comparison-verdict">${value.controlled_method_comparison
        ? '数据、模型、候选、日期与交易规则一致，可以对照方法输出；单次差异不代表效果已经得到证明。'
        : '存在不同或未记录的输入，结果差异不能单独归因于方法。'}</p>
      <table class="table comparison-dimensions"><thead><tr><th>对照条件</th><th>核对结果</th></tr></thead><tbody>
      ${Object.entries(names).map(([key,name])=>`<tr><td>${name}</td><td>${states[value.dimensions?.[key]?.status]||'未记录'}</td></tr>`).join('')}</tbody></table>
      <details><summary>方法内容差异 · ${(value.changed_files||[]).length} 个文件</summary>
      ${(value.changed_files||[]).map(file=>`<h4>${escape(file.path)}</h4><pre class="method-diff">${escape(file.diff)}</pre>`).join('')||'<p>没有已知文件差异；缺少方法快照时不能据此认定相同。</p>'}</details>
      <table class="table"><thead><tr><th>股票</th><th>${label(value.left)}</th><th>${label(value.right)}</th></tr></thead><tbody>
      ${(value.stocks||[]).map(row=>`<tr><td>${escape(row.symbol)}</td><td><b>${direction[row.left]||escape(row.left)}</b><p>${escape(reason(row.left_reason))}</p></td><td><b>${direction[row.right]||escape(row.right)}</b><p>${escape(reason(row.right_reason))}</p></td></tr>`).join('')||'<tr><td colspan="3">两份记录均无可比较的股票结果。</td></tr>'}</tbody></table>
      <h4>收盘后模拟回放</h4><p>${label(value.left)}：${outcome(value.outcomes?.left)}<br>${label(value.right)}：${outcome(value.outcomes?.right)}</p>
      <p class="muted">方向对错与扣费盈亏分开评价；练习、历史回放与前瞻成绩不混合。</p>`;
  }
};

async function showRunComparison(left,right,target) {
  const token=Symbol('comparison');target.comparisonRequest=token;
  target.innerHTML='<p role="status">正在核对两份冻结记录…</p>';
  try {
    const value=await api('compare_research_runs',left,right);
    if(target.comparisonRequest===token)target.innerHTML=SHAQComparison.html(value);
  } catch(error) {
    if(target.comparisonRequest===token)target.innerHTML=`<p role="alert">无法比较：${esc(error.message)}</p>`;
  }
}

function addHistoryComparisonControls() {
  const table=q('#history .result-table');if(!table)return;
  const bar=document.createElement('div');bar.className='comparison-toolbar';
  bar.innerHTML='<span id="comparison-selection-count"></span><button id="compare-selected" class="secondary">比较选中版本</button><button id="clear-comparison" class="text-button">清除选择</button>';
  table.before(bar);
  const update=()=>{
    q('#comparison-selection-count').textContent=`已选 ${SHAQComparison.selections.size} 条记录`;
    q('#compare-selected').disabled=SHAQComparison.selections.size!==2;
  };
  const inputs=[];
  const addChoice=(item,insert)=>{
    const key=JSON.stringify(item),input=document.createElement('input');
    input.type='checkbox';input.className='compare-record';
    input.dataset.comparisonKey=key;
    input.setAttribute('aria-label',`选择 ${item.batch_id} ${item.variant_key} 进行比较`);
    input.checked=SHAQComparison.selections.has(key);
    input.onclick=event=>event.stopPropagation();
    input.onchange=()=>{
      if(input.checked)SHAQComparison.selections.set(key,item);
      else SHAQComparison.selections.delete(key);
      inputs.forEach(other=>{other.checked=SHAQComparison.selections.has(other.dataset.comparisonKey);});
      update();
    };
    inputs.push(input);insert(input);
  };
  for(const row of qa('#history .result-table tr[data-batch]')) {
    addChoice({batch_id:row.dataset.batch,variant_key:row.dataset.variantKey},input=>row.children[0].prepend(input));
  }
  for(const button of qa('#history [data-repeat-batch]')) {
    addChoice({batch_id:button.dataset.repeatBatch,variant_key:button.dataset.key},input=>button.before(input));
  }
  q('#clear-comparison').onclick=()=>{SHAQComparison.selections.clear();renderHistory();};
  q('#compare-selected').onclick=()=>{
    const selected=[...SHAQComparison.selections.values()];if(selected.length!==2)return;
    q('#comparison-modal').showModal();
    showRunComparison(selected[0],selected[1],q('#comparison-detail'));
  };
  update();
}
