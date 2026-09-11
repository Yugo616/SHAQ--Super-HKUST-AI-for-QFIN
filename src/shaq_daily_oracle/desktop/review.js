// Display-only summaries come from verified records; never feed a prediction task.
const candidateBeforeReview = window.showCandidate;
window.showCandidate = function(batchId, key, symbol) {
  candidateBeforeReview(batchId, key, symbol);
  const summary = state.selectedBatch?.replay_summaries?.[key]?.[symbol];
  if (!summary) return;
  const section = document.createElement('section');
  section.className = 'aftermarket';
  section.innerHTML = `<h3>简短复盘 · 盘后查看</h3><p>${esc(summary.explanation)}</p>
    ${summary.return_pct == null ? '' : `<p>开盘至收盘 ${Number(summary.return_pct).toFixed(2)}% · ${summary.correct == null ? '未计方向成绩' : summary.correct ? '预测正确' : '预测错误'}。扣费盈亏另见虚拟账户。</p>`}
    ${(summary.basis || []).map(r => `<details><summary>当时依据 · ${esc(moduleName(r.domain))}</summary><p>${esc(r.thesis)}</p><p>反向考虑：${esc(r.antithesis)}</p><small>引用：${esc((r.evidence_ids || []).join('、'))}</small></details>`).join('')}`;
  q('#candidate-analysis .aftermarket').after(section);
};

// Price results count as soon as the first complete post-close bar is saved.
// A later matching provider read only upgrades the wording to reviewed.
const showCandidateWithImmediateResults=window.showCandidate;
window.showCandidate=function(batchId,key,symbol){
  showCandidateWithImmediateResults(batchId,key,symbol);
  const batch=state.selectedBatch,label=batch?.labels?.labels?.[symbol]||{};
  if(!['provisional','final'].includes(label.status))return;
  const prediction=batch?.variants?.[key]?.predictions?.find(row=>row.symbol===symbol);
  const panel=q('#candidate-analysis .aftermarket p');
  if(!panel)return;
  const pnl=prediction ? (prediction.direction==='bearish'
    ? Number(label.official_unadjusted_open)-Number(label.official_unadjusted_close)
    : Number(label.official_unadjusted_close)-Number(label.official_unadjusted_open)) : null;
  const phase=label.status==='final'?'已复核':'初步';
  panel.textContent=`${phase}：官方未复权开盘 $${Number(label.official_unadjusted_open).toFixed(2)}，收盘 $${Number(label.official_unadjusted_close).toFixed(2)}；实际${dir(label.actual_direction)}。${prediction?`一股方向回放 ${money(pnl)}。`:''}`;
};

