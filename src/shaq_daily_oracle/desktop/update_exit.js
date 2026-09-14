/* No field values or secrets leave the window: only dirty-group generations. */
window.SHAQUpdateExit=(()=>{
  const groups=new Map();let generation=0;
  const dirty=group=>groups.set(group,++generation);
  const groupForSave=(name,args)=>({
    save_lab_model_profile:()=>`connection:${args[0].protocol}`,
    save_lab_setup:()=>args[0].data_profile?'data':'research',
    save_skill_package_draft:()=>`skill:${args[0]}`,
    save_module_draft:()=>`module:${args[0]}`,
    save_decision_draft:()=> 'decision',
    finalize_local_version:()=> 'version-description'
  }[name]?.());
  const changed=event=>{
    const field=event.target;
    if(field.closest?.('#model-form')){
      if(field.name!=='protocol')dirty(`connection:${document.querySelector('#model-form').elements.protocol.value}`);
    }else if(field.closest?.('#data-form'))dirty('data');
    else if(field.closest?.('#research-form'))dirty('research');
    else if(field.closest?.('#editor')){
      if(['skill-method','skill-foundations','agent-display-name','agent-description','agent-prompt','agent-policy'].includes(field.id))dirty(`skill:${document.querySelector('#edit-skill').value}`);
      else if(['module-code','module-cases'].includes(field.id))dirty(`module:${wb.selectedModule}`);
      else if(['decision-script','decision-cases'].includes(field.id))dirty('decision');
      else if(field.id==='version-description')dirty('version-description');
    }
  };
  document.addEventListener('input',changed,true);
  document.addEventListener('change',changed,true);
  return {
    dirty,
    beforeSave(name,args){const group=groupForSave(name,args);return {group,generation:groups.get(group)}},
    afterSave(snapshot){if(snapshot?.group&&groups.get(snapshot.group)===snapshot.generation)groups.delete(snapshot.group)},
    prepare(){document.body.inert=true;return groups.size===0},
    cancel(){document.body.inert=false}
  };
})();
