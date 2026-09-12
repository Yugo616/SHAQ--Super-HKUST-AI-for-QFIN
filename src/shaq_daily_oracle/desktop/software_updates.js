async function checkSoftwareUpdate() {
  const target=q('#software-update-detail');
  q('#software-update-modal').showModal();
  if(target.dataset.checking==='true')return;
  target.dataset.checking='true';
  target.innerHTML='<p role="status">正在检查这台电脑适用的软件版本…</p>';
  try {
    const value=await api('check_software_update');
    const text={available:'有新版本可下载',current:'已是最新发布版',local_newer:'本机版本比当前公开安装包更新',
      no_release:'还没有适合这台电脑的安装包',unsupported:'暂不提供此平台安装包',unknown_local_version:'无法识别本机版本'};
    target.innerHTML=`<h3>${text[value.status]||'检查完成'}</h3><p>${esc(value.platform)} · 当前 ${esc(value.current_version)}${value.latest_version?` · 最新发布 ${esc(value.latest_version)}${value.internal_test_release?'（内部测试版）':''}`:''}</p>
      <p>方法通过「上传版本／下载团队版本」更新，无需重装软件。本次软件更新使用完整安装包；安装前请等待分析和结算结束，关闭应用后再安装。</p>
      ${value.status==='available'?'<button class="primary" id="download-software">下载此电脑适用的安装包</button>':''}
      <details><summary>发布说明</summary><pre class="release-notes">${esc(value.notes||'暂无发布说明')}</pre></details>
      <button class="secondary" id="retry-software-check">重新检查</button>`;
    if(q('#download-software'))q('#download-software').onclick=async()=>{
      try{await api('open_software_release');}catch(error){notice(error.message,true);}
    };
    q('#retry-software-check').onclick=checkSoftwareUpdate;
  } catch(error) {
    target.innerHTML=`<p role="alert">未能检查更新：${esc(error.message)}</p><button id="retry-software-check">重试</button>`;
    q('#retry-software-check').onclick=checkSoftwareUpdate;
  } finally {target.dataset.checking='false';}
}
document.querySelector('#software-update-button').onclick=checkSoftwareUpdate;
