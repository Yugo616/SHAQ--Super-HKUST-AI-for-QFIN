/* Read-only after-close views. No broker, network, model call, or account engine lives here. */
const SHAQAccounts = (() => {
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
  const usd = value => value == null ? '—' : '$' + Number(value).toLocaleString(
    'en-US', {minimumFractionDigits:2, maximumFractionDigits:2}
  );
  const scopeName = scope => ({
    forward:'持续账户', historical:'历史回放 · 规则在预测后配置',
    practice:'练习回放 · 不计前瞻成绩', late:'迟到回放 · 不计前瞻成绩',
    duplicate:'重复运行 · 不重复入账',
  }[scope] || scope || '未标明范围');
  const statusName = status => ({
    settled:'旧版已保存结算', saved_only:'saved-only · 只读',
    empty:'空榜 · 0 笔交易', pending:'等待收盘后的分钟资料',
    provisional:'初步 · 已计入', final:'已复核',
    unavailable:'资料不可用 · 尚未确认', incomplete:'未完成 · 退出分钟缺失',
    blocked_previous:'等待前一交易日未完成回放', error:'回放失败',
    duplicate:'重复记录 · 不重复入账',
  }[status] || status || '状态未知');

  function canonicalKey(value) {
    if (!value) return '';
    if (value.variant_key) return value.variant_key;
    if (value.series_key) return String(value.series_key).split(':')[0];
    return value.author && value.version_id ? `${value.author}/${value.version_id}` : '';
  }

  function methodMeta(value, versions) {
    const key = typeof value === 'string' ? value : canonicalKey(value);
    const [author='team', versionId=''] = key.split('/', 2);
    const row = (versions || []).find(version => {
      const rowAuthor = String(version.author || 'team');
      return (version.history_keys || []).includes(key) || (rowAuthor === author && (String(version.version_id) === versionId ||
        (version.aliases || []).map(String).includes(versionId)));
    });
    return row ? {
      author: String(row.author || 'team'), version_id: String(row.version_id),
      method_name: String(row.method_name || row.label || row.version_id),
      status_badge: String(row.status_badge || '本地版本'), installed: true,
    } : {
      author, version_id: versionId,
      method_name: String(value?.method_name || value?.label || key || '未标明方法'),
      status_badge: String(value?.status_badge || '历史记录'), installed: false,
    };
  }

  function historyIdentity(value, versions) {
    const meta = methodMeta(value, versions);
    const rawKey = typeof value === 'string' ? value : canonicalKey(value);
    const linked=(versions||[]).some(v=>(v.history_keys||[]).includes(rawKey));
    return {
      ...meta,
      filter_key: `${meta.author}/${meta.version_id}`,
      series_key: String(value?.series_key || rawKey || `${meta.author}/${meta.version_id}`),
      comparison_key: linked ? `${meta.author}/${meta.version_id}:${value?.model_identity||value?.model||''}` : String(value?.series_key||rawKey),
    };
  }

  function normalizeSelections(versions, selections) {
    const normalized = [], seen = new Set();
    for (const selected of selections || []) {
      const author = String(selected.author || 'team').toLowerCase();
      const versionId = String(selected.version_id || '');
      const row = (versions || []).find(version =>
        String(version.author || 'team').toLowerCase() === author &&
        (String(version.version_id) === versionId || (version.aliases || []).map(String).includes(versionId))
      );
      if (!row) continue;
      const value = {author:String(row.author || 'team'), version_id:String(row.version_id)};
      const key = `${value.author}/${value.version_id}`;
      if (!seen.has(key)) { seen.add(key); normalized.push(value); }
    }
    return normalized;
  }

  const badge = meta => `<span class="method-badge">${e(meta.status_badge)}</span>`;
  const method = (value, versions) => {
    const meta = methodMeta(value, versions);
    return `<b>${e(meta.method_name)}</b> ${badge(meta)}`;
  };
  const localTime = value => value ? e(String(value).replace('T', ' ').slice(0, 22)) : '—';

  function rulesText(r) {
    return r ? `${r.risk_fraction == null ? `旧规则每票最多 ${usd(r.per_prediction_budget)}` : `实验风险定仓（非最优性保证）：期初模拟余额 × ${(Number(r.risk_fraction)*100).toFixed(2)}% ÷ 各股票冻结波动率；单票上限 ${(Number(r.per_symbol_cap)*100).toFixed(0)}%，总开仓上限 ${(Number(r.gross_cap)*100).toFixed(0)}%，回看 ${e(r.lookback)} 个有效交易日`} · 09:31 ET 分钟开盘参考进入 · 收盘前5分钟开盘参考退出 · 单边手续费 ${(Number(r.commission_rate)*100).toFixed(2)}% · 单边不利滑点 ${(Number(r.slippage_rate)*100).toFixed(2)}%`
      : '分钟回放账户尚未启用';
  }

  function officialOutcome(trade) {
    if (trade.official_open == null || trade.official_close == null) {
      return '官方 O→C 方向成绩：—';
    }
    const actual=Number(trade.official_open_to_close_return ?? (Number(trade.official_close)/Number(trade.official_open)-1))*100;
    const adjusted=Number(trade.direction_adjusted_return ?? (trade.direction==='bearish'?-actual/100:actual/100))*100;
    return `官方 O→C ${usd(trade.official_open)} → ${usd(trade.official_close)} · 实际 O→C ${actual.toFixed(2)}% · 方向调整 ${adjusted.toFixed(2)}% · 方向成绩 ${trade.direction_correct ? '正确' : '错误'}`;
  }

  function tradeRows(trades, day) {
    const names = {unavailable_entry:'开仓分钟缺失 · 未模拟成交',closed:'已平仓',
      open_incomplete:'退出分钟缺失 · 模拟持仓未平',unfilled_budget:'预算不足 · 未模拟成交',
      unfilled_volume:'目标分钟成交量不足 · 未模拟成交'};
    return (trades || []).map(trade => {const missing = trade.status === 'unavailable_entry';return `<tr>
      <td>${e(trade.symbol)}</td>
      <td>${trade.direction === 'bearish' ? '卖空 → 买回' : '买入 → 卖出'}<br><small>${missing ? '' : `${e(trade.quantity)} 股整数数量 · `}${e(names[trade.status] || trade.status || '')}</small></td>
      <td>进入参考价 ${usd(trade.entry_reference_open)}<br><small>${localTime(trade.entry_reference_at_et || day.entry_reference_at_et)}</small><br>退出参考价 ${usd(trade.exit_reference_open)}<br><small>${localTime(trade.exit_reference_at_et || day.exit_reference_at_et)}</small></td>
      <td>进入模拟成交价 ${usd(trade.entry_price)}<br>退出模拟成交价 ${usd(trade.exit_price)}</td>
      <td>${e(officialOutcome(trade))}</td>
      <td>手续费 ${usd(missing ? null : trade.fees)}<br>滑点影响 ${usd(missing ? null : trade.slippage_cost)}<br>净盈亏 ${usd(missing ? null : trade.net_pnl)}</td>
    </tr>`}).join('');
  }

  const noEntryData = row => row?.status === 'unavailable' && !(row.trades || []).some(trade => trade.quantity > 0);
  const amount = (row, key) => usd(noEntryData(row) ? null : row?.[key]);

  function dayHtml(row) {
    if (!row) return '<p>尚无账户回放。</p>';
    const intro = `<div class="section-head"><div><h3>收盘后模拟回放</h3><p>${e(scopeName(row.scope))}</p></div><span class="status ${row.status === 'final' || row.status === 'empty' ? 'ok' : row.status === 'error' || row.status === 'incomplete' ? 'bad' : ''}">${e(statusName(row.status))}</span></div>
      ${row.rules ? `<p>${rulesText(row.rules)}</p>` : ''}
      <p class="account-note">只使用冻结预测做券商无关的分钟回放；不是盘中实际提交的订单。官方 O→C 方向成绩与交易净盈亏分别计算。</p>`;
    const stateNotes = {
      pending:'尚未生成模拟成交；预测、六领域分析与决策记录保持不变。',
      blocked_previous:'同一账户前一日仍未完成，因此本日未继续累计。',
      error:row.error || '分钟回放失败；没有写入最终账户净值。',
    };
    if (stateNotes[row.status]) return intro + `<p>${e(stateNotes[row.status])}</p>`;
    const unavailable = row.status === 'unavailable'
      ? `<p class="provisional-note">${(row.trades || []).some(trade => trade.quantity > 0) ? '部分目标分钟资料不可用；已有模拟成交与未成交逐股列示。' : '目标分钟资料不可用；没有模拟成交。'}没有替代其他分钟或日线价格；尚未计入持续账户净值，同一账户后续交易日等待本日解决。</p>` : '';
    const refresh = row.latest_refresh && row.latest_refresh.status !== 'available'
      ? `<p class="account-note">最近刷新 ${localTime(row.latest_refresh.captured_at_et)}：目标分钟不可用 ${e(JSON.stringify(row.latest_refresh.missing_targets || {}))}。保留此前目标分钟证据；本次缺失不构成新的独立确认。原始读取记录 ${e(row.latest_refresh.observation_sha256 || '')}</p>` : '';
    const provisional = row.status === 'provisional'
      ? row.scope === 'historical'
        ? '<p class="provisional-note">初步模拟结算仅计入历史回放余额，不进入持续账户净值；稍后真实读取到相同目标分钟后标记为已复核。</p>'
        : '<p class="provisional-note">初步模拟结算已计入持续账户净值；稍后真实读取到相同目标分钟后标记为已复核。</p>' : '';
    const incomplete = row.status === 'incomplete'
      ? `<p class="incomplete-note">未完成：退出分钟缺失，仍有模拟持仓 ${e(JSON.stringify(row.closing_positions || {}))}；不伪造退出成交或最终盈亏。</p>` : '';
    const summary = row.status === 'empty' ? '<p>本日空榜，没有模拟交易或成本。</p>' :
      `<p>${row.status === 'unavailable' && !noEntryData(row) ? '已有模拟成交部分（非整日最终结算）：' : ''}零成本盈亏 ${amount(row,'gross_pnl')} − 手续费 ${amount(row,'fees')} − 滑点影响 ${amount(row,'slippage_cost')} = 净盈亏 <b>${amount(row,'net_pnl')}</b>${['provisional','final'].includes(row.status) ? ` · 期初 ${usd(row.opening_cash)} · 期末 ${usd(row.closing_cash)}` : ''}</p>`;
    const rows = tradeRows(row.trades, row);
    return intro + unavailable + refresh + provisional + incomplete + summary + (rows ? `
      <div class="account-scroll"><table class="table trade-detail"><thead><tr><th>股票</th><th>方向 / 数量</th><th>分钟参考</th><th>Zipline 模拟成交</th><th>官方 O→C 方向成绩</th><th>成本 / 净盈亏</th></tr></thead><tbody>${rows}</tbody></table></div>` : '') + `
      <p><small>资料读取 ${localTime(row.captured_at_et)} · 引擎处理开始 ${localTime(row.processing_started_at_et)} · 完成 ${localTime(row.processed_at_et)}</small></p>
      <details><summary>Zipline 模拟订单记录</summary>${(row.orders || []).map(order => `<p>${e(order.symbol)} · ${order.phase === 'open' ? '开仓' : '平仓'} · 参考分钟 ${localTime(order.reference_at_et)} · 引擎处理 ${localTime(order.executed_at_et)} · ${e(order.size)} 股 · ${usd(order.price)} · ${e(order.status)}</p>`).join('') || '<p>无模拟订单</p>'}</details>`;
  }

  function plot(accounts) {
    const colors=['#285ba8','#35846c','#ad7833','#89559c','#737c88'];
    const points=accounts.flatMap(account => account.curve || []);
    const dates=[...new Set(points.map(point => point.date))].sort();
    if (!points.length) return '<p>收盘净值：首个初步持续账户交易日后显示曲线。</p>';
    let lo=Math.min(...points.map(point => point.equity));
    let hi=Math.max(...points.map(point => point.equity));
    if (hi===lo) {hi+=1;lo-=1;}
    const x=point=>dates.length===1?462.5:85+dates.indexOf(point.date)/(dates.length-1)*755;
    const y=point=>150-(point.equity-lo)/(hi-lo)*125;
    const axis=dates.length===1?`<text x="462.5" y="185" text-anchor="middle">${e(dates[0])}</text>`:`<text x="85" y="185">${e(dates[0])}</text><text x="840" y="185" text-anchor="end">${e(dates.at(-1))}</text>`;
    const legend=`<div class="balance-legend">${accounts.map((account,index)=>`<span><i style="background:${colors[index%colors.length]}"></i>${e(account.method_name||account.label||'未标明版本')}${account.model?` · ${e(account.model)}`:''}</span>`).join('')}</div>`;
    return `<h3>收盘净值</h3>${legend}<svg class="pnl-chart" viewBox="0 0 900 220" role="img" aria-label="各账户收盘净值；纵轴余额（USD），横轴日期（交易日）"><text x="0" y="12" font-size="12">余额（USD）</text><text x="0" y="30">${usd(hi)}</text><text x="0" y="150">${usd(lo)}</text>${accounts.map((account,index)=>`<polyline fill="none" stroke="${colors[index%colors.length]}" stroke-width="2" points="${(account.curve||[]).map(point=>`${x(point)},${y(point)}`).join(' ')}"/>${(account.curve||[]).map(point=>`<circle cx="${x(point)}" cy="${y(point)}" r="3" fill="${colors[index%colors.length]}"><title>${e(account.method_name || account.label)} ${e(point.date)} ${usd(point.equity)}</title></circle>`).join('')}`).join('')}${axis}<text x="450" y="211" text-anchor="middle" font-size="12">日期（交易日）</text></svg>`;
  }

  function resultRows(rows, versions) {
    return (rows || []).map(row => {const trades=row.trades||[],score=row.status==='empty'?'0 / 0':trades.length&&trades.every(trade=>typeof trade.direction_correct==='boolean')?`${trades.filter(trade=>trade.direction_correct).length} / ${trades.filter(trade=>!trade.direction_correct).length}`:'—';return `<tr class="${row.batch_id ? 'clickable' : ''}" ${row.batch_id ? `data-account-batch="${e(row.batch_id)}" data-account-key="${e(row.variant_key)}"` : ''}>
      <td>${e(row.trade_date)}<br>${method(row, versions)}</td><td>${e(row.source_scope === 'simulation_cumulative' ? '模拟累计 · 来源历史已标记' : scopeName(row.scope))}</td>
      <td>${e(statusName(row.status))}</td><td>${score}</td>
      <td>${amount(row,'net_pnl')}</td><td>${usd(row.account_cumulative_net_pnl)}</td><td>${usd(row.account_balance)}</td>
      <td>${amount(row,'gross_pnl')}</td><td>${amount(row,'fees')}</td><td>${amount(row,'slippage_cost')}</td></tr>`}).join('');
  }

  function overviewHtml(data, versions, filters={}) {
    const matches = row => {
      const identity = historyIdentity(row, versions);
      return (!filters.version || identity.filter_key === filters.version) &&
        (!filters.model || row.model === filters.model) &&
        (!filters.from || !row.trade_date || row.trade_date >= filters.from) &&
        (!filters.to || !row.trade_date || row.trade_date <= filters.to);
    };
    const accounts=(data.accounts || []).filter(matches).map(account => ({
      ...account, method_name:methodMeta(account, versions).method_name,
      curve:(account.curve || []).filter(point => (!filters.from || point.date >= filters.from) &&
        (!filters.to || point.date <= filters.to)),
    }));
    const forward=(data.results || []).filter(row => row.scope === 'forward' && matches(row)).slice().reverse();
    const research=(data.results || []).filter(row => row.scope !== 'forward' && row.scope !== 'duplicate' && matches(row)).slice().reverse();
    const legacy=data.legacy || {status:'unavailable',results:[]};
    const accountRows=accounts.map(account => {
      const rules=account.rules || data.rules || {};
      const engine=account.engine === 'zipline-reloaded' ? 'Zipline-reloaded' : account.engine;
      return `<tr><td>${method(account, versions)}<br><small>${e(account.model)} · ${e(engine)} ${e(account.engine_version)}</small></td>
        <td>${usd(account.opening_simulation_balance ?? rules.initial_cash)}</td><td>${usd(account.equity)}</td><td>${usd(Number(account.equity)-Number(account.opening_simulation_balance ?? rules.initial_cash))}</td>
        <td>${usd(Number(account.gross_equity)-Number(rules.initial_cash))}</td><td>${usd(account.fees)}</td>
        <td>${usd(account.slippage_cost)}</td><td>${account.max_drawdown == null ? '—' : (Number(account.max_drawdown)*100).toFixed(2)+'%'}</td></tr>`;
    }).join('');
    const legacyRows=(legacy.results || []).map(row => `<tr><td>${e(row.trade_date || '—')}</td><td>${e(row.engine === 'backtrader' ? 'Backtrader' : row.engine || '旧引擎')}</td><td>${e(row.status || 'saved-only')}</td><td>${usd(row.net_pnl)}</td></tr>`).join('');
    return `<h2>独立分钟回放账户</h2><p>${rulesText(data.rules)}</p>
      <p class="account-note">收盘后模拟回放。方法、模型、引擎和规则各自绑定账户；初步完整结算立即计入，后续读取仅复核或修订。卖空仅作可借券研究假设。</p>${plot(accounts)}
      <h3>持续账户</h3><div class="account-scroll"><table class="table"><thead><tr><th>方法 / 模型 / 引擎</th><th>期初模拟余额</th><th>当前余额</th><th>净盈亏</th><th>零成本对照</th><th>手续费</th><th>滑点影响</th><th>最大收盘回撤</th></tr></thead><tbody>${accountRows || '<tr><td colspan="8">等待首个合格的持续账户结果。</td></tr>'}</tbody></table></div>
      <h3>持续账户每日记录</h3><div class="account-scroll"><table class="table"><thead><tr><th>日期 / 方法</th><th>范围</th><th>状态</th><th>正确 / 错误</th><th>当日净盈亏</th><th>累计净盈亏</th><th>账户余额</th><th>零成本盈亏</th><th>手续费</th><th>滑点影响</th></tr></thead><tbody>${resultRows(forward, versions) || '<tr><td colspan="10">尚无持续账户记录。</td></tr>'}</tbody></table></div>
      <details class="account-secondary"><summary>历史来源 / 练习（前瞻统计分开）</summary><div class="account-scroll"><table class="table"><thead><tr><th>日期 / 方法</th><th>范围</th><th>状态</th><th>正确 / 错误</th><th>当日净盈亏</th><th>累计净盈亏</th><th>账户余额</th><th>零成本盈亏</th><th>手续费</th><th>滑点影响</th></tr></thead><tbody>${resultRows(research, versions) || '<tr><td colspan="10">没有历史或练习回放。</td></tr>'}</tbody></table></div></details>
      <details class="account-secondary"><summary>旧版已保存结果（只读） · ${e(legacy.status === 'saved_only' ? 'saved-only' : legacy.status || 'unavailable')}</summary><p>旧引擎记录仅显示已保存文件，不重算、不并入 Zipline 分钟账户。</p><div class="account-scroll"><table class="table"><thead><tr><th>日期</th><th>旧引擎</th><th>保存状态</th><th>净盈亏</th></tr></thead><tbody>${legacyRows || '<tr><td colspan="4">没有可核验的旧版已保存结果。</td></tr>'}</tbody></table></div></details>`;
  }

  function compactOverviewHtml(data,versions=[],filters={}){
    const accounts=(data.accounts||[]).filter(account=>(!filters.version||historyIdentity(account,versions).filter_key===filters.version)&&(!filters.model||account.model===filters.model)).map(account=>({...account,method_name:methodMeta(account,versions).method_name,curve:(account.curve||[]).filter(point=>(!filters.from||point.date>=filters.from)&&(!filters.to||point.date<=filters.to))}));
    const cards=accounts.map(account=>{
      const rows=(data.results||[]).filter(row=>row.scope==='forward'&&(account.series_key?row.series_key===account.series_key:row.variant_key===account.variant_key)).sort((a,b)=>String(b.trade_date).localeCompare(String(a.trade_date)));
      const latest=rows[0];
      return `<article class="balance-card" data-view-key="${e(account.series_key||account.variant_key||account.label)}"><h3>${method(account,versions)}</h3><small>${e(account.model||'未记录模型')}</small><p>当前余额 <b>${usd(account.equity)}</b></p><p>${e(latest?.trade_date||'当日')} 净盈亏 <b>${amount(latest,'net_pnl')}</b></p></article>`;
    }).join('');
    return `<h2>版本余额</h2><div class="balance-cards">${cards||'<p>尚无可用余额。</p>'}</div>${plot(accounts)}`;
  }

  return {dayHtml, overviewHtml, compactOverviewHtml, plot, rulesText, usd, scopeName, statusName,
    methodMeta, historyIdentity, normalizeSelections, escape:e};
})();

// The compact desktop does not mount the legacy detailed-account renderers.
