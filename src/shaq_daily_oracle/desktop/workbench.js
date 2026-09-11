// Three work surfaces share the existing desktop bridge and immutable records.
const wb={filters:{},selectedModule:'',draft:'',base:null,scheduleSelectionsLoaded:false};
const formalState=s=>({waiting:'等待开始',workflow_running:'正式流程运行中',workflow_complete:'正式流程完成',workflow_failed:'正式流程失败',connected_to_existing_worker:'已连接原后台',missed_daily_cutoff:'已错过盘前截止',session_already_recorded:'今日已完成',configuration_error:'配置错误',setup_required:'需要完成连接'}[s]||s||'等待后台回应');
async function showFormalRun(id){try{const run=await api('get_run',id);const target=q('#formal-detail');target.innerHTML=`<h3>${esc(run.trade_date)} · 正式模拟盘</h3><p>${run.predictions.map(p=>`${esc(p.symbol)} ${dir(p.direction)}`).join('、')||'没有发布预测'} · 模拟订单 ${run.orders.length} 笔</p><label>股票 <select id="formal-symbol">${Object.keys(run.reports_by_symbol).map(s=>`<option>${esc(s)}</option>`).join('')}</select></label><div id="formal-analysis"></div>`;const draw=()=>{const symbol=q('#formal-symbol').value;q('#formal-analysis').innerHTML=(run.reports_by_symbol[symbol]||[]).map(r=>`<h4>${esc(moduleName(r.domain))} · ${dir(r.verdict)}</h4><p>${esc(r.thesis||'')}</p><p>反方：${esc(r.antithesis||'')}</p>`).join('')};q('#formal-symbol').onchange=draw;draw()}catch(e){notice(e.message,true)}}
const moduleName=id=>({decision:'最终决策',screening:'候选筛选',market:'市场环境',relationships:'行业与关系传导',event:'公司催化事件',capital:'买卖压力与流动性',derivatives:'期权定价与仓位线索',price_volume:'价格走势与参与度',adversary:'反方审查'}[id]||(state.data?.skill_explanations?.[id]||id).split(' · ')[0]);
const versionName=v=>v.method_name||v.label||v.version_id;
const changed=v=>{if(v.status_badge==='正式基准')return '基准配置';const baseline=(state.data.versions||[]).find(x=>x.status_badge==='正式基准')?.skill_hashes||{};const paths=Object.entries(v.skill_hashes||{}).filter(([p,h])=>baseline[p]!==h).map(([p])=>p.split('/')[0]==='decision'?'decision':p.split('/')[1]);return [...new Set(paths.map(moduleName))].join('、')||'与当前基准一致'};
const historyIdentity=row=>SHAQAccounts.historyIdentity(row,state.data?.versions||[]);
const historyMethod=row=>{const x=historyIdentity(row);return `${x.method_name} · ${x.status_badge}`};
const jobName=s=>({queued:'等待开始',running:'运行中',complete:'已完成',partial_failure:'部分失败',failed:'失败'}[s]||s);
const oldRenderEditor=renderEditor,oldRenderHistory=renderHistory,oldRenderBatch=renderBatch,oldLoadSkill=loadSkill;
function restoreUtilities(){for(const section of [...qa('#utility-content > .page')]){section.classList.remove('active');q('main').append(section)}q('#utility-content')?.replaceChildren()}
function openUtility(page,title){let layer=q('#utility');if(!layer){layer=document.createElement('div');layer.id='utility';layer.className='modal';layer.innerHTML='<div class="setup-card"><div class="section-head"><h2 id="utility-title"></h2><button class="secondary" id="close-utility">关闭</button></div><div id="utility-content"></div></div>';document.body.append(layer);q('#close-utility').onclick=()=>{restoreUtilities();layer.classList.add('hidden')}}restoreUtilities();q('#utility-title').textContent=title;q('#utility-content').append(q('#'+page));q('#'+page).classList.add('active');layer.classList.remove('hidden');({upload:renderUpload,updates:renderUpdates,data:renderData,settings:renderSettings}[page])()}
function teamSync(){openUtility('upload','团队同步');const bar=document.createElement('div');bar.className='editor-tabs';bar.innerHTML='<button class="secondary" id="sync-upload">上传我的版本</button><button class="secondary" id="sync-download">获取团队版本</button>';q('#utility-content').prepend(bar);q('#sync-upload').onclick=()=>teamSync();q('#sync-download').onclick=()=>{q('#upload').classList.remove('active');document.querySelector('main').append(q('#upload'));q('#utility-content').append(q('#updates'));q('#updates').classList.add('active');renderUpdates()}}
renderRun=function(){
  const s=state.data.settings, versions=state.data.versions||[], profiles=s.model_profiles||[];
  if(state.runSelections===null)state.runSelections=new Set(versions.filter(v=>v.status_badge==='正式基准').map(versionKey));
  const rows=versions.map(v=>`<tr><td><input class="version-check" type="checkbox" data-author="${esc(v.author||'team')}" data-version="${esc(v.version_id)}" ${state.runSelections.has(versionKey(v))?'checked':''}></td><td>${esc(versionName(v))} <span class="method-badge">${esc(v.status_badge||'本地版本')}</span><br><small>${esc(v.description||'')}</small></td><td>${esc(v.author||'团队')}</td><td>${esc(changed(v))}</td></tr>`).join('');
  q('#run').innerHTML=`<div class="run-toolbar"><span></span><button class="text-button" id="show-data">查看数据时间</button><label>模型 <select id="run-profile">${profiles.map(p=>`<option value="${esc(p.profile_id)}" ${p.profile_id===s.active_model_profile_id?'selected':''}>${esc(p.model)} · ${esc(p.profile_id)}</option>`).join('')}</select></label></div>
    <div class="sheet"><table class="table"><thead><tr><th></th><th>方法版本</th><th>作者</th><th>修改模块</th></tr></thead><tbody>${rows}</tbody></table><div class="run-actions"><button class="secondary" id="select-all">全选</button><button class="primary" id="start-batch" ${profiles.length?'':'disabled'}>运行选中版本</button></div><div id="estimate" class="estimate"></div></div>
    <details class="sheet" id="automatic-panel"><summary id="automatic-summary">读取自动运行设置…</summary><div id="automatic-settings"></div></details>
    <section class="sheet"><h3>当日运行进度</h3><div id="today-progress">${SHAQProgress.progressHtml(state.data.jobs||[],versions,state.data.clock?.et||new Date().toISOString())}</div></section>`;
  q('#start-batch').onclick=startBatch;
  q('#select-all').onclick=()=>{const boxes=qa('.version-check'),on=boxes.some(b=>!b.checked);boxes.forEach(b=>b.checked=on);rememberSelections();estimate()};
  qa('.version-check').forEach(b=>b.onchange=()=>{rememberSelections();estimate()});
  q('#run-profile').onchange=estimate;
  q('#show-data').onclick=()=>openUtility('data','数据更新时间');
  qa('[data-progress-result]').forEach(b=>b.onclick=()=>{showPage('history');loadBatch(b.dataset.progressResult)});
  qa('[data-progress-retry]').forEach(b=>b.onclick=()=>{
    const job=(state.data.jobs||[]).find(j=>j.job_id===b.dataset.progressRetry);
    const selected=SHAQProgress.retryVersions(job||{});
    state.runSelections=new Set(selected.map(v=>versionKey(v)));
    qa('.version-check').forEach(box=>box.checked=state.runSelections.has(box.dataset.author+'/'+box.dataset.version));
    estimate(); notice('已选择失败版本，请确认上方模型后点击运行；成功分析会复用。');
    q('#start-batch').focus();
  });
  qa('[data-research-variant],[data-research-symbol]').forEach(select=>select.onchange=()=>{
    const article=select.closest('[data-progress-job]');
    const job=(state.data.jobs||[]).find(row=>row.job_id===article?.dataset.progressJob);
    if(!job)return;
    job.research_selection={variant:article.querySelector('[data-research-variant]')?.value,
      symbol:article.querySelector('[data-research-symbol]')?.value,
      open:[...article.querySelectorAll('[data-research-section][open]')].map(row=>row.dataset.researchSection)};
    renderRun();
  });
  const panel=q('#automatic-panel');panel.open=Boolean(wb.autoPanelOpen);panel.ontoggle=()=>{wb.autoPanelOpen=panel.open};
  renderAutomatic();estimate();
};
async function renderAutomatic(){
  const target=q('#automatic-settings'), summary=q('#automatic-summary');
  try{
    const x=await api('get_research_schedule');
    if(!target.isConnected)return;
    summary.textContent=SHAQProgress.scheduleText(x);
    target.innerHTML=`<label class="check-row"><input id="auto-enabled" type="checkbox" ${x.enabled?'checked':''}>每天自动运行</label><label>美东启动时间 <input id="auto-time" type="time" value="${esc(x.start_et.slice(0,5))}"></label><p>${x.enabled?esc(x.local_start||''):'尚未启用，不会自动启动。'} ${esc(x.status_message||'')}</p><p>保存时使用上方勾选的版本和模型。电脑需要保持开机、联网。</p><button class="secondary" id="save-automatic">保存自动运行设置</button>`;
    q('#save-automatic').onclick=async()=>{
      try{
        await api('save_research_schedule',{enabled:q('#auto-enabled').checked,start_et:q('#auto-time').value,selections:selectedVersions(),model_profile_id:q('#run-profile').value});
        notice('自动运行设置已保存');renderAutomatic();
      }catch(e){notice(e.message,true)}
    };
  }catch(e){target.textContent=e.message;summary.textContent='自动运行状态读取失败，请展开检查'}
}
renderEditor=function(){oldRenderEditor();const tools=q('.editor-toolbar');tools.insertAdjacentHTML('beforeend','<button class="primary" id="copy-version">复制为新版本</button><button class="secondary" id="team-sync">团队同步</button>');q('#team-sync').onclick=teamSync;q('#copy-version').onclick=async()=>{try{const [author,version_id]=q('#edit-version').value.split('/');const value=await api('copy_local_version',version_id,author);wb.draft=value.draft_id;q('#draft-id').value=wb.draft;notice('已复制为本地草稿，可开始修改')}catch(e){notice(e.message,true)}};q('#save-draft').textContent='保存模块到草稿';q('.package-footer').insertAdjacentHTML('beforeend','<div class="card"><label>修改说明<input id="version-description" placeholder="这次改了什么"></label><button class="primary" id="publish-local">保存版本并用于运行</button></div>');q('#publish-local').onclick=async()=>{try{await api('finalize_local_version',q('#draft-id').value,q('#version-description').value);notice('新版本已保存，可在开始运行中选择');await load(false)}catch(e){notice(e.message,true)}};const container=document.createElement('div');container.className='module-workspace';const menu=document.createElement('aside');menu.className='module-menu';menu.innerHTML=Object.keys(state.data.skill_explanations).map(id=>`<button class="module-button" data-module="${esc(id)}">${esc(moduleName(id))}</button>`).join('')+'<button class="module-button" data-module="decision">最终决策</button>';const body=document.createElement('div');body.className='module-body';[...q('#editor').children].filter(x=>x!==tools).forEach(x=>body.append(x));container.append(menu,body);q('#editor').append(container);qa('[data-module]').forEach(b=>b.onclick=()=>{qa('[data-module]').forEach(x=>x.classList.toggle('active',x===b));if(b.dataset.module==='decision'){q('.decision-editor').scrollIntoView({behavior:'smooth',block:'start'})}else{q('#edit-skill').value=b.dataset.module;loadSkill()}});q('#edit-skill').closest('label').classList.add('hidden');q('.package-intro').classList.add('hidden');if(wb.draft)q('#draft-id').value=wb.draft;};
saveDraft=async function(){const id=q('#draft-id').value.trim();if(!id)return notice('请先复制为新版本',true);try{await api('save_skill_package_draft',q('#edit-skill').value,q('#skill-method').value,q('#skill-foundations').value,{display_name:q('#agent-display-name').value,short_description:q('#agent-description').value,default_prompt:q('#agent-prompt').value},id);wb.draft=id;notice('领域修改已保存到本地草稿')}catch(e){notice(e.message,true)}};
function plotResults(rows){const eligible=rows.filter(r=>r.score_eligible!==false&&r.daily_pnl!==null),groups={};for(const r of [...eligible].reverse()){(groups[historyIdentity(r).series_key]??=[]).push(r)}const all=Object.values(groups).flat(),vals=all.map(r=>Number(r.cumulative_pnl||0));let lo=Math.min(0,...vals),hi=Math.max(0,...vals);if(hi===lo){hi++;lo--}const dates=[...new Set(all.map(r=>r.trade_date))].sort();const colors=['#285ba8','#35846c','#ad7833','#89559c','#7c8289'];return `<svg class="pnl-chart" viewBox="0 0 900 190" role="img" aria-label="各版本累计纸面盈亏"><line x1="60" y1="${160-(0-lo)/(hi-lo)*135}" x2="880" y2="${160-(0-lo)/(hi-lo)*135}" stroke="#d3d6da"/><text x="4" y="25">$${hi.toFixed(2)}</text><text x="4" y="160">$${lo.toFixed(2)}</text>${Object.values(groups).map((rs,i)=>`<polyline fill="none" stroke="${colors[i%colors.length]}" stroke-width="2" points="${rs.map(r=>`${60+dates.indexOf(r.trade_date)/Math.max(1,dates.length-1)*800},${160-(Number(r.cumulative_pnl||0)-lo)/(hi-lo)*135}`).join(' ')}"/><text x="${65+i*165}" y="185" fill="${colors[i%colors.length]}">${esc(historyMethod(rs[0]))}</text>`).join('')}</svg>`}
renderHistory=function(){oldRenderHistory();const rows=state.data.dashboard.daily_results||[],versionOptions=new Map(),models=[...new Set(rows.map(r=>r.model||'未记录模型'))];for(const row of rows){const x=historyIdentity(row);if(!versionOptions.has(x.filter_key))versionOptions.set(x.filter_key,historyMethod(row))}const tools=document.createElement('div');tools.className='history-filters';tools.innerHTML=`<label>开始日期<input id="history-from" type="date" value="${esc(wb.filters.from||'')}"></label><label>结束日期<input id="history-to" type="date" value="${esc(wb.filters.to||'')}"></label><label>版本<select id="history-version"><option value="">全部版本</option>${[...versionOptions].map(([key,label])=>`<option value="${esc(key)}" ${wb.filters.version===key?'selected':''}>${esc(label)}</option>`).join('')}</select></label><label>模型<select id="history-model"><option value="">全部模型</option>${models.map(v=>`<option ${wb.filters.model===v?'selected':''}>${esc(v)}</option>`).join('')}</select></label>`;q('#history').prepend(tools);const filtered=rows.filter(r=>(!wb.filters.from||r.trade_date>=wb.filters.from)&&(!wb.filters.to||r.trade_date<=wb.filters.to)&&(!wb.filters.version||historyIdentity(r).filter_key===wb.filters.version)&&(!wb.filters.model||(r.model||'未记录模型')===wb.filters.model));const kept=new Set(filtered.map(r=>r.batch_id+'|'+r.variant_key));qa('#history tr[data-batch]').forEach(r=>r.classList.toggle('hidden',!kept.has(r.dataset.batch+'|'+r.dataset.variantKey)));q('#history .metrics').outerHTML=`<div class="result-summary">${(()=>{const scored=filtered.filter(r=>r.score_eligible!==false&&r.status==='final'),good=scored.reduce((s,r)=>s+r.correct,0),bad=scored.reduce((s,r)=>s+r.incorrect,0);return `已核验 ${good+bad} 次 · 正确 ${good} / 错误 ${bad} · 命中率 ${good+bad?(100*good/(good+bad)).toFixed(1)+'%':'—'} · 空榜 ${filtered.filter(r=>r.status==='empty'&&r.score_eligible!==false).length} 次`})()}<span>固定一股 · 零费用纸面回放</span></div><div class="sheet">${plotResults(filtered)}</div>`;for(const [id,key] of [['history-from','from'],['history-to','to'],['history-version','version'],['history-model','model']])q('#'+id).onchange=e=>{wb.filters[key]=e.target.value;renderHistory()};};
renderBatch=function(batch,key,symbol){oldRenderBatch(batch,key,symbol);const head=q('.batch-head');if(!head)return;head.insertAdjacentHTML('beforeend',`<label>对比本次另一版本 <select id="compare-version"><option value="">选择版本</option>${Object.entries(batch.variants||{}).filter(([k])=>k!==key).map(([k,v])=>`<option value="${esc(k)}">${esc(historyMethod({variant_key:k,label:v.variant?.label}))}</option>`).join('')}</select></label><div id="version-comparison"></div>`);q('#compare-version').onchange=e=>{const v=batch.variants[e.target.value];q('#version-comparison').innerHTML=v?`<table class="table"><thead><tr><th>股票</th><th>对照版本结论</th><th>理由</th></tr></thead><tbody>${Object.entries(v.integration_audit||{}).map(([s,a])=>`<tr><td>${esc(s)}</td><td>${dir(v.predictions?.find(p=>p.symbol===s)?.direction||'neutral')}</td><td>${esc(a.decision_reason||(a.rejection_reasons||[]).join('；'))}</td></tr>`).join('')}</tbody></table>`:''}};
showPage=function(page){state.page=page;qa('main>.page,.nav').forEach(e=>e.classList.remove('active'));q('#'+page).classList.add('active');q(`.nav[data-page="${page}"]`)?.classList.add('active');q('#page-title').textContent={run:'开始运行',editor:'修改版本',history:'查看结果'}[page]||'SHAQ';renderPage()};
q('#connections-button').onclick=()=>q('#setup').classList.remove('hidden');q('#close-setup').onclick=()=>q('#setup').classList.add('hidden');q('#model-step').parentElement.prepend(q('#model-step'));
const editorWithLayout=renderEditor;
const skillModuleIds={'market-common-shock':'market','pit-peer-spillover':'relationships','primary-event-reasoner':'event','capital-order-flow':'capital','derivatives-evidence':'derivatives','price-volume-structure':'price_volume'};
renderEditor=function(){editorWithLayout();const first=document.createElement('button');first.className='module-button';first.textContent='候选筛选';first.dataset.module='screening';q('.module-menu').prepend(first);const panel=document.createElement('div');panel.id='module-code-panel';panel.className='sheet hidden';panel.innerHTML='<h3 id="module-code-title"></h3><label>规则代码<textarea id="module-code" spellcheck="false"></textarea></label><label>研究依据与固定测试<textarea id="module-cases" spellcheck="false"></textarea></label><button class="primary" id="save-module-code">测试并保存计算规则</button><p id="module-code-status"></p>';q('.module-body').append(panel);q('#save-module-code').onclick=async()=>{try{if(!q('#draft-id').value)throw new Error('请先复制为新版本');const v=await api('save_module_draft',wb.selectedModule,q('#module-code').value,q('#module-cases').value,q('#draft-id').value);q('#module-code-status').textContent=`${v.case_count}项测试通过，已保存`;notice('计算规则已保存到草稿')}catch(e){q('#module-code-status').textContent=e.message}};qa('[data-module]').forEach(button=>{const previous=button.onclick;button.onclick=async()=>{previous?.();const id=button.dataset.module;const module=skillModuleIds[id]||(id==='screening'?'screening':null);q('#module-code-panel').classList.toggle('hidden',!module);if(module){wb.selectedModule=module;const [author,version]=q('#edit-version').value.split('/');try{const doc=await api('get_module_document',module,version,author,q('#draft-id').value);q('#module-code-title').textContent=moduleName(module)+' · 计算规则';q('#module-code').value=doc.script;q('#module-cases').value=doc.cases;panel.scrollIntoView({block:'start',behavior:'smooth'})}catch(e){notice(e.message,true)}}}});};
loadSkill=async function(){await oldLoadSkill();if(!q('#draft-id')?.value)return;try{const docs=await api('get_local_draft',q('#draft-id').value),name=q('#edit-skill').value,prefix=`skills/${name}/`;if(docs[prefix+'SKILL.md'])q('#skill-method').value=docs[prefix+'SKILL.md'];if(docs[prefix+'references/foundations.md'])q('#skill-foundations').value=docs[prefix+'references/foundations.md'];const role=docs[prefix+'agents/openai.yaml'];if(role){for(const [field,id] of [['display_name','agent-display-name'],['short_description','agent-description'],['default_prompt','agent-prompt']]){const line=role.split('\n').find(x=>x.startsWith('  '+field+': '));if(line)q('#'+id).value=JSON.parse(line.slice(field.length+4))}}if(docs['decision/decision.js'])q('#decision-script').value=docs['decision/decision.js'];if(docs['decision/cases.json'])q('#decision-cases').value=docs['decision/cases.json'];}catch(e){notice(e.message,true)}};

