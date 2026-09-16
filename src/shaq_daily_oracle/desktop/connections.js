/* Connection feedback is presentation only. The backend performs a real schema probe. */
const providerDraftFields=['model','secret','base_url','relay_base_url','auth_style','output_mode','maximum_context_tokens'];
const providerDraftControllers=new WeakMap();
const SHAQConnections = {
  diagnosticMessage(diagnostic, fallback) {
    if(!diagnostic)return fallback || '连接失败，请重新检测。';
    const local=diagnostic.protocol||diagnostic.login_protocol;
    if(local==='codex-cli'||local==='claude-code') {
      const messages={
        executable_missing:'未找到本机模型程序。请先安装后重新检测。',
        executable_unusable:'本机模型程序无法使用。请重新安装或检查系统设置后重试。',
        authentication:'本机模型尚未登录。请点击登录后重新检测。',
        status_invalid:'无法确认本机模型登录状态。请重试或重新登录。',
        timeout:'本机模型登录状态检查超时。请重试。',
        login_timeout:'登录操作超时或已取消；原有连接保持不变。',
        login_failed:'登录未完成或已取消；原有连接保持不变。'
      };
      return messages[diagnostic.kind]||'本机模型连接失败。请重新检测。';
    }
    if(diagnostic.kind==='timeout')return '连接超时。请检查网络与服务地址后重新检测。';
    const parts=[diagnostic.status?`HTTP ${diagnostic.status}`:'请求失败'];
    if(diagnostic.provider_code)parts.push(`服务代码：${diagnostic.provider_code}`);
    if(diagnostic.provider_message)parts.push(`服务信息：${diagnostic.provider_message}`);
    if(diagnostic.request_id)parts.push(`请求编号：${diagnostic.request_id}`);
    return parts.join(' · ');
  },
  bindProviderDrafts(form, applyPreset) {
    let controller=providerDraftControllers.get(form);
    const values=()=>Object.fromEntries(providerDraftFields.map(
      name=>[name,form.elements[name]?.value??'']));
    const restore=value=>providerDraftFields.forEach(
      name=>{if(form.elements[name])form.elements[name].value=value?.[name]??''});
    if(!controller) {
      controller={drafts:new Map(),current:form.elements.protocol.value,
        secretGenerations:new Map(),applyPreset};
      controller.capture=()=>controller.drafts.set(controller.current,values());
      controller.capture();
      providerDraftControllers.set(form,controller);
    } else {
      controller.current=form.elements.protocol.value;
      controller.capture();
      controller.applyPreset=applyPreset;
    }
    form.elements.protocol.onchange=()=>{
      controller.capture();
      controller.current=form.elements.protocol.value;
      restore(null);
      controller.applyPreset();
      if(controller.drafts.has(controller.current))restore(controller.drafts.get(controller.current));
      else controller.capture();
    };
    if(form.elements.secret)form.elements.secret.oninput=()=>{
      const protocol=form.elements.protocol.value;
      controller.secretGenerations.set(protocol,(controller.secretGenerations.get(protocol)||0)+1);
    };
    return controller.drafts;
  },
  captureSubmission(form, protocol, secret) {
    const controller=providerDraftControllers.get(form);
    if(controller) {
      controller.current=protocol;
      controller.capture();
    }
    return {protocol,secret,generation:controller?.secretGenerations.get(protocol)||0};
  },
  clearSubmittedSecret(form, drafts, submission) {
    const controller=providerDraftControllers.get(form);
    if(controller&&(controller.secretGenerations.get(submission.protocol)||0)!==submission.generation)return;
    const draft=drafts?.get(submission.protocol);
    if(draft?.secret===submission.secret)draft.secret='';
    if(form.elements.protocol.value===submission.protocol&&form.elements.secret.value===submission.secret) {
      form.elements.secret.value='';
    }
  },
  clearDraftSecret(drafts, protocol) {
    const draft=drafts?.get(protocol);
    if(draft)draft.secret='';
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
  const login=q('#login-model'),loginProtocol=value.diagnostic?.login_protocol||'';
  if(login?.dataset) {
    login.dataset.protocol=loginProtocol;
    login.textContent=loginProtocol==='codex-cli'?'登录 Codex':loginProtocol==='claude-code'?'登录 Claude Code':'登录本机模型';
    login.classList.toggle('hidden',!loginProtocol);
  }
  for(const button of qa('#model-step button:not([data-install-model])')) {
    button.disabled=value.status==='testing';
  }
}

let modelConnectionController;
let lastConnectionProtocol;
let lastLocalAction='catalog';
function connectionController() {
  if(!modelConnectionController)modelConnectionController=SHAQConnections.controller(
    (profile,secret,test)=>api('save_lab_model_profile',profile,secret,test),showConnectionState);
  return modelConnectionController;
}

const localModelDrafts=new Map();
let localModelCatalogRequest=0;
async function connectLocalModel(protocol) {
  lastLocalAction='catalog';
  lastConnectionProtocol=protocol;
  const form=q('#local-model-form');
  if(form.dataset.protocol)localModelDrafts.set(form.dataset.protocol,form.elements.model.value);
  const profiles=state.data?.settings?.model_profiles||[];
  const saved=profiles.find(p=>p.protocol===protocol&&p.profile_id===state.data?.settings?.active_model_profile_id)||profiles.find(p=>p.protocol===protocol);
  form.dataset.protocol=protocol;
  form.dataset.profileId=saved?.profile_id||(protocol==='codex-cli'?'my-codex':'my-claude');
  form.elements.model.value=localModelDrafts.get(protocol)??(saved?.model==='subscription-default'?'':saved?.model||'');
  form.classList.remove('hidden');q('#model-form').classList.add('hidden');
  q('#local-model-options').innerHTML='';
  const request=++localModelCatalogRequest;
  showConnectionState({status:'testing',message:'正在读取可选模型（不发起分析调用）…'});
  try {
    const value=await api('list_local_models',protocol);
    if(request!==localModelCatalogRequest)return;
    q('#local-model-options').innerHTML=(value.models||[]).map(m=>`<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
    showConnectionState({status:'choosing',message:'请选择明确型号，再点击测试并保存。读取列表不会消耗分析用量。'});
  } catch(error) {
    if(request!==localModelCatalogRequest)return;
    showConnectionState({status:'failed',diagnostic:error?.diagnostic,
      message:SHAQConnections.diagnosticMessage(error?.diagnostic,error?.message)});
  }
}

async function saveLocalModel() {
  lastLocalAction='save';
  const form=q('#local-model-form'),protocol=form.dataset.protocol,model=form.elements.model.value.trim();
  if(!['codex-cli','claude-code'].includes(protocol)||!model||['default','subscription-default'].includes(model.toLowerCase())) {
    showConnectionState({status:'choosing',message:'请先选择明确的分析模型，不会自动使用默认型号。'});return;
  }
  lastConnectionProtocol=protocol;
  const saved=(state.data?.settings?.model_profiles||[]).find(p=>p.profile_id===form.dataset.profileId);
  const profile={profile_id:form.dataset.profileId||(protocol==='codex-cli'?'my-codex':'my-claude'),protocol,
    base_url:'',model,auth_style:'bearer',output_mode:'strict',
    timeout_seconds:180,maximum_output_tokens:12000,maximum_context_tokens:128000,
    max_concurrency:2,rate_limit_per_minute:30,reasoning_effort:'high',
    input_price_per_million:null,output_price_per_million:null,...saved,model};
  if(await connectionController().test(profile,'')) {
    localModelDrafts.set(protocol,model);
    if(await refreshSavedModel(profile))
      showConnectionState({status:'connected',message:`已保存 ${model}，后续手动和自动运行使用此型号。`});
  }
}

let savedModelRefreshRequest=0;
async function refreshSavedModel(profile) {
  const request=++savedModelRefreshRequest;
  await load(false);
  if(request!==savedModelRefreshRequest)return false;
  // Explicit saves supersede a preserved old selector, but not time/version drafts.
  const saved=typeof state==='undefined'?[]:(state.data?.settings?.model_profiles||[]);
  const profiles=[...saved.filter(p=>p.profile_id!==profile.profile_id),profile];
  for(const selector of ['#run-profile','#auto-model']) {
    const select=q(selector);
    if(select?.tagName!=='SELECT')continue;
    select.innerHTML=profiles.map(p=>`<option value="${esc(p.profile_id)}">${esc(p.model)} · ${esc(p.profile_id)}</option>`).join('');
    select.value=profile.profile_id;
  }
  if(typeof estimate==='function')estimate();
  return true;
}

async function loginLocalModel() {
  const protocol=q('#login-model')?.dataset?.protocol;
  if(!['codex-cli','claude-code'].includes(protocol))return;
  showConnectionState({status:'testing',message:'正在打开本机浏览器登录…'});
  try {
    await api('begin_local_model_login',protocol);
    await connectLocalModel(protocol);
  } catch(error) {
    const diagnostic={...(error?.diagnostic||{}),login_protocol:protocol};
    showConnectionState({status:'failed',diagnostic,
      message:SHAQConnections.diagnosticMessage(diagnostic,error?.message)});
  }
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
  const clearApiModels=()=>{q('#api-model-options').innerHTML='';};
  const providerPreset=()=>{clearApiModels();applyProtocolPreset();};
  const apiIdentity=()=>JSON.stringify(['protocol','base_url','relay_base_url','auth_style','secret']
    .map(key=>form.elements[key]?.value||''));
  form.oninput=event=>{
    if(['protocol','base_url','relay_base_url','auth_style','secret'].includes(event.target?.name))clearApiModels();
  };
  form.onchange=form.oninput;
  const executionForm=q('#execution-policy-form');
  if(executionForm?.elements?.timeout_seconds){
    const policy=state.data?.settings?.model_execution_policy||{};
    if(!executionForm.dataset.loaded){
      executionForm.elements.timeout_seconds.value=policy.timeout_seconds??600;
      executionForm.elements.transient_retries.value=policy.transient_retries??1;
      executionForm.dataset.loaded='true';
    }
    executionForm.onsubmit=async event=>{
      event.preventDefault();
      try{await api('save_model_execution_policy',{
        timeout_seconds:Number(executionForm.elements.timeout_seconds.value),
        transient_retries:Number(executionForm.elements.transient_retries.value)});
        notice('调用设置已保存；模型与方法身份保持不变。');await load(false)}
      catch(error){notice(error.message,true)}
    };
  }
  q('#connect-codex').onclick=()=>connectLocalModel('codex-cli');
  q('#connect-claude').onclick=()=>connectLocalModel('claude-code');
  q('#local-model-form').onsubmit=event=>{event.preventDefault();saveLocalModel();};
  q('#refresh-local-models').onclick=()=>connectLocalModel(q('#local-model-form').dataset.protocol);
  q('#login-model').onclick=loginLocalModel;
  q('#show-api-form').onclick=()=>{
    localModelCatalogRequest++;
    q('#local-model-form').classList.add('hidden');
    let restored;
    if(!form.dataset.prefilled) {
      const settings=state.data?.settings||{};
      const profiles=(settings.model_profiles||[]).filter(p=>!['codex-cli','claude-code'].includes(p.protocol));
      const saved=profiles.find(p=>p.profile_id===settings.active_model_profile_id)||profiles[0];
      if(saved) {
        restored=saved;
        for(const key of ['profile_id','protocol','model','auth_style','output_mode','maximum_context_tokens','max_concurrency','rate_limit_per_minute']) {
          if(form.elements[key])form.elements[key].value=saved[key]??'';
        }
        form.elements.relay_base_url.value=saved.base_url||'';
      }
      providerPreset();
      if(restored)for(const key of ['auth_style','output_mode'])form.elements[key].value=restored[key];
      SHAQConnections.bindProviderDrafts(form,providerPreset);
      form.dataset.prefilled='true';
    }
    form.classList.remove('hidden');
  };
  q('#refresh-api-models').onclick=async()=>{
    const protocol=form.elements.protocol.value;
    const base_url=protocol==='openai-chat-completions'?form.elements.relay_base_url.value.trim():form.elements.base_url.value;
    const button=q('#refresh-api-models');button.disabled=true;
    const identity=apiIdentity();
    q('#api-model-options').innerHTML='';
    try {
      const value=await api('list_api_models',{profile_id:form.elements.profile_id.value,protocol,base_url,model:'catalog-only',
        auth_style:form.elements.auth_style.value},form.elements.secret.value);
      if(identity!==apiIdentity())return;
      q('#api-model-options').innerHTML=value.models.map(m=>`<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
      q('#model-status').textContent='列表已读取，请选择型号并测试保存。';
    }catch(error){if(identity===apiIdentity())q('#model-status').textContent=error.message;}
    finally{button.disabled=false;}
  };
  q('#retry-model').onclick=()=>{
    if(['codex-cli','claude-code'].includes(lastConnectionProtocol)) {
      if(lastLocalAction==='save')saveLocalModel();else connectLocalModel(lastConnectionProtocol);
    }
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
  const providerDrafts=SHAQConnections.bindProviderDrafts(form,providerPreset);
  form.onsubmit=async event=>{
    event.preventDefault();
    const profile=Object.fromEntries(new FormData(form).entries()),secret=profile.secret;
    const submission=SHAQConnections.captureSubmission(form,profile.protocol,secret);
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
      SHAQConnections.clearSubmittedSecret(form,providerDrafts,submission);
      if(await refreshSavedModel(profile))q('#setup').classList.remove('hidden');
    }
  };
}
