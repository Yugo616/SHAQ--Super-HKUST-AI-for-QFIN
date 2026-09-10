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
      return `<article class="progress-batch"><header><span>${esc(time + carry)}</span><b>${esc(status(job.status))}</b></header><p>${esc(job.message || '')}</p>${versionsHtml}<div class="progress-actions">${job.batch_id ? `<button class="text-button" data-progress-result="${esc(job.batch_id)}">查看结果</button>` : ''}${retryVersions(job).length ? `<button class="secondary" data-progress-retry="${esc(job.job_id)}">选择失败版本重试</button>` : ''}</div></article>`;
    }).join('');
  }
  return {etDay,currentJobs,retryVersions,scheduleText,progressHtml};
})();
if (typeof module !== 'undefined') module.exports = SHAQProgress;
