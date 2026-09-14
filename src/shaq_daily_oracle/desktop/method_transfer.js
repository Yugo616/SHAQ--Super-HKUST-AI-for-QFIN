// Explicit transfer sessions are independent of the regular application poll.
const SHAQTransfer={
  controller(direction,call,changed){
    const c={direction,status:'idle',rows:[],selected:new Set(),results:new Map(),error:'',busy:false,
      async open(){
        if(c.busy||c.status==='loading')return;
        c.status='loading';c.error='';changed(c);
        try{
          const value=await call('open_method_transfer',direction);
          Object.assign(c,value);c.results.clear();
          c.selected=new Set([...c.selected].filter(key=>c.rows.some(r=>r.key===key&&r.eligible)));
          c.status='ready';
        }catch(error){c.status='failed';c.error=error.message;}
        changed(c);
      },
      select(key,on){if(c.busy||c.status!=='ready'||!c.rows.some(r=>r.key===key&&r.eligible))return;
        on?c.selected.add(key):c.selected.delete(key);changed(c);},
      selectAll(){if(c.busy||c.status!=='ready')return;
        const eligible=c.rows.filter(r=>r.eligible&&c.results.get(r.key)?.status!=='complete');
        const on=eligible.some(r=>!c.selected.has(r.key));
        eligible.forEach(r=>on?c.selected.add(r.key):c.selected.delete(r.key));changed(c);},
      async submit(){
        if(c.busy||c.status!=='ready')return;
        const keys=[...c.selected].filter(key=>c.rows.some(r=>r.key===key&&r.eligible)&&c.results.get(key)?.status!=='complete');
        c.busy=true;changed(c);
        for(const key of keys){
          c.results.set(key,{key,status:'running',message:'正在传输…'});changed(c);
          try{
            const rows=await call('transfer_methods',c.operation_id,[key]);
            const result=rows.find(r=>r.key===key);
            if(!result)throw new Error('传输结果缺失，请刷新清单核实');
            c.results.set(key,result);
            if(result.status==='complete'){c.selected.delete(key);c.rows.find(r=>r.key===key).eligible=false;}
          }catch(error){c.results.set(key,{key,status:'failed',message:error.message});}
          changed(c);
        }
        c.busy=false;changed(c);
      }
    };return c;
  },
  preserveEditor(build){return function(){
    const select=q('#edit-version');
    if(select){
      // Keep the DOM, event bindings, unsaved text, selection and scroll intact.
      const existing=new Set([...select.options].map(o=>o.value));
      for(const v of state.data.versions||[]){const key=`${v.author||'team'}/${v.version_id}`;
        if(!existing.has(key))select.insertAdjacentHTML('beforeend',`<option value="${esc(key)}">${esc(v.label||v.method_name||v.version_id)}</option>`);}
      return;
    }
    build();
    q('#load-skill')?.remove();q('#team-sync')?.remove();
    const copy=q('#copy-version');copy.textContent='新建本地草稿';q('.package-footer').prepend(copy);
    q('.editor-toolbar').insertAdjacentHTML('beforeend','<button class="secondary" id="download-methods">下载</button><button class="primary" id="upload-methods">上传</button>');
    q('#download-methods').onclick=()=>openMethodTransfer('download');
    q('#upload-methods').onclick=()=>openMethodTransfer('upload');
    q('#edit-version').onchange=()=>{wb.draft='';q('#draft-id').value='';loadSkill();};
    q('#publish-local').textContent='保存为新版本';
  };}
};
const methodTransferSessions=new Map();
let activeMethodTransfer=null;

