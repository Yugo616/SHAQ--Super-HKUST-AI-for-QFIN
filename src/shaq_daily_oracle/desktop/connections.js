/* Connection feedback is presentation only. The backend performs a real schema probe. */
const SHAQConnections = {
  diagnosticMessage(diagnostic, fallback) {
    if(!diagnostic)return fallback || '连接失败，请重新检测。';
    if(diagnostic.kind==='timeout')return '连接超时。请检查网络与服务地址后重新检测。';
    const parts=[diagnostic.status?`HTTP ${diagnostic.status}`:'请求失败'];
    if(diagnostic.provider_code)parts.push(`服务代码：${diagnostic.provider_code}`);
    if(diagnostic.provider_message)parts.push(`服务信息：${diagnostic.provider_message}`);
    if(diagnostic.request_id)parts.push(`请求编号：${diagnostic.request_id}`);
    return parts.join(' · ');
  },
  bindProviderDrafts(form, applyPreset) {
    const names=['model','secret','base_url','relay_base_url','auth_style','output_mode','maximum_context_tokens'];
    const drafts=new Map();
    let current=form.elements.protocol.value;
    const capture=()=>Object.fromEntries(names.map(name=>[name,form.elements[name]?.value??'']));
    const restore=value=>names.forEach(name=>{if(form.elements[name])form.elements[name].value=value?.[name]??''});
    drafts.set(current,capture());
    form.elements.protocol.onchange=()=>{
      drafts.set(current,capture());
      current=form.elements.protocol.value;
      restore(null);
      applyPreset();
      if(drafts.has(current))restore(drafts.get(current));
      else drafts.set(current,capture());
    };
    return drafts;
  },
  controller(invoke, show) {
    let busy = false;
    return {
      get busy() { return busy; },
      async test(profile, secret) {
        if (busy) return false;
        busy = true;
        show({status:'testing', message:'正在检测程序与登录，并测试模型能否返回合格结果…'});
        try {
          await invoke(profile, secret, true);
          show({status:'connected', message:'连接测试通过，已保存。'});
          return true;
        } catch (error) {
          show({status:'failed', diagnostic:error?.diagnostic,
            message:SHAQConnections.diagnosticMessage(error?.diagnostic,error?.message)});
          return false;
        } finally { busy = false; }
      }
    };
  }
};

function showConnectionState(value) {
  const status=q('#model-status'), actions=q('#model-error-actions');
  status.textContent=value.message;
  status.dataset.status=value.status;
  actions.classList.toggle('hidden',value.status!=='failed');
  for(const button of qa('#model-step button:not([data-install-model])')) {
    button.disabled=value.status==='testing';
  }
}

let modelConnectionController;
let lastConnectionProtocol;
function connectionController() {
  if(!modelConnectionController)modelConnectionController=SHAQConnections.controller(
    (profile,secret,test)=>api('save_lab_model_profile',profile,secret,test),showConnectionState);
  return modelConnectionController;
}

async function connectLocalModel(protocol) {
  lastConnectionProtocol=protocol;
  const profile={profile_id:protocol==='codex-cli'?'my-codex':'my-claude',protocol,
    base_url:'',model:'subscription-default',auth_style:'bearer',output_mode:'strict',
    timeout_seconds:180,maximum_output_tokens:12000,maximum_context_tokens:128000,
    max_concurrency:2,rate_limit_per_minute:30,reasoning_effort:'high',
    input_price_per_million:null,output_price_per_million:null};
  if(await connectionController().test(profile,''))await load(false);
}

async function copyConnectionError() {
  const text=q('#model-status').textContent;
  try {
    if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(text);
    else {
      const field=document.createElement('textarea');field.value=text;
      q('#setup .setup-card').append(field);field.select();
      const copied=document.execCommand('copy');field.remove();
      if(!copied)throw new Error('请选中上方错误文字复制。');
    }
    q('#copy-model-error').textContent='已复制';
  } catch(error) { notice(error.message || '请选中上方错误文字复制。',true); }
}

function bindModelConnections() {
  const form=q('#model-form');
  q('#connect-codex').onclick=()=>connectLocalModel('codex-cli');
  q('#connect-claude').onclick=()=>connectLocalModel('claude-code');
  q('#show-api-form').onclick=()=>{
    form.classList.remove('hidden');applyProtocolPreset();
  };
  q('#retry-model').onclick=()=>{
    if(['codex-cli','claude-code'].includes(lastConnectionProtocol))connectLocalModel(lastConnectionProtocol);
    else form.requestSubmit();
  };
  q('#edit-model').onclick=()=>{
    if(['codex-cli','claude-code'].includes(lastConnectionProtocol))q('#model-step').scrollIntoView({block:'start'});
    else {form.classList.remove('hidden');form.elements.model.focus();}
  };
  q('#copy-model-error').onclick=copyConnectionError;
  qa('[data-install-model]').forEach(button=>button.onclick=async()=>{
    try {await api('open_model_installation',button.dataset.installModel);}
    catch(error){notice(error.message,true);}
  });
  SHAQConnections.bindProviderDrafts(form,applyProtocolPreset);
  form.onsubmit=async event=>{
    event.preventDefault();
    const profile=Object.fromEntries(new FormData(form).entries()),secret=profile.secret;
    delete profile.secret;
    lastConnectionProtocol=profile.protocol;
    if(profile.protocol==='openai-chat-completions')profile.base_url=profile.relay_base_url.trim();
    delete profile.relay_base_url;
    const optional=value=>String(value??'').trim()===''?null:Number(value);
    Object.assign(profile,{timeout_seconds:180,maximum_output_tokens:12000,
      maximum_context_tokens:Number(profile.maximum_context_tokens||128000),
      max_concurrency:Number(profile.max_concurrency),rate_limit_per_minute:Number(profile.rate_limit_per_minute),
      reasoning_effort:'high',input_price_per_million:optional(profile.input_price_per_million),
      output_price_per_million:optional(profile.output_price_per_million)});
    if(await connectionController().test(profile,secret)) {
      form.elements.secret.value='';await load(false);q('#setup').classList.remove('hidden');
    }
  };
}