// The detail selector must follow this version's screener, not the union collected for the batch.
const renderComparedBatch=renderBatch;
renderBatch=function(batch,key,symbol){const selected=key&&batch.variants?.[key]?key:Object.keys(batch.variants||{})[0];const intake=batch.variants?.[selected]?.candidate_intake;renderComparedBatch(intake?{...batch,evidence:{...batch.evidence,candidates:intake.candidates}}:batch,selected,symbol)};

const editorWithModules=renderEditor;
renderEditor=function(){
  editorWithModules();
  const body=q('.module-body'), footer=q('.package-footer'), panel=q('#module-code-panel'), decision=q('.decision-editor');
  body.insertBefore(panel,footer);body.insertBefore(decision,footer);decision.classList.add('hidden');
  const tabs=q('.module-body > .editor-tabs');
  tabs.insertAdjacentHTML('beforeend','<button class="tab hidden" id="calculation-tab">计算代码与测试</button>');
  q('#calculation-tab').onclick=()=>{qa('[data-editor-panel]').forEach(e=>e.classList.add('hidden'));qa('[data-editor-tab]').forEach(e=>e.classList.remove('active'));panel.classList.remove('hidden');q('#calculation-tab').classList.add('active')};
  qa('[data-editor-tab]').forEach(button=>{const handler=button.onclick;button.onclick=()=>{handler();panel.classList.add('hidden');q('#calculation-tab').classList.remove('active')}});
  qa('[data-module]').forEach(button=>button.onclick=async()=>{
    const id=button.dataset.module,module=skillModuleIds[id]||(id==='screening'?'screening':null);
    qa('[data-module]').forEach(e=>e.classList.toggle('active',e===button));
    decision.classList.toggle('hidden',id!=='decision');
    tabs.classList.toggle('hidden',id==='decision'||id==='screening');
    qa('[data-editor-panel]').forEach(e=>e.classList.add('hidden'));
    panel.classList.add('hidden');q('#calculation-tab').classList.toggle('hidden',!module);
    if(id!=='decision'&&id!=='screening'){q('#edit-skill').value=id;await loadSkill();q('[data-editor-tab="method"]').click()}
    if(module){wb.selectedModule=module;const [author,version]=q('#edit-version').value.split('/');try{const doc=await api('get_module_document',module,version,author,q('#draft-id').value);q('#module-code-title').textContent=moduleName(module);q('#module-code').value=doc.script;q('#module-cases').value=doc.cases;if(id==='screening')panel.classList.remove('hidden')}catch(e){notice(e.message,true)}}
  });
  q('#draft-id').onchange=e=>{wb.draft=e.target.value.trim()};
};

