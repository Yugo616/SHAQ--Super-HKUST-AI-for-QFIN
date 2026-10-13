// Patch the live progress region while retaining its controls and disclosure state.
const SHAQDOM=(()=>{
  const key=node=>node.nodeType===1?(node.id||['progressJob','progressVariant','viewKey','researchSection'].map(name=>node.dataset[name]||'').join('|')):'';
  const matches=(left,right)=>left.nodeType===right.nodeType&&left.nodeName===right.nodeName&&key(left)===key(right);
  function patch(target,source){
    let cursor=target.firstChild;
    for(const next of [...source.childNodes]){
      let current=cursor;
      if(!current||!matches(current,next)){
        current=[...target.childNodes].find(node=>matches(node,next)&&key(next).replaceAll('|',''));
        if(!current)current=next.cloneNode(true);
        target.insertBefore(current,cursor);
      }
      if(current.nodeType===1){
        for(const attr of [...current.attributes]){
          if(attr.name==='open'&&current.tagName==='DETAILS')continue;
          if(!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
        }
        for(const attr of [...next.attributes]){
          if(attr.name==='open'&&current.tagName==='DETAILS')continue;
          if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
        }
        patch(current,next);
      }else if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;
      cursor=current.nextSibling;
    }
    while(cursor){const next=cursor.nextSibling;cursor.remove();cursor=next;}
  }
  function update(target,html){
    const template=document.createElement('template');template.innerHTML=html;
    patch(target,template.content);
  }
  return {update};
})();
