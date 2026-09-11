// Display-only: trading timestamps, predictions and account records stay unchanged.
const SHAQProgress = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const active = job => ['queued', 'running'].includes(job.status);
  function etDay(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(date);
    const get = name => parts.find(p => p.type === name).value;
    return `${get('year')}-${get('month')}-${get('day')}`;
  }
  function currentJobs(jobs, now) {
    const day = etDay(now);
    return jobs.filter(job => active(job) || (day && etDay(job.started_at_et) === day))
      .sort((a,b) => Number(active(b))-Number(active(a)) || (Date.parse(b.started_at_et)||0)-(Date.parse(a.started_at_et)||0));
  }
  function retryVersions(job) {
    return Object.entries(job.variant_progress || {}).filter(([,status]) =>
      status === 'failed' || (job.status === 'failed' && status !== 'complete'))
      .map(([key]) => {const [author, ...rest] = key.split('/'); return {author,version_id:rest.join('/')};});
  }
  function scheduleText(value) {
    return value.enabled ? `自动运行已开启 · 美东 ${String(value.start_et || '').slice(0,5)}` : '自动运行未开启 · 当前不会每天自动跑';
  }
  function progressHtml(jobs, versions, now) {
    const rows = currentJobs(jobs, now);
    if (!rows.length) return '<p class="progress-empty">今天还没有启动分析。历史记录请到「查看结果」。</p>';
    const names = key => {
      const [author, id] = key.split('/');
      const item = versions.find(v => v.author === author && (v.version_id === id || (v.aliases || []).includes(id)));
      return item?.method_name || item?.label || key;
    };
    const status = value => ({queued:'等待开始',running:'分析中',complete:'已完成',partial_failure:'部分失败',failed:'失败'}[value] || value);
    return rows.map(job => {
      const date = job.started_at_et ? new Date(job.started_at_et) : null;
      const time = date && Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN',{hour12:false}) : '等待启动';
      const carry = active(job) && etDay(job.started_at_et) && etDay(job.started_at_et) !== etDay(now) ? ' · 前一日未完成任务' : '';
      const versionsHtml = job.status === 'complete' ? '' : `<ul class="progress-versions">${Object.entries(job.variant_progress || {}).map(([key,value]) => `<li><span>${esc(names(key))}</span><span>${esc(status(value))}</span></li>`).join('')}</ul>`;
      return `<article class="progress-batch" data-progress-job="${esc(job.job_id)}"><header><span>${esc(time + carry)}</span><b>${esc(status(job.status))}</b></header><p>${esc(job.message || '')}</p>${versionsHtml}<details class="research-progress"><summary>研究执行明细</summary>${researchHtml(job.research_progress||[],[],job.research_selection||{})}</details><div class="progress-actions">${job.batch_id ? `<button class="text-button" data-progress-result="${esc(job.batch_id)}">查看结果</button>` : ''}${retryVersions(job).length ? `<button class="secondary" data-progress-retry="${esc(job.job_id)}">选择失败版本重试</button>` : ''}</div></article>`;
    }).join('');
  }
  function researchHtml(events, legacyReports, selection={}) {
    const rows=Array.isArray(events)?events:[], reports=Array.isArray(legacyReports)?legacyReports:[];
    if(!rows.length){return reports.length?`<section class="research-view"><p>旧记录没有执行时间线；以下为当时保存且已校验的报告。</p>${reports.map(r=>`<details><summary>${esc(r.domain||'研究报告')}</summary><p>${esc(r.thesis||'')}</p></details>`).join('')}</section>`:'<p>尚无研究执行明细。</p>'}
    const variants=[...new Set(rows.map(r=>r.variant_key).filter(Boolean))], chosen=variants.includes(selection.variant)?selection.variant:variants[0];
    const scoped=rows.filter(r=>!r.variant_key||r.variant_key===chosen), symbols=[...new Set(scoped.flatMap(r=>r.symbols||[]).concat(scoped.map(r=>r.symbol)).filter(Boolean))];
    const symbol=symbols.includes(selection.symbol)?selection.symbol:symbols[0], relevant=scoped.filter(r=>!symbol||r.symbol===symbol||(r.symbols||[]).includes(symbol));
    const completed=relevant.filter(r=>['complete','cache_hit','validated'].includes(r.status)||['report_validated','decision_complete'].includes(r.stage)).length;
    const elapsed=relevant.reduce((n,r)=>n+Number(r.elapsed_seconds||0),0);
    const validated=relevant.filter(r=>r.stage==='report_validated'&&r.report);
    const option=(value,selected,attr)=>`<option ${attr}="${esc(value)}"${value===selected?' selected':''}>${esc(value)}</option>`;
    const sections=validated.map(r=>{const report=r.report||{}, id=`domain-${r.domain||'other'}`, open=(selection.open||[]).includes(id)?' open':'';return `<details data-research-section="${esc(id)}"${open}><summary>${esc(r.domain||'研究')} · ${esc(r.status||'validated')}</summary><h4>主要结论</h4><p>${esc(report.thesis||'')}</p><p><b>支持：</b>${esc(report.support||report.thesis||'—')}</p><p><b>反方：</b>${esc(report.antithesis||'—')}</p><p><b>未知：</b>${esc((report.unknowns||[]).join('；')||'无')}</p><p><b>失效条件：</b>${esc((report.invalidation||[]).join('；')||'未列明')}</p><details><summary>原报告与来源细节</summary><pre>${esc(JSON.stringify(report.original||report,null,2))}</pre>${(report.evidence||[]).map(e=>`<p>${esc(e.provider||'未知来源')} · ${esc(e.captured_at||'时间未记录')} · ${esc(e.unit||'单位未记录')}<br><small>${esc(e.source_uri||'')}</small></p>`).join('')}</details></details>`}).join('');
    const calls=relevant.filter(r=>r.stage==='domain_call_start').map(r=>`<li>${esc(r.domain||'模型调用')} · ${esc((r.symbols||[]).join('、'))} · ${esc(r.occurred_at_et||'')}</li>`).join('');
    return `<section class="research-view"><label>版本 <select data-research-variant>${variants.map(v=>option(v,chosen,'data-research-variant-option')).join('')}</select></label><label>候选 <select data-research-symbol>${symbols.map(s=>option(s,symbol,'data-research-symbol')).join('')}</select></label><p>实际任务 ${relevant.length} · 已完成 ${completed} · 实际耗时 ${elapsed.toFixed(1)} 秒</p><details><summary>执行时间线</summary><ol>${calls}</ol></details>${sections||'<p>尚无已校验报告；原始无效输出不会显示为结论。</p>'}</section>`;
  }
  return {etDay,currentJobs,retryVersions,scheduleText,progressHtml,researchHtml};
})();
if (typeof module !== 'undefined') module.exports = SHAQProgress;