const batchBeforeReview = renderBatch;
renderBatch = function(batch, key, symbol) {
  batchBeforeReview(batch, key, symbol);
  const selected = key && batch.variants?.[key] ? key : Object.keys(batch.variants || {})[0];
  const selector = q('#compare-version');
  if (!selector) return;
  const previous = selector.onchange;
  selector.onchange = async () => {
    previous?.();
    const other = selector.value, comparison = batch.version_comparisons?.[selected]?.[other];
    if (!comparison) return;
    const left = batch.variants[selected], right = batch.variants[other];
    const leftMeta = SHAQAccounts.methodMeta(selected, state.data?.versions || []);
    const rightMeta = SHAQAccounts.methodMeta(other, state.data?.versions || []);
    let installed = null;
    let installedUnavailable = false;
    try {
      const [leftAuthor, leftVersion] = selected.split('/', 2);
      const [rightAuthor, rightVersion] = other.split('/', 2);
      installed = await api('compare_lab_methods',
        {author:leftAuthor, version_id:leftVersion},
        {author:rightAuthor, version_id:rightVersion});
    } catch (error) {
      if (/^Skill version is not installed: [A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(error?.message || '')) {
        installedUnavailable = true;
      } else {
        notice(`安装包比较失败：${error?.message || '未知错误'}`, true);
        throw error;
      }
    }
    const pick = (v, symbol) => {
      const p = v.predictions?.find(p => p.symbol === symbol);
      const reason = v.integration_audit?.[symbol];
      return `${p ? dir(p.direction) : '未发布'} · ${reason?.decision_reason || (reason?.rejection_reasons || []).join('；') || '见该股票分析'}`;
    };
    const box = document.createElement('section');
    const account = key => batch.virtual_accounts?.results?.find(row => row.variant_key === key);
    const outcome = key => { const row=account(key); return row ? `${SHAQAccounts.statusName(row.status)} · 净盈亏 ${SHAQAccounts.usd(row.net_pnl)} · 手续费 ${SHAQAccounts.usd(row.fees)} · 滑点 ${SHAQAccounts.usd(row.slippage_cost)}` : '尚无分钟回放'; };
    box.innerHTML = `<h3>两版具体差在哪里</h3><p>${esc(leftMeta.method_name)} <span class="method-badge">${esc(leftMeta.status_badge)}</span> ↔ ${esc(rightMeta.method_name)} <span class="method-badge">${esc(rightMeta.status_badge)}</span></p>
      <p>修改模块：${esc(comparison.changed_modules.map(moduleName).join('、') || '方法相同')}。${installed ? `安装包实际差异 ${installed.changed_file_count} 个文件；决策模式 ${esc(installed.decision_mode.left)} / ${esc(installed.decision_mode.right)}。` : installedUnavailable ? '实际安装包比较不可用：历史版本未安装；以下只显示该批次冻结记录。' : ''}</p>
      ${installed ? `<details><summary>安装包变化路径</summary><p>${installed.changed_paths.map(esc).join('<br>')}</p></details>` : ''}
      <p>${esc(leftMeta.method_name)}：${esc(left.variant?.description || '原版方法基准')}<br>${esc(rightMeta.method_name)}：${esc(right.variant?.description || '见方法差异')}</p>
      <p>同一批冻结数据；${comparison.same_model ? '相同模型配置。' : '模型配置不同，不能只把结果差异归因于方法。'}</p>
      <table class="table"><thead><tr><th>股票</th><th>当前版本最终决定</th><th>对照版本最终决定</th></tr></thead><tbody>${comparison.stocks.map(r => `<tr><td>${esc(r.symbol)}</td><td>${esc(pick(left, r.symbol))}</td><td>${esc(pick(right, r.symbol))}</td></tr>`).join('')}</tbody></table>
      <table class="table"><thead><tr><th>同日收盘后模拟回放</th><th>状态与结果</th></tr></thead><tbody><tr><td>${esc(leftMeta.method_name)}</td><td>${esc(outcome(selected))}</td></tr><tr><td>${esc(rightMeta.method_name)}</td><td>${esc(outcome(other))}</td></tr></tbody></table>
      <p>官方 O→C 正确／错误和扣费净盈亏分开显示；初步完整结果立即计分，后续读取负责复核或修订。</p>`;
    q('#version-comparison').prepend(box);
  };
};

if(typeof renderHistory!=='undefined'){
const renderHistoryWithImmediateResults=renderHistory;
renderHistory=function(){
  renderHistoryWithImmediateResults();
  const allRows=state.data.dashboard.daily_results||[];
  const rows=allRows.filter(row=>(!wb.filters.from||row.trade_date>=wb.filters.from)&&
    (!wb.filters.to||row.trade_date<=wb.filters.to)&&
    (!wb.filters.version||historyIdentity(row).filter_key===wb.filters.version)&&
    (!wb.filters.model||(row.model||'未记录模型')===wb.filters.model));
  const accountRows=state.data.dashboard.virtual_accounts?.results||[];
  const header=q('.result-table thead tr');
  if(header)header.innerHTML='<th>日期</th><th>版本</th><th>最终结果</th><th>正确 / 错误</th><th>账户当日净盈亏</th><th>账户累计净盈亏</th><th>账户余额</th><th>状态 / 范围</th>';
  for(const tr of qa('.result-table tr[data-batch]')){
    const row=rows.find(item=>item.batch_id===tr.dataset.batch&&item.variant_key===tr.dataset.variantKey);
    const account=accountRows.find(item=>item.batch_id===tr.dataset.batch&&item.variant_key===tr.dataset.variantKey);
    if(row&&['provisional','final'].includes(row.status)&&tr.children[3]){
      tr.children[3].textContent=`${row.correct} / ${row.incorrect}`;
    }
    if(row&&tr.children[6]){
      tr.children[4].textContent=SHAQAccounts.usd(account?.net_pnl);
      tr.children[5].textContent=SHAQAccounts.usd(account?.account_cumulative_net_pnl);
      const balance=document.createElement('td');
      balance.textContent=SHAQAccounts.usd(account?.account_balance);
      tr.insertBefore(balance,tr.children[6]);
      const statusCell=tr.children[7];
      let accountStatus=statusCell.querySelector('.account-result-status');
      if(!accountStatus){accountStatus=document.createElement('small');accountStatus.className='account-result-status';statusCell.appendChild(accountStatus)}
      const scope=!account?'':account.scope==='historical'
        ? `${SHAQAccounts.scopeName(account.scope)} · 不进入前瞻账户`
        : SHAQAccounts.scopeName(account.scope);
      accountStatus.textContent=`方向状态：${replayStatus(row.status)} · 账户状态：${account?SHAQAccounts.statusName(account.status):'尚无账户回放'}${scope?` · 范围：${scope}`:''}`;
    }
  }
  const table=q('.result-table');
  if(table&&rows.length)table.insertAdjacentHTML('afterend',`<details class="one-share-reference"><summary>一股零成本参考（不计入账户）</summary>${rows.map(row=>`<p>${esc(row.trade_date)} · ${esc(historyMethod(row))} · 当日 ${money(row.daily_pnl)} · 累计 ${money(row.cumulative_pnl)}</p>`).join('')}</details>`);
  const scored=rows.filter(row=>row.score_eligible!==false&&['provisional','final'].includes(row.status));
  const good=scored.reduce((sum,row)=>sum+Number(row.correct||0),0);
  const bad=scored.reduce((sum,row)=>sum+Number(row.incorrect||0),0);
  const empty=rows.filter(row=>row.score_eligible!==false&&row.status==='empty').length;
  const summary=q('#history .result-summary');
  if(summary)summary.firstChild.textContent=`已有结果 ${good+bad} 次 · 正确 ${good} / 错误 ${bad} · 命中率 ${good+bad?(100*good/(good+bad)).toFixed(1)+'%':'—'} · 空榜 ${empty} 次`;
};
}
