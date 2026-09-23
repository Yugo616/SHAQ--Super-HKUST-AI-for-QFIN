// Display-only: trading timestamps, predictions and account records stay unchanged.
const SHAQProgress = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const active = job => ['queued', 'running'].includes(job.status);
  const domainName = value => ({market:'市场环境',relationships:'行业与关系传导',event:'公司催化事件',capital:'买卖压力与流动性',derivatives:'期权定价与仓位',price_volume:'价格走势与参与度',screening:'候选筛选',preparation:'资料准备',adversary:'反方审查',decision:'最终决策'}[value]||value||'研究');
  function callSummary(events,variant){
    const calls=new Map(),attempts=new Set();
    const states={call_requested:'running',model_started:'running',model_returned:'complete',cache_hit:'reused',failure:'failed',validation_failure:'failed',model_attempt_failed:'failed',model_retry_scheduled:'running',model_retry_started:'running'};
    for(const event of events||[]){
      if(event.variant_key!==variant||!event.call_id||!states[event.stage])continue;
      const attempt=Number(event.attempt||1),previous=calls.get(event.call_id);
      attempts.add(event.call_id+':'+attempt);
      if(!previous||attempt>=previous.attempt)calls.set(event.call_id,{attempt,status:states[event.stage]});
    }
    const result={total:calls.size,complete:0,running:0,failed:0,reused:0,attempts:attempts.size};
    for(const call of calls.values())result[call.status]++;
    return result;
  }
  const callText=value=>`实际调用 ${value.total} · 已完成 ${value.complete} · 进行中 ${value.running} · 失败 ${value.failed} · 复用 ${value.reused}${value.attempts>value.total?` · 调用尝试 ${value.attempts}`:''}`;
  function taskProgress(events,variant){
    const rows=(events||[]).filter(row=>row.variant_key===variant);
    const plan=rows.filter(row=>row.stage==='tasks_planned').at(-1);
    const tasks=new Map((plan?.tasks||[]).map(task=>[task.task_id,task]));
    const completed=new Set();
    for(const row of rows){
      const id=row.stage==='report_validated'?`report:${row.symbol}:${row.domain}`:row.stage==='decision_complete'?'decision':['adversary','synthesis'].includes(row.stage)&&row.status==='complete'?row.stage:null;
      if(id&&tasks.has(id))completed.add(id);
    }
    if(rows.some(row=>row.stage==='variant_reused'&&row.status==='complete'))for(const id of tasks.keys())completed.add(id);
    return {total:plan?tasks.size:null,complete:completed.size,tasks:[...tasks.values()],rows};
  }
  function attemptStatus(event, now){
    if(!event)return '等待';
    if(event.stage==='model_retry_scheduled')return `正在自动重试 · 第 ${Number(event.attempt||1)} / ${Number(event.max_attempts||1)} 次尝试${event.next_retry_at?' · 下一次 '+new Date(event.next_retry_at).toLocaleTimeString('zh-CN',{hour12:false}):''}`;
    if(event.status==='failed')return failureText(event);
    const start=Date.parse(event.occurred_at_et),end=Date.parse(now);
    const elapsed=Number.isFinite(start)&&Number.isFinite(end)?`，已用时 ${Math.max(0,Math.floor((end-start)/1000))}秒`:'';
    return `正在分析${elapsed}`;
  }
  function compactResearchHtml(progress, executionState, now){
    const ended=!['queued','running'].includes(executionState);
    const unfinished=executionState==='complete'?'报告未记录':'未完成';
    const reports=new Map(progress.rows.filter(row=>row.stage==='report_validated').map(row=>[`${row.symbol}:${row.domain}`,row]));
    const tasks=progress.tasks.filter(task=>task.symbol&&task.domain);
    if(!tasks.length)for(const row of reports.values())tasks.push({symbol:row.symbol,domain:row.domain});
    const symbols=[...new Set(tasks.map(task=>task.symbol))];
    const html=symbols.map(symbol=>`<section class="progress-symbol" data-view-key="${esc(symbol)}"><h4>${esc(symbol)}</h4><div class="domain-status-list">${tasks.filter(task=>task.symbol===symbol).map(task=>{
      const row=reports.get(`${symbol}:${task.domain}`),report=row?.report;
      const id=`${symbol}:${task.domain}`;
      if(report)return `<details data-view-key="${esc(id)}" data-research-section="${esc(id)}"><summary>${esc(domainName(task.domain))} · ${row.status==='no_data'?'无合格资料':'已完成'}</summary><p><b>主要结论：</b>${esc(report.thesis||'—')}</p><p><b>反方：</b>${esc(report.antithesis||'—')}</p><p><b>未知：</b>${esc((report.unknowns||[]).join('；')||'未列明')}</p><p><b>失效条件：</b>${esc((report.invalidation||[]).join('；')||'未列明')}</p></details>`;
      const latest=progress.rows.filter(event=>event.domain===task.domain&&(event.symbol===symbol||(event.symbols||[]).includes(symbol))).at(-1);
      const clock=!ended&&['model_started','model_retry_started'].includes(latest?.stage)?` data-call-start="${esc(latest.occurred_at_et)}"`:'';
      return `<p>${esc(domainName(task.domain))} · <span${clock}>${esc(ended?unfinished:attemptStatus(latest,now))}</span></p>`;
    }).join('')}</div></section>`).join('');
    return html||'<p>尚无已保存的分析报告。</p>';
  }
  function etDay(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(date);
    const get = name => parts.find(p => p.type === name).value;
    return `${get('year')}-${get('month')}-${get('day')}`;
  }
  function currentJobs(jobs, now) {
    const day = etDay(now);
    const seen = new Set();
    return jobs.filter(job => job.status==='running' ||
      (job.status==='queued' && day && etDay(job.queued_at_et || job.started_at_et)===day) ||
      (day && etDay(job.started_at_et) === day))
      .sort((a,b) => ({running:2,queued:1}[b.status]||0)-({running:2,queued:1}[a.status]||0) ||
        (Date.parse(b.started_at_et||b.queued_at_et)||0)-(Date.parse(a.started_at_et||a.queued_at_et)||0))
      .filter(job=>{const key=job.batch_id||job.job_id;if(seen.has(key))return false;seen.add(key);return true});
  }
  function failureText(failure) {
    const raw=String(failure?.message||failure?.error||'');
    const domain=['derivatives','price_volume','capital','relationships','market','event'].find(name=>raw.includes(name));
    const label=domain?domainName(domain):'分析报告';
    if(/cited evidence outside|unavailable or invented evidence|invalid evidence/i.test(raw))return `${label}引用了不合格或本次任务之外的证据，结果未采用。可继续未完成分析。`;
    if(/timeout|timed out|超时/i.test(raw))return `${label}等待模型超时。已完成的分析已保留，可继续未完成部分。`;
    if(/403|401|Forbidden|Unauthorized/i.test(raw))return '模型服务拒绝访问，请检查登录或连接设置后继续。';
    if(/429|rate.?limit/i.test(raw))return '模型服务暂时限流，请稍后继续未完成分析。';
    if(/database.*locked|SQLITE_BUSY/i.test(raw))return '本地数据暂时被占用，稍后重试；已保存结果不受影响。';
    if(/Required reports incomplete|model output|schema|JSON/i.test(raw))return `${label}返回内容未通过检查。已完成部分已保留，可继续未完成分析。`;
    if(/[\u3400-\u9fff]/.test(raw))return raw.replace(/\s+/g,' ').slice(0,120);
    return '分析未完成，已保存成功结果。请继续未完成分析；详细错误保留在运行记录中。';
  }
  function retryVersions(job) {
    return Object.entries(job.variant_progress || {}).filter(([,status]) =>
      status === 'failed' || status === 'incomplete' || (['failed','incomplete'].includes(job.status) && status !== 'complete'))
      .map(([key]) => {const [author, ...rest] = key.split('/'); return {author,version_id:rest.join('/')};});
  }
  function scheduleText(value) {
    return value.enabled ? `自动运行已开启 · 美东 ${String(value.start_et || '').slice(0,5)}` : '自动运行未开启 · 当前不会每天自动跑';
  }
  function dataRetry(job){
    if(!active(job))return null;
    const last=(job.research_progress||[]).filter(e=>String(e.stage||'').startsWith('data_')).at(-1);
    return ['data_retry_scheduled','data_retry_started'].includes(last?.stage)?last:null;
  }
  function primaryAction(jobs, selections, now, profileId) {
    const selected=new Set(selections||[]),day=etDay(now);
    const job=currentJobs(jobs||[],now).find(row=>(active(row)||etDay(row.started_at_et)===day)&&
      profileId&&row.model_profile_id===profileId&&selected.size&&
      [...selected].every(key=>key in (row.variant_progress||{})));
    if(!job)return {action:'start',label:'开始今日分析'};
    if(active(job)){
      const calls=new Map((job.research_progress||[]).filter(e=>e.call_id&&selected.has(e.variant_key)).map(e=>[e.call_id,e]));
      if(dataRetry(job)||[...calls.values()].some(e=>e.stage==='model_retry_scheduled'))return {action:'retrying',label:'正在自动重试',job};
      return {action:'progress',label:'查看进度',job};
    }
    if([...selected].every(key=>job.variant_progress[key]==='complete'))return {action:'results',label:'查看结果',job};
    if(job.next_retry_at)return {action:'retrying',label:'正在自动重试',job};
    return {action:'resume',label:'继续未完成分析',job};
  }
  function preparationHtml(job){
    const latest=new Map((job.research_progress||[]).filter(e=>e.stage==='data_preparation').map(e=>[e.component,e]));
    if(!latest.size)return '';
    const names={universe:'股票池',history:'历史价格',premarket:'盘前价格',metadata:'股票资料',events:'公司公告',options:'期权资料',public_context:'公开市场资料'};
    const retry=dataRetry(job);
    const retryText=retry?`<p class="data-retry">正在自动重试 · ${esc(retry.source||'')} ${esc(retry.symbol||'')} · 第 ${esc(retry.attempt)} / ${esc(retry.max_attempts)} 次尝试${retry.stage==='data_retry_scheduled'&&retry.next_retry_at?' · 下次 '+esc(new Date(retry.next_retry_at).toLocaleTimeString('zh-CN',{hour12:false})):''}</p>`:'';
    return `<section class="preparation-progress"><h4>1 · 准备数据</h4>${retryText}${[...latest].map(([component,e])=>{
      const total=Number.isInteger(e.total)&&e.total>0?e.total:null;
      const completed=Number.isInteger(e.completed)?e.completed:0;
      const status=e.status==='complete'?'已准备':e.status==='failed'?'获取失败':e.status==='unavailable'?'资料不足':!active(job)?'未完成':e.status==='retrying'?'正在重试':'准备中';
      const bar=total?`<progress max="${total}" value="${Math.min(total,completed)}" aria-label="${esc(names[component]||component)}"></progress> ${completed} / ${total}`:'';
      return `<div data-view-key="prepare-${esc(component)}"><b>${esc(names[component]||component)}</b> · ${esc(status)} · ${esc(e.source||'')}${bar}<small>${esc(e.message||'')}</small></div>`;
    }).join('')}</section>`;
  }
  function applyTodayAvailability(button, clock, hasModel) {
    button.disabled = !hasModel || clock?.today_available !== true;
    button.title = clock?.today_message || '正在核验美东交易日与盘前时段';
  }
  function finalizationHtml(job,names){
    const rows=Object.entries(job.variant_progress||{}).map(([key,status])=>{
      const progress=taskProgress(job.research_progress,key),ended=!active(job)||status==='complete';
      const stages=progress.tasks.filter(t=>['adversary','synthesis','decision'].includes(t.task_id));
      if(!stages.length&&status!=='complete')return `<div><b>${esc(names(key))}</b><p>等待任务清单；汇总结果尚未开始。</p></div>`;
      const items=stages.map(task=>{
        const stage=task.task_id==='decision'?'decision_complete':task.task_id;
        const done=progress.rows.some(e=>e.stage===stage&&e.status==='complete');
        const started=progress.rows.some(e=>e.domain===task.task_id||e.stage===stage);
        return {name:task.task_id==='synthesis'?'综合取舍':domainName(task.task_id),done,text:done?'已完成':ended?'未完成':started?'进行中':'等待'};
      });
      items.push({name:'保存结果',done:status==='complete',text:status==='complete'?'已完成':ended?'未完成':'等待'});
      return `<div><b>${esc(names(key))}</b><progress max="${items.length}" value="${items.filter(i=>i.done).length}" aria-label="汇总结果"></progress><p>${items.map(i=>`${esc(i.name)}：${i.text}`).join(' · ')}</p></div>`;
    });
    return rows.length?`<section class="finalization-progress"><h4>3 · 汇总结果</h4>${rows.join('')}</section>`:'';
  }
  function overallHtml(job,now){
    const value=job.progress_summary;if(!value)return '';
    const known=Number.isInteger(value.total_tasks),done=job.status==='complete';
    const amount=known?`${value.completed_tasks} / ${value.total_tasks} 项`:(done?'已完成':'等待任务清单');
    const bar=done?'<progress max="1" value="1" aria-label="整体进度"></progress>':known?`<progress max="${Math.max(1,value.total_tasks)}" value="${value.completed_tasks}" aria-label="整体进度"></progress>`:'';
    const stage={preparation:'准备数据',screening:'筛选候选',domain_analysis:'六领域分析',adversary:'反方审查',decision:'最终决策',complete:'已完成',incomplete:'未完成'}[value.stage]||'准备中';
    const duration=seconds=>seconds<60?`${seconds}秒`:`${Math.floor(seconds/60)}分${seconds%60}秒`;
    const started=Date.parse(value.started_at),end=Date.parse(active(job)?now:value.completed_at);
    const elapsed=Number.isFinite(started)&&Number.isFinite(end)?` · ${active(job)?'已运行':'用时'} ${duration(Math.max(0,Math.floor((end-started)/1000)))}`:'';
    const observed=Date.parse(value.last_event_at),time=Date.parse(now);
    const age=active(job)&&Number.isFinite(observed)&&Number.isFinite(time)?` · 最近进展 ${duration(Math.max(0,Math.floor((time-observed)/1000)))}前`:'';
    return `<section class="overall-progress"><b>${done?'今日研究已完成':`当前阶段：${esc(stage)}`}</b><label>整体进度 · ${esc(amount)}${bar}</label><small>${esc(stage+elapsed+age)}</small></section>`;
  }
  function updateClocks(jobs,now){
    for(const article of document.querySelectorAll('[data-progress-job]')){
      const job=jobs.find(row=>row.job_id===article.dataset.progressJob);
      const summary=article.querySelector('.overall-progress');
      if(job&&active(job)&&summary)summary.outerHTML=overallHtml(job,now);
      if(job&&active(job))for(const node of article.querySelectorAll('[data-call-start]')){
        node.textContent=attemptStatus({occurred_at_et:node.dataset.callStart},now);
      }
    }
  }
  function progressHtml(jobs, versions, now) {
    const rows = currentJobs(jobs, now);
    if (!rows.length) return '<p class="progress-empty">今天还没有启动分析。历史记录请到「查看结果」。</p>';
    const names = key => {
      const [author, id] = key.split('/');
      const item = versions.find(v => v.author === author && (v.version_id === id || (v.aliases || []).includes(id)));
      return item?.method_name || item?.label || key;
    };
    const status = value => ({queued:'等待开始',running:'分析中',complete:'已完成',partial_failure:'部分失败',failed:'失败',stopped:'已停止',cancelled:'已取消',incomplete:'未完成'}[value] || value);
    return rows.map(job => {
      const date = job.started_at_et ? new Date(job.started_at_et) : null;
      const time = date && Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN',{hour12:false}) : '等待启动';
      const previousDay=etDay(job.started_at_et)&&etDay(job.started_at_et)!==etDay(now);
      const carry = previousDay ? ` · 历史未完成（${etDay(job.started_at_et)}）` : '';
      const versionsHtml = Object.entries(job.variant_progress || {}).map(([key,value])=>{
        // Terminal job state wins over stale in-flight display events. A completed
        // sibling stays complete even when the batch ends with another failure.
        const executionState=!active(job)&&['queued','running'].includes(value)?'incomplete':value;
        const progress=taskProgress(job.research_progress,key);
        const summary=job.progress_summary?.variants?.[key];
        if(summary){progress.total=summary.total_tasks;progress.complete=summary.completed_tasks;}
        const failure=job.variant_errors?.[key]||progress.rows.filter(row=>row.status==='failed').at(-1)||{};
        const reason=value==='failed'?`<p class="status bad">${esc(failureText(failure))}</p>`:'';
        const completed=executionState==='complete';
        const text=completed?'已完成':progress.total===null?'等待任务清单':`${progress.complete} / ${progress.total} 项`;
        const bar=completed?`<progress max="1" value="1" aria-label="${esc(names(key))}：已完成"></progress>`:
          progress.total===null?'':`<progress max="${Math.max(1,progress.total)}" value="${progress.complete}" aria-label="${esc(names(key))}：${esc(text)}"></progress>`;
        return `<details class="research-progress variant-progress" data-view-key="${esc(key)}" data-progress-variant="${esc(key)}"><summary><span>${esc(names(key))} · ${esc(status(executionState))}</span>${bar}<small>${text}</small></summary>${reason}${compactResearchHtml(progress,executionState,now)}</details>`;
      }).join('');
      return `<article class="progress-batch" data-progress-job="${esc(job.job_id)}"><header><span>${esc(previousDay?carry.slice(3):'本次运行')}</span><b>${esc(status(job.status))}</b></header>${overallHtml(job,now)}${preparationHtml(job)}${versionsHtml?'<h4>2 · 两版分析</h4>'+versionsHtml:''}${finalizationHtml(job,names)}<div class="progress-actions">${job.batch_id ? `<button class="text-button" data-progress-result="${esc(job.batch_id)}">查看结果</button>` : ''}${retryVersions(job).length && !active(job) ? `<button class="secondary" data-progress-retry="${esc(job.job_id)}">${job.batch_id?'继续未完成分析':'重试数据准备'}</button>` : ''}</div></article>`;
    }).join('');
  }
  function researchHtml(events, legacyReports, selection={}) {
    const rows=Array.isArray(events)?events:[], reports=Array.isArray(legacyReports)?legacyReports:[];
    if(!rows.length){return reports.length?`<section class="research-view"><p>旧记录没有执行时间线；以下为当时保存且已校验的报告。</p>${reports.map(r=>`<details><summary>${esc(r.domain||'研究报告')}</summary><p>${esc(r.thesis||'')}</p></details>`).join('')}</section>`:'<p>尚无研究执行明细。</p>'}
    const variants=[...new Set(rows.map(r=>r.variant_key).filter(Boolean))], chosen=variants.includes(selection.variant)?selection.variant:variants[0];
    const scoped=rows.filter(r=>!r.variant_key||r.variant_key===chosen), symbols=[...new Set(scoped.flatMap(r=>r.symbols||[]).concat(scoped.map(r=>r.symbol)).filter(Boolean))];
    const symbol=symbols.includes(selection.symbol)?selection.symbol:symbols[0], relevant=scoped.filter(r=>!symbol||r.symbol===symbol||(r.symbols||[]).includes(symbol));
    const counts=callSummary(relevant,chosen),inflight=counts.running;
    const times=relevant.map(r=>Date.parse(r.occurred_at_et)).filter(Number.isFinite), end=inflight&&times.length?Math.max(Date.now(),...times):Math.max(0,...times), elapsed=times.length?(end-Math.min(...times))/1000:Math.max(0,...relevant.map(r=>Number(r.elapsed_seconds||0)));
    const validated=relevant.filter(r=>r.stage==='report_validated'&&r.report);
    const option=(value,selected,attr,label=value)=>`<option value="${esc(value)}" ${attr}="${esc(value)}"${value===selected?' selected':''}>${esc(label)}</option>`;
    const sections=validated.map(r=>{const report=r.report||{}, id=`domain-${r.domain||'other'}`, originalId=`original-${r.symbol||symbol||'all'}-${r.domain||'other'}`, open=(selection.open||[]).includes(id)?' open':'', originalOpen=(selection.open||[]).includes(originalId)?' open':'';return `<details data-research-section="${esc(id)}"${open}><summary>${esc(r.domain||'研究')} · ${esc(r.status||'validated')}</summary><h4>主要结论</h4><p>${esc(report.thesis||'')}</p><p><b>支持：</b>${esc(report.support||report.thesis||'—')}</p><p><b>反方：</b>${esc(report.antithesis||'—')}</p><p><b>未知：</b>${esc((report.unknowns||[]).join('；')||'无')}</p><p><b>失效条件：</b>${esc((report.invalidation||[]).join('；')||'未列明')}</p>${(report.evidence||[]).map(e=>`<p><b>关键证据：</b>${esc(JSON.stringify(e.observed??'值未记录'))}<br><small>${esc(e.provider||'未知来源')} · ${esc(e.captured_at||'时间未记录')} · ${esc(e.unit||'单位未记录')} · ${esc(e.source_uri||'')}</small></p>`).join('')}<details data-research-section="${esc(originalId)}"${originalOpen}><summary>原报告与来源细节</summary><pre>${esc(JSON.stringify(report.original||report,null,2))}</pre></details></details>`}).join('');
    const labels={call_requested:'请求分析',model_started:'模型开始',model_returned:'模型返回',cache_hit:'复用缓存',report_validated:'报告已校验',validation_failure:'校验失败',adversary:'反方审查',decision_complete:'决策完成',failure:'失败',variant_reused:'复用完整版本',preparation:'准备',screening:'筛选'};
    const statusName=value=>({requested:'等待回应',running:'进行中',complete:'已完成',validated:'已校验',cache_hit:'复用缓存',reused:'复用结果',no_data:'没有合格资料',failed:'失败'}[value]||value||'');
    const timeline=relevant.map(r=>`<li>${esc(labels[r.stage]||r.stage)} · ${esc(domainName(r.domain))} ${esc((r.symbols||[r.symbol]).filter(Boolean).join('、'))} · ${esc(statusName(r.status))} · ${esc(r.occurred_at_et||'')}${r.elapsed_seconds!=null?` · ${Number(r.elapsed_seconds).toFixed(1)}秒`:''}</li>`).join('');
    const timelineOpen=(selection.open||[]).includes('timeline')?' open':'';
    const versionLabel=key=>{const [author,...rest]=key.split('/'),id=rest.join('/');const item=(selection.versions||[]).find(v=>(v.author||'team')===author&&(v.version_id===id||(v.aliases||[]).includes(id)));return item?.method_name||item?.label||key};
    return `<section class="research-view"><label>版本 <select data-view-key="variant" data-research-variant>${variants.map(v=>option(v,chosen,'data-research-variant-option',versionLabel(v))).join('')}</select></label><label>候选 <select data-view-key="symbol" data-research-symbol>${symbols.map(s=>option(s,symbol,'data-research-symbol')).join('')}</select></label><p>${callText(counts)} · 实际耗时 ${elapsed.toFixed(1)} 秒</p><details data-research-section="timeline"${timelineOpen}><summary>执行时间线</summary><ol>${timeline}</ol></details>${sections||'<p>尚无已校验报告；原始无效输出不会显示为结论。</p>'}</section>`;
  }
  return {etDay,currentJobs,retryVersions,scheduleText,progressHtml,researchHtml,callSummary,taskProgress,domainName,applyTodayAvailability,failureText,updateClocks,primaryAction};
})();
if (typeof module !== 'undefined') module.exports = SHAQProgress;
