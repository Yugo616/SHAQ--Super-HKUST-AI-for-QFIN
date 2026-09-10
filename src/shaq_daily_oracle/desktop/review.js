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

const batchBeforeReview = renderBatch;
renderBatch = function(batch, key) {
  batchBeforeReview(batch, key);
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
      <p>官方 O→C 正确／错误和扣费净盈亏分开显示；未核验结果不提前计分。</p>`;
    q('#version-comparison').prepend(box);
  };
};