function renderMethodTransfer(c){
  if(activeMethodTransfer!==c)return;
  const upload=c.direction==='upload',verb=upload?'上传':'下载';
  q('#method-transfer-title').textContent=verb;
  q('#method-transfer-detail').innerHTML=`<p>来源／发布目标：${esc(c.destination||'读取配置中…')}</p>
    <p>${esc(c.note||'')}</p><p role="status">${c.status==='loading'?'正在读取并核验版本清单…':esc(c.error)}</p>
    <div class="transfer-table"><table class="table"><thead><tr><th>选择</th><th>方法名称／版本</th><th>作者</th><th>修改领域／说明</th><th>创建时间</th><th>${upload?'远端状态':'本地状态'}</th></tr></thead>
    <tbody>${c.rows.map(r=>{const result=c.results.get(r.key);return `<tr><td><input type="checkbox" class="transfer-check" data-key="${esc(r.key)}" ${c.selected.has(r.key)?'checked':''} ${!r.eligible||c.busy||c.status!=='ready'?'disabled':''}></td>
      <td>${esc(r.method_name||r.label||r.version_id)}<br><small>${esc(r.version_id)}</small>${r.content_sha256?`<details><summary>内容 ${esc(r.content_sha256.slice(0,12))}</summary><code class="transfer-hash">${esc(r.content_sha256)}</code></details>`:'<small>内容身份不可用</small>'}</td>
      <td>${esc(r.author)}${(r.source_aliases||[]).length?`<details><summary>来源记录</summary>${r.source_aliases.map(a=>`<p>${esc(a.author)} / ${esc(a.version_id)} · ${esc(a.branch)}${a.legacy_source?'（旧来源）':''}</p>`).join('')}</details>`:''}</td>
      <td>${esc((r.changed_domains||[]).join('、'))}<br>${esc(r.description||'')}</td><td>${esc(r.created_at||'未记录')}${r.published_at?`<br>发布：${esc(r.published_at)}`:''}</td><td role="status">${esc(result?.message||r.local_status||'')}</td></tr>`}).join('')||`<tr><td colspan="6">${c.status==='ready'?'没有可列出的已保存版本':'清单尚未可用'}</td></tr>`}</tbody></table></div>
    <div class="run-actions"><button class="secondary" id="transfer-select-all" ${c.busy||c.status!=='ready'?'disabled':''}>全选可用版本</button>
    <button class="secondary" id="transfer-refresh" ${c.busy||c.status==='loading'?'disabled':''}>${c.status==='failed'?'重试读取清单':'刷新清单'}</button>
    <button class="primary" id="transfer-confirm" ${c.busy||!c.selected.size||c.status!=='ready'?'disabled':''}>${verb}选中版本</button></div>
    <p>失败项保留选择，可再次点击重试；成功项不会重复传输。</p>`;
  qa('.transfer-check').forEach(box=>box.onchange=()=>c.select(box.dataset.key,box.checked));
  q('#transfer-select-all').onclick=()=>c.selectAll();
  q('#transfer-refresh').onclick=()=>c.open();
  q('#transfer-confirm').onclick=async()=>{await c.submit();if(c.results.size)await load(false);};
}

async function openMethodTransfer(direction){
  let c=methodTransferSessions.get(direction);
  if(!c){c=SHAQTransfer.controller(direction,(...args)=>api(...args),renderMethodTransfer);methodTransferSessions.set(direction,c);}
  activeMethodTransfer=c;
  const modal=q('#method-transfer-modal');
  const close=()=>{modal.close();activeMethodTransfer=null;};
  q('#method-transfer-close').onclick=close;
  modal.oncancel=event=>{event?.preventDefault();close();};
  renderMethodTransfer(c);if(!modal.open)modal.showModal();
  await c.open();
}

async function saveMethodDraft(){
  const id=q('#draft-id').value.trim();
  if(!id)return notice('请先新建本地草稿',true);
  try{
    const profile={display_name:q('#agent-display-name').value,short_description:q('#agent-description').value,
      default_prompt:q('#agent-prompt').value,
      allow_implicit_invocation:state.editorDocument.package.agent_profile.allow_implicit_invocation};
    await api('save_skill_package_draft',q('#edit-skill').value,q('#skill-method').value,q('#skill-foundations').value,profile,id);
    wb.draft=id;notice('领域修改已保存到本地草稿；完成后请保存为新版本');
  }catch(error){notice(error.message,true);}
}

if(typeof saveDraft==='function')saveDraft=saveMethodDraft;
if(typeof renderEditor==='function')renderEditor=SHAQTransfer.preserveEditor(renderEditor);
