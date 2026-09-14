let softwareUpdateTimer;
function renderSoftwareUpdate(value) {
  const target=q('#software-update-detail');
  const labels={available:'有新版本可下载',current:'已是最新发布版',local_newer:'本机版本比公开安装包更新',
    no_release:'还没有适合这台电脑的安装包',unsupported:'暂不提供此平台安装包',unknown_local_version:'无法识别本机版本',
    downloading:'正在后台下载并校验',ready:'更新已准备就绪',applying:'正在更新并重启',download_failed:'下载或校验失败'};
  const managed=value.mode==='managed';
  const timestamp=value=>value?`<time datetime="${esc(value)}">${esc(new Date(value).toLocaleString())}</time>`:'暂无记录';
  const size=bytes=>`${(Number(bytes||0)/1048576).toFixed(1)} MB`;
  const signature=JSON.stringify(value);
  if(target.dataset.renderedValue!==signature){
  const expanded=target.querySelector('details')?.open===true;
  const focus=target.contains(document.activeElement)?document.activeElement.id:null;
  const scroll=target.scrollTop;
  const previousError=target.querySelector('#software-update-error')?.textContent||'';
  target.innerHTML=`<h3>${labels[value.status]||'检查完成'}</h3><p>${esc(value.platform||'')} · 当前 ${esc(value.current_version)}${value.latest_version?` · 最新 ${esc(value.latest_version)}${value.internal_test_release?'（内部测试版）':''}`:''}</p>
    <p>${esc(value.waiting_for_idle?'已下载，等待本地任务运行完更新':value.message||'旧安装仅提供完整安装包：请等待分析和结算结束，关闭应用后安装。')}</p>
    ${value.target_version?`<p>更新目标版本：${esc(value.target_version)}</p>`:''}
    <label class="check-row"><input type="checkbox" id="automatic-software-update" ${value.automatic_enabled?'checked':''}>自动软件更新（默认关闭）</label>
    <p>仅在应用打开时自动检查、下载，并等待所有本地任务结束后更新重启；不会更改自动预测开关、模型或方法包。</p>
    <p>最近检查：${timestamp(value.last_checked_at)}<br>上次成功更新：${value.last_update?`${esc(value.last_update.version)} · ${value.last_update.method==='automatic'?'自动':'手动'} · ${timestamp(value.last_update.completed_at)}`:'暂无已确认记录'}</p>
    ${managed?`<p>预计下载 ${size(value.download_size_bytes)}；完整包 ${size(value.size_bytes)}</p>`:''}
    ${value.status==='downloading'?`<progress max="100" value="${Number(value.progress)||0}"></progress><span>${Number(value.progress)||0}%</span>`:''}
    ${['available','download_failed'].includes(value.status)?`<button class="primary" id="download-software">${managed?'下载更新':'下载完整安装包（首次接入更新）'}</button>`:''}
    ${value.status==='ready'?'<button class="primary" id="apply-software">更新并重启</button>':''}
    ${value.status==='ready'&&value.queued_apply_method==='manual'?'<button id="cancel-software-wait">取消本次等待</button>':''}
    <p id="software-update-error" role="alert"></p>
    <details><summary>发布说明</summary><pre class="release-notes">${esc(value.notes||'暂无发布说明')}</pre></details>
    ${!['downloading','applying'].includes(value.status)?'<button class="secondary" id="retry-software-check">重新检查</button>':''}`;
  target.dataset.renderedValue=signature;
  target.querySelector('details').open=expanded;
  target.querySelector('#software-update-error').textContent=previousError;
  target.scrollTop=scroll;
  if(focus)document.getElementById(focus)?.focus({preventScroll:true});
  if(['available','download_failed'].includes(value.status))q('#download-software').onclick=async()=>{
    try {
      if(!managed){await api('open_software_release');return;}
      renderSoftwareUpdate(await api('download_software_update'));
    } catch(error){q('#software-update-error').textContent=error.message;}
  };
  q('#automatic-software-update').onchange=async()=>{
    try{renderSoftwareUpdate(await api('set_automatic_software_update',q('#automatic-software-update').checked));}
    catch(error){q('#automatic-software-update').checked=Boolean(value.automatic_enabled);q('#software-update-error').textContent=error.message;}
  };
  if(value.status==='ready')q('#apply-software').onclick=async()=>{
    try {renderSoftwareUpdate(await api('apply_software_update'));}
    catch(error){q('#software-update-error').textContent=error.message;}
  };
  if(value.status==='ready'&&value.queued_apply_method==='manual')q('#cancel-software-wait').onclick=async()=>{
    try{renderSoftwareUpdate(await api('cancel_queued_software_update'));}
    catch(error){q('#software-update-error').textContent=error.message;}
  };
  if(!['downloading','applying'].includes(value.status))q('#retry-software-check').onclick=checkSoftwareUpdate;
  }
  clearTimeout(softwareUpdateTimer);
  if(q('#software-update-modal').open&&(value.status==='downloading'||value.waiting_for_idle||value.automatic_enabled))softwareUpdateTimer=setTimeout(pollSoftwareUpdate,700);
}
async function pollSoftwareUpdate() {
  if(!q('#software-update-modal').open||q('#software-update-detail').dataset.checking==='true')return;
  try {renderSoftwareUpdate(await api('software_update_status'));}
  catch(error){q('#software-update-error').textContent=error.message;softwareUpdateTimer=setTimeout(pollSoftwareUpdate,2000);}
}
async function checkSoftwareUpdate() {
  const target=q('#software-update-detail');
  q('#software-update-modal').showModal();
  if(target.dataset.checking==='true')return;
  target.dataset.checking='true';
  // Keep an already-open explanation visible while checking in the background.
  if(!target.dataset.renderedValue)target.innerHTML='<p role="status">正在检查这台电脑适用的软件版本…</p>';
  try {
    renderSoftwareUpdate(await api('check_software_update'));
  } catch(error) {
    try {
      renderSoftwareUpdate(await api('software_update_status'));
      q('#software-update-error').textContent=`未能检查更新：${error.message}`;
    } catch(localError) {
      target.innerHTML=`<p role="alert">未能读取更新状态：${esc(localError.message)}</p><button id="retry-software-check">重试</button>`;
      q('#retry-software-check').onclick=checkSoftwareUpdate;
    }
  } finally {target.dataset.checking='false';}
}
document.querySelector('#software-update-button').onclick=checkSoftwareUpdate;
document.querySelector('#software-update-modal').addEventListener('close',()=>clearTimeout(softwareUpdateTimer));