const renderFilteredHistory=renderHistory;
renderHistory=function(){
  renderFilteredHistory();
  const rows=(state.data.dashboard.daily_results||[]).filter(r=>(!wb.filters.from||r.trade_date>=wb.filters.from)&&(!wb.filters.to||r.trade_date<=wb.filters.to)&&(!wb.filters.version||historyIdentity(r).filter_key===wb.filters.version)&&(!wb.filters.model||(r.model||'未记录模型')===wb.filters.model));
  const groups=new Map();for(const r of rows){const id=r.trade_date+'|'+historyIdentity(r).series_key;if(!groups.has(id))groups.set(id,[]);groups.get(id).push(r)}
  const tbody=q('.result-table tbody');
  tbody.innerHTML=[...groups.values()].map(rs=>{const r=rs.find(x=>x.score_eligible===true)||rs[0],identity=historyIdentity(r);return `<tr data-batch="${esc(r.batch_id)}" data-variant-key="${esc(r.variant_key)}" class="clickable"><td>${esc(r.trade_date)}</td><td>${esc(identity.method_name)} <span class="method-badge">${esc(identity.status_badge)}</span><br><small>${esc(r.model||'')}</small></td><td>${r.predictions.map(p=>`${esc(p.symbol)} ${dir(p.direction)}`).join('、')||'空榜'}</td><td>${r.status==='final'?`${r.correct} / ${r.incorrect}`:'—'}</td><td>${money(r.daily_pnl)}</td><td>${money(r.cumulative_pnl)}</td><td>${replayStatus(r.status)}<br><small>${r.score_eligible?'盘前记录':'练习／回放，不计盘前成绩'}</small>${rs.length>1?`<details><summary>${rs.length} 次运行</summary>${rs.map(x=>`<button class="text-button" data-repeat-batch="${esc(x.batch_id)}" data-key="${esc(x.variant_key)}">${esc(x.batch_id)} · ${x.score_eligible?'计分':'不计分'}</button>`).join('')}</details>`:''}</td></tr>`}).join('')||'<tr><td colspan="7">尚无本地研究记录</td></tr>';
  qa('.result-table tr[data-batch]').forEach(e=>e.onclick=event=>{if(!event.target.closest('details'))loadBatch(e.dataset.batch,e.dataset.variantKey)});
  qa('[data-repeat-batch]').forEach(e=>e.onclick=event=>{event.stopPropagation();loadBatch(e.dataset.repeatBatch,e.dataset.key)});
};

