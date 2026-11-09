'use strict';
function syncMaterialProgress(cell,w,key){
  const el=cell.querySelector('.material-progress');if(!el||!['working','queued'].includes(w[key]))return false;
  const fraction=w[key+'Progress'],known=knownNumber(fraction),percent=known?Math.min(99,Math.floor(fraction*100)):null;
  const label=w[key]==='queued'?'等待下载':known&&fraction===1?'正在保存':known?percent+'%':'下载中';
  el.classList.toggle('indeterminate',!known);el.setAttribute('aria-label',(key==='media'?'音视频':'封面')+'：'+label);
  el.querySelector('span').textContent=label;
  if(known){el.setAttribute('aria-valuemin','0');el.setAttribute('aria-valuemax','100');el.setAttribute('aria-valuenow',percent);el.querySelector('i').style.transform='scaleX('+(percent/100)+')';}
  else for(const name of ['aria-valuemin','aria-valuemax','aria-valuenow'])el.removeAttribute(name);
  return true;
}
function refreshProgress(w){const row=rowNodes.get(w.id);if(row?.isConnected)updateRow(w,row);}
function materialMarkup(w,key){
  const state=w[key],name=key==='media'?'音视频':'封面';
  if(key==='media'&&w.kind==='image'||['unavailable','privacy'].includes(state))return '<span class="asset-unavailable" title="'+esc(w[key+'Reason']||'')+'">'+(w.kind==='image'&&key==='media'?'图文':state==='privacy'?'隐私跳过':'不可用')+'</span>';
  if(['working','queued'].includes(state)){
    const fraction=w[key+'Progress'],known=knownNumber(fraction),percent=known?Math.min(99,Math.floor(fraction*100)):null,label=state==='queued'?'等待下载':known&&fraction===1?'正在保存':known?percent+'%':'下载中';
    return '<div class="material-progress '+(!known?'indeterminate':'')+'" role="progressbar" aria-label="'+esc(name+'：'+label)+'" '+(known?'aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+percent+'"':'')+'><span>'+label+'</span><div class="material-track"><i style="transform:scaleX('+(known?percent/100:1)+')"></i></div></div>';
  }
  const ready=state==='ready',label=ready?'查看':'下载'+name;
  const hint=ready?(key==='cover'&&w.coverCanRedownload?'查看封面；右键可重新下载高清封面':'查看已保存的'+name):state==='failed'?'下载未完成，点击只重试这项素材':'下载这条作品的'+name;
  return '<button class="asset-status '+(ready?'ready':'material-download')+' '+(state==='failed'?'failed':'')+'" data-asset="'+key+'" data-action="'+(ready?'view':'download')+'" aria-label="'+esc(label+'：'+w.title)+'" title="'+hint+'"><i>'+icon(ready?'file':'download')+'</i><span'+(ready?'':' class="sr-only"')+'>'+label+'</span></button>';
}
