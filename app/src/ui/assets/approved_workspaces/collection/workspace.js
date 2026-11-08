/* Approved collection focus transition. */
'use strict';
window.WorkspaceUI=(()=>{
  const q=s=>document.querySelector(s);
  const region=q('#intakeRegion'),content=q('#intakeContent'),workspace=q('#workspace'),button=q('#focusToggle');
  let focused=false,animations=[],sequence=0;
  const card=q('#workspace>.results-card'),paper=q('.results-paper'),scroller=q('#tableScroller'),table=q('#worksTable');
  const empty=q('#emptyState'),emptyHeading=q('#emptyState h2');
  const movingLayers=[q('.results-top'),q('.filter-panel'),scroller];
  const motionNodes=[region,card,paper,...movingLayers,q('.selection-bar')];
  const motionClasses=active=>motionNodes.forEach(el=>el.classList.toggle('focus-plane-moving',active));
  let normalScroll=null,focusScroll=null,scrollIdentity='';
  const identity=()=>table.dataset.record||'';
  function setFocus(next,{immediate=false}={}){
    const scale=1;
    const wasMoving=animations.length>0;
    if(next!==focused){LuluSelect.close({immediate:true});document.querySelectorAll('[popover]:popover-open').forEach(pop=>pop.hidePopover());}
    const beforePaper=paper.getBoundingClientRect(),beforeTable=table.getBoundingClientRect(),beforeEmpty=emptyHeading.getBoundingClientRect();
    const beforeLayers=movingLayers.map(el=>el.getBoundingClientRect());
    const beforeContent=getComputedStyle(content),opacity=beforeContent.opacity,contentTransform=beforeContent.transform;
    if(next&&!focused&&!wasMoving){normalScroll=scroller.scrollTop;scrollIdentity=identity();}
    const desiredScroll=!next&&normalScroll!==null&&identity()===scrollIdentity&&Math.abs(scroller.scrollTop-focusScroll)<1?normalScroll:scroller.scrollTop;
    let liveViewport=false;
    const token=++sequence;
    animations.forEach(animation=>animation.cancel());animations=[];
    movingLayers.forEach(el=>el.style.removeProperty('transform'));table.style.removeProperty('transform');paper.style.removeProperty('transform');
    motionClasses(false);empty.style.removeProperty('transform');empty.style.removeProperty('height');empty.style.removeProperty('bottom');
    for(const name of ['top','height','padding-bottom'])scroller.style.removeProperty(name);
    focused=next;button.setAttribute('aria-pressed',String(next));button.querySelector('span').textContent=next?'退出放大':'放大查看';
    workspace.dataset.focused=String(next);syncOperation();
    region.inert=next;region.setAttribute('aria-hidden',String(next));region.style.height=next?'0px':'auto';
    const targetPaper=paper.getBoundingClientRect(),targetLayers=movingLayers.map(el=>el.getBoundingClientRect());
    const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches||document.body.classList.contains('keyboard-mode')||document.body.classList.contains('reduce-motion');
    const finish=()=>{
      if(token!==sequence)return;
      const settledScroll=liveViewport?scroller.scrollTop:desiredScroll;
      content.style.opacity=next?'0':'1';content.style.transform=next?'translateY(-10px)':'none';
      movingLayers.forEach(el=>el.style.removeProperty('transform'));table.style.removeProperty('transform');paper.style.removeProperty('transform');
      animations.forEach(animation=>animation.cancel());animations=[];
      motionClasses(false);empty.style.removeProperty('transform');empty.style.removeProperty('height');empty.style.removeProperty('bottom');
      for(const name of ['top','height','padding-bottom'])scroller.style.removeProperty(name);
      scroller.scrollTop=settledScroll;
      if(next)focusScroll=scroller.scrollTop;else{normalScroll=null;focusScroll=null;}
      workspace.dataset.focusAnimating='false';
    };
    const delta=(beforePaper.top-targetPaper.top)/scale;
    if(immediate||reduce||Math.abs(delta)<.5||workspace.hidden){finish();return;}
    // Keep the larger viewport during movement. Bottom padding preserves the
    // final scroll range, so rows never jump when a near-bottom view resizes.
    const targetHeight=targetLayers[2].height/scale;
    const height=Math.max(beforeLayers[2].height/scale,targetHeight);
    scroller.style.setProperty('top',scroller.offsetTop+'px');
    scroller.style.setProperty('height',height+'px');
    scroller.style.setProperty('padding-bottom',Math.max(0,height-targetHeight)+'px');
    motionClasses(true);scroller.scrollTop=desiredScroll;liveViewport=true;
    if(next)focusScroll=scroller.scrollTop;
    if(!empty.hidden){empty.style.bottom='auto';empty.style.height=Math.max(0,targetHeight-40)+'px';}
    const adjustedTable=table.getBoundingClientRect();
    const distance=Math.min(1,Math.abs(delta)/Math.max(content.offsetHeight,1));
    const base=parseFloat(getComputedStyle(workspace).getPropertyValue('--focus-slide'))||280;
    const duration=Math.max(120,base*Math.sqrt(distance));
    const easing=getComputedStyle(workspace).getPropertyValue('--ease-out').trim();
    const move=(element,from,to)=>element.animate([{transform:from},{transform:to}],{duration,easing,fill:'both'});
    workspace.dataset.focusAnimating='true';
    const paperAnimation=move(paper,`translateY(${delta}px) scaleY(${beforePaper.height/targetPaper.height})`,'translateY(0) scaleY(1)');
    const layerAnimations=movingLayers.map((el,index)=>move(el,`translateY(${(beforeLayers[index].top-targetLayers[index].top)/scale}px)`,'translateY(0)'));
    const outerDelta=(beforeLayers[2].top-targetLayers[2].top)/scale;
    const innerDelta=(beforeTable.top-adjustedTable.top)/scale-outerDelta;
    const tableAnimation=Math.abs(innerDelta)>.05?move(table,`translateY(${innerDelta}px)`,'translateY(0)'):null;
    const emptyDelta=(beforeEmpty.top-emptyHeading.getBoundingClientRect().top)/scale-outerDelta;
    const emptyAnimation=!empty.hidden&&Math.abs(emptyDelta)>.05?move(empty,`translateY(${emptyDelta}px)`,'translateY(0)'):null;
    const contentAnimation=content.animate([{opacity,transform:contentTransform==='none'?'translateY(0)':contentTransform},{opacity:next?0:1,transform:next?'translateY(-10px)':'translateY(0)'}],{duration:next?140:Math.min(180,duration),delay:!next&&Number(opacity)<.05?Math.min(70,duration*.2):0,easing,fill:'both'});
    animations=[paperAnimation,...layerAnimations,...(tableAnimation?[tableAnimation]:[]),...(emptyAnimation?[emptyAnimation]:[]),contentAnimation];paperAnimation.onfinish=finish;
  }
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',event=>{if(event.matches&&animations.length)setFocus(focused,{immediate:true});});
  function syncOperation(){const active=focused&&!q('#collectStop').hidden;q('#focusOperation').hidden=!active;if(active){const text=q('#collectStatus').textContent,match=text.match(/(?:已找到|已读取)\s*(\d+)/);q('#focusOperation span').textContent=match?'已取得 '+match[1]+' 条':'正在采集';q('#focusOperation').title=text;}}
  q('#focusStop').onclick=()=>q('#collectStop').click();
  LuluSelect.setFooter(q('#dateMode'),q('#customRange'),{width:344,ownedPopups:['#startCalendar','#endCalendar']});
  function syncFilter(){
    const state=window.CollectionFilters?.snapshot(),active=!!state?.condition;
    q('#clearCondition').hidden=!active;
    const field=q('#fieldName').selectedOptions[0];
    q('#conditionField').textContent=field?.textContent||'';
    const wrapper=q('[data-select-for=fieldName]');if(wrapper)wrapper.dataset.active=String(active);
    const date=q('[data-select-for=dateMode] .select-trigger');if(date)date.title=q('#dateMode').title;
    if(!q('#fieldName').value&&q('#filterDetails').matches(':popover-open'))q('#filterDetails').hidePopover();
    if(q('#filterDetails').matches(':popover-open'))positionPopover(wrapper,q('#filterDetails'));
  }
  let conditionPending=false;
  function openCondition(){
    if(conditionPending)return;conditionPending=true;
    queueMicrotask(()=>{conditionPending=false;
    const pop=q('#filterDetails');LuluSelect.close({immediate:true});
    if(!q('#fieldName').value){if(pop.matches(':popover-open'))pop.hidePopover();return;}
    const anchor=q('[data-select-for=fieldName]');positionPopover(anchor,pop,true);pop.showPopover();
    if(!q('#fieldValue').hidden)q('#fieldValue').focus({preventScroll:true});
    });
  }
  q('#fieldName').addEventListener('change',openCondition);
  q('#fieldName').addEventListener('selectactivate',openCondition);
  q('#filterDetails').addEventListener('keydown',event=>{if(event.key==='Escape'&&!document.querySelector('.select-menu')){event.preventDefault();q('#filterDetails').hidePopover();q('[data-select-for=fieldName] .select-trigger').focus({preventScroll:true});}});
  q('#clearCondition').onclick=()=>{q('#fieldName').value='';q('#fieldName').dispatchEvent(new Event('change',{bubbles:true}));};
  function reset(){setFocus(false,{immediate:true});if(q('#filterDetails').matches(':popover-open'))q('#filterDetails').hidePopover();syncFilter();}
  return {toggleFocus:()=>setFocus(!focused),setFocus,reset,syncFilter,syncOperation,state:()=>({focused,animating:animations.length>0})};
})();