const renderRunSurface=renderRun;
renderRun=function(){renderRunSurface();const clock=state.data.clock;if(clock)q('.run-toolbar > span').textContent=`${clock.is_trading_day?'交易日 '+clock.trade_date:'今日休市 · 下次交易日 '+clock.next_trade_date} · 本地 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;const updated=state.data.data_status?.items?.[0]?.updated_at;q('#show-data').textContent=updated?'数据 '+new Date(updated).toLocaleTimeString('zh-CN',{hour12:false}):'数据将在运行时更新'};


estimate=async function(){const target=q('#estimate');if(!target)return;const items=selectedVersions();if(!items.length){target.textContent='至少选择一个版本。';return}try{const v=await api('estimate_shadow_batch',items,q('#run-profile')?.value||'');target.innerHTML=`${v.selected_versions} 个版本 · 采集后按资料量拆分调用，已成功的相同分析直接复用。<br><small>主要可用：${esc(v.available_domains.join('、'))}；${esc([...v.limited_domains,...v.unavailable_domains].join('；'))}</small>`}catch(e){target.textContent=e.message}};

// Workflow timing and the paper ledger are program-owned, not editable research modules.
const loadEditableSkill=loadSkill;
loadSkill=async function(){if(q('#edit-skill')?.value==='daily-oracle')q('#edit-skill').value='market-common-shock';return loadEditableSkill()};
const renderEditableModules=renderEditor;
renderEditor=function(){renderEditableModules();q('[data-module="daily-oracle"]')?.remove();q('#edit-skill option[value="daily-oracle"]')?.remove();q('[data-module="market-common-shock"]')?.classList.add('active');qa('#editor textarea,#editor input').forEach(field=>field.addEventListener('input',()=>{const active=q('.module-button.active');if(active&&!active.dataset.dirty){active.dataset.dirty='true';active.textContent+=' •'}}))};

const showOriginalCandidate=window.showCandidate;
window.showCandidate=function(batchId,key,symbol){showOriginalCandidate(batchId,key,symbol);const reports=state.selectedBatch?.variants?.[key]?.reports_by_symbol?.[symbol]||[];qa('#candidate-analysis .domain h4').forEach((heading,i)=>{if(reports[i])heading.textContent=moduleName(reports[i].domain)+' · '+dir(reports[i].verdict)})};

const renderReplay=renderBatch;
renderBatch=function(batch,key,symbol){const chosen=key&&batch.variants?.[key]?key:Object.keys(batch.variants||{})[0];renderReplay(batch,chosen,symbol);const selector=q('#compare-version');if(!selector)return;selector.onchange=()=>{
  const other=selector.value,target=q('#version-comparison');if(!other){target.innerHTML='';return}
  const left=batch.variants[chosen],right=batch.variants[other],a=batch.skill_snapshots?.[chosen]?.documents||{},b=batch.skill_snapshots?.[other]?.documents||{};
  const paths=[...new Set([...Object.keys(a),...Object.keys(b)])].filter(p=>a[p]!==b[p]);
  const symbols=[...new Set([...Object.keys(left.reports_by_symbol||{}),...Object.keys(right.reports_by_symbol||{})])].sort();
  const brief=(v,s)=>(v.reports_by_symbol?.[s]||[]).map(r=>`${moduleName(r.domain)}：${dir(r.verdict)}`).join('；')||'未进入此版本候选';
  target.innerHTML=`<p>模型：${esc(left.model_name||'已记录')} / ${esc(right.model_name||'已记录')} · ${left.model_profile_sha256===right.model_profile_sha256?'相同模型配置':'模型配置不同'}</p><details><summary>方法与代码差异：${paths.length} 个文件</summary>${paths.map(p=>`<h4>${esc(p)}</h4><div class="comparison-columns"><pre>${esc(a[p]||'未配置此扩展')}</pre><pre>${esc(b[p]||'未配置此扩展')}</pre></div>`).join('')||'<p>方法、研究依据和代码相同。</p>'}</details><table class="table"><thead><tr><th>股票</th><th>当前版本</th><th>对照版本</th></tr></thead><tbody>${symbols.map(s=>`<tr><td>${esc(s)}</td><td>${esc(brief(left,s))}</td><td>${esc(brief(right,s))}</td></tr>`).join('')}</tbody></table>`;
}};

const renderResearchAndFormal=renderRun;
renderRun=function(){
  renderResearchAndFormal();
  const formal=state.data.formal_operator;if(!formal?.connected)return;
  const section=document.createElement('section');section.className='sheet';section.id='formal-operator';
  section.innerHTML=`<div class="section-head"><h3>原正式版 · 富途模拟盘</h3><button class="primary" id="start-formal">开始／恢复今天正式版</button></div><p>${esc(formal.session_date)} · ${esc(formalState(formal.service?.state))}</p><p>${esc(formal.service?.message||'沿用原正式数据、模型与每票一股规则；下方版本列表只运行研究Shadow。')}</p>${formal.runs.map(r=>`<p>${esc(r.run_id)} · ${esc(r.stage)} · ${esc(r.status)} ${esc(r.detail||'')} ${r.has_frozen_result?`<button class="text-button" data-formal-run="${esc(r.run_id)}">查看正式结果</button>`:''}</p>`).join('')}<div id="formal-detail"></div>`;
  q('#run').prepend(section);
  q('#start-formal').onclick=async()=>{const button=q('#start-formal');button.disabled=true;try{const result=await api('run_today');notice(result.message);await load(false)}catch(e){notice(e.message,true)}finally{button.disabled=false}};
  qa('[data-formal-run]').forEach(b=>b.onclick=()=>showFormalRun(b.dataset.formalRun));
};
setInterval(()=>{if(state.page==='run'&&state.data?.formal_operator?.connected&&!q('#formal-symbol'))load(false)},15000);

const showCandidateBeforeSynthesis=window.showCandidate;
window.showCandidate=function(batchId,key,symbol){
  showCandidateBeforeSynthesis(batchId,key,symbol);
  const row=state.selectedBatch?.variants?.[key]?.synthesis?.decisions?.find(r=>r.symbol===symbol);
  if(!row)return;
  const section=document.createElement('section');section.className='synthesis-detail';
  section.innerHTML=`<h3>最终综合判断 · ${dir(row.direction)}</h3><p><b>主要依据：</b>${esc(row.thesis)}</p><p><b>最强反方：</b>${esc(row.antithesis)}</p><p><b>最终取舍：</b>${esc(row.resolution)}</p><p><b>为什么选择或放弃它：</b>${esc(row.comparison)}</p><p><b>还不知道：</b>${esc((row.unknowns||[]).join('；')||'无')}</p><p><b>何时失效：</b>${esc((row.invalidation||[]).join('；')||'未列明')}</p><details><summary>所引用的证据</summary>${(row.evidence_ids||[]).map(id=>`<p>${esc(id)}</p>`).join('')}</details>`;
  q('#candidate-analysis .aftermarket').before(section);
};

const renderBatchWithResearchProgress=renderBatch;
renderBatch=function(batch,key,symbol){
  const chosen=key&&batch.variants?.[key]?key:Object.keys(batch.variants||{})[0];
  const chosenSymbol=symbol||state.replay?.symbol||Object.keys(batch.variants?.[chosen]?.reports_by_symbol||{})[0];
  renderBatchWithResearchProgress(batch,chosen,chosenSymbol);
  const reports=batch.variants?.[chosen]?.reports_by_symbol?.[chosenSymbol]||[];
  const selection={variant:chosen,symbol:chosenSymbol,open:state.researchOpen||[]};
  const box=document.createElement('details');box.className='card research-progress';box.open=true;
  box.innerHTML=`<summary>研究执行明细</summary>${SHAQProgress.researchHtml(batch.research_progress||[],reports,selection)}`;
  const head=q('.batch-head');
  if(head?.parentNode)head.parentNode.insertBefore(box,head.nextSibling||null);
  if(typeof box.querySelector==='function'){
    box.querySelector('[data-research-variant]')?.addEventListener('change',e=>renderBatch(batch,e.target.value,chosenSymbol));
    box.querySelector('[data-research-symbol]')?.addEventListener('change',e=>renderBatch(batch,chosen,e.target.value));
  }
  if(typeof box.querySelectorAll==='function')box.querySelectorAll('[data-research-section]').forEach(row=>row.addEventListener('toggle',()=>{
    state.researchOpen=[...box.querySelectorAll('[data-research-section][open]')].map(item=>item.dataset.researchSection);
  }));
};
