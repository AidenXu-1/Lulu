/* Preview adapter for the collection page's existing filter and ordering contract. */
'use strict';
window.CollectionFilters=(()=>{
  const C=window.CollectionContract,q=s=>document.querySelector(s),all=s=>[...document.querySelectorAll(s)];
  const initial=()=>({sort:3,desc:true,date:'all',start:C.today,end:C.today,condition:null,search:''});
  let state=initial(),change=()=>{};
  const fold=v=>String(v).toLowerCase().replace(/ß/g,'ss').replace(/ς/g,'σ');
  const known=v=>typeof v==='number'&&Number.isFinite(v);
  const fields=C._FILTER_FIELDS;
  const definition=()=>fields.find(f=>String(f[1]||'')===q('#fieldName').value)||fields[0];
  function fraction(value){const [base,exponent='0']=String(value).toLowerCase().split('e');const places=(base.split('.')[1]||'').length-Number(exponent);let n=BigInt(base.replace('.',''));return places>=0?[n,10n**BigInt(places)]:[n*10n**BigInt(-places),1n];}
  function numericCompare(actual,expected,multiplier=1){const [a,ad]=fraction(actual),[e,ed]=fraction(expected);const diff=a*ed-e*BigInt(multiplier)*ad;return diff<0?-1:diff>0?1:0;}
  function matches(item,condition){
    if(!condition)return true;
    const [field,type,op,expected]=condition;
    if(type==='numeric'){
      const value=item[field==='duration_sec'?'duration_ms':field];if(!known(value))return false;
      const cmp=numericCompare(value,expected,field==='duration_sec'?1000:1);
      return {gt:cmp>0,ge:cmp>=0,eq:cmp===0,ne:cmp!==0,lt:cmp<0,le:cmp<=0}[op];
    }
    const raw=item[field];if(raw==null||raw===''||(field==='content_kind'&&raw==='unknown'))return false;
    const value=fold(raw);
    if(type==='text')return {contains:value.includes(expected),not_contains:!value.includes(expected),eq:value===expected,ne:value!==expected}[op];
    return op==='eq'?value===expected:value!==expected;
  }
  function bounds(s){
    if(s.date==='all')return null;
    if(s.date==='custom')return [Date.parse(s.start+'T00:00:00+08:00')/1000,Date.parse(s.end+'T00:00:00+08:00')/1000+86400];
    const upper=Date.parse(C.today+'T00:00:00+08:00')/1000+86400;
    return [upper-Number(s.date)*86400,upper];
  }
  function filter(items,s=state){
    const range=bounds(s),keyword=s.search.trim().toLowerCase();
    const result=items.filter(item=>(!keyword||String(item.title||'').toLowerCase().includes(keyword)||String(item.author_name||'').toLowerCase().includes(keyword))&&(!range||(known(item.create_time)&&item.create_time>=range[0]&&item.create_time<range[1]))&&matches(item,s.condition));
    if(s.sort===0)return result;
    const attr=C._SORT_ATTR[s.sort],dir=s.desc?-1:1;
    return result.sort((a,b)=>{
      const x=a[attr],y=b[attr];if(attr!=='author_name'){if(!known(x))return known(y)?1:0;if(!known(y))return -1;}
      return (x<y?-1:x>y?1:0)*dir;
    });
  }
  function options(select,rows){select.replaceChildren(...rows.map(([label,value])=>new Option(label,String(value??''))));}
  function notify(){sync();change();}
  function error(message=''){q('#fieldValue').setAttribute('aria-invalid',String(!!message));q('#fieldValue').title=message;q('#filterError').textContent=message;}
  function applyEditor(){
    const [,field,type]=definition(),op=q('#fieldOperator').value;if(!field||type==='none')return;
    let value;
    if(type==='enum')value=q('#fieldEnum').value;
    else {
      const raw=q('#fieldValue').value.trim();if(!raw){error();state.condition=null;notify();return;}
      if(type==='numeric'){
        const normalized=raw.normalize('NFKC').replace(/[٠-٩۰-۹]/g,d=>String(d.charCodeAt(0)-(d<='٩'?0x660:0x6f0)));
        const decimal=/^[+-]?(?:(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?)|(?:\.\d(?:_?\d)*))(?:[eE][+-]?\d(?:_?\d)*)?$/;
        value=Number(normalized.replace(/_/g,''));
        if(!decimal.test(normalized)||!Number.isFinite(value)||value<0||(field!=='duration_sec'&&!/^\p{Decimal_Number}+$/u.test(raw))){error('请输入有效的非负数字，保留上一次有效筛选');return;}
      }else value=fold(raw);
    }
    error();state.condition=[field,type,op,value];notify();
  }
  function fieldChanged(){
    error();const [,field,type]=definition();options(q('#fieldOperator'),C._FILTER_OPERATORS[type]||[]);if(type==='numeric')q('#fieldOperator').value='ge';
    options(q('#fieldEnum'),C._ENUM_FILTER_VALUES[field]||[]);q('#fieldValue').value='';q('#fieldValue').placeholder=type==='text'?'输入文字':field==='duration_sec'?'输入秒数':'输入整数';q('#fieldValue').inputMode=type==='numeric'?'decimal':'text';
    q('#operatorWrap').hidden=type==='none';q('#fieldValue').hidden=!['numeric','text'].includes(type);q('#enumWrap').hidden=type!=='enum';
    if(type==='none')state.condition=null;
    if(type==='enum')applyEditor();else notify();
  }
  function sync(){
    q('#sortField').value=String(state.sort);q('#dateMode').value=state.date;
    q('#dateMode').dataset.displayValue=state.date==='custom'?state.start.slice(5).replace('-','.')+' 至 '+state.end.slice(5).replace('-','.') : '';
    q('#dateMode').title=state.date==='custom'?state.start+' 至 '+state.end:'按作品发布时间筛选';
    q('#dateStart').value=state.date==='custom'?state.start:'';q('#dateEnd').value=state.date==='custom'?state.end:'';
    q('#dateStart').placeholder=state.start;q('#dateEnd').placeholder=state.end;
    const direction=q('#sortDirection');direction.disabled=state.sort===0;direction.title=state.sort===0?'保留主页顺序':state.desc?'降序，点击改为升序':'升序，点击改为降序';direction.setAttribute('aria-label',direction.title);direction.firstElementChild.textContent=state.desc?'↓':'↑';
    all('thead [data-sort]').forEach(b=>{const active=Number(b.dataset.sort)===state.sort;b.classList.toggle('active',active);const mark=b.querySelector('.sort-mark')||document.createElement('span');mark.className='sort-mark';mark.textContent=active?(state.desc?'↓':'↑'):'';if(!mark.isConnected)b.append(mark);const original=b.querySelector('i');if(original)original.hidden=active;b.closest('th').setAttribute('aria-sort',active?(state.desc?'descending':'ascending'):'none');b.title=active?(state.desc?'当前降序，点击升序':'当前升序，点击降序'):'按'+b.textContent.trim()+'排序';});
    const active=state.condition;
    q('#activeCondition').textContent=active?`${fields.find(f=>f[1]===active[0])[0]} ${C._FILTER_OPERATORS[active[1]].find(o=>o[1]===active[2])[0]} ${active[1]==='enum'?C._ENUM_FILTER_VALUES[active[0]].find(v=>v[1]===active[3])?.[0]:active[3]}`:'';
    window.LuluSelect?.syncAll();
  }
  const validDate=v=>/^\d{4}-\d{2}-\d{2}$/.test(v)&&Number(v.slice(0,4))>=1&&!Number.isNaN(Date.parse(v+'T00:00:00Z'))&&new Date(v+'T00:00:00Z').toISOString().slice(0,10)===v;
  function commitDate(which,value){if(!validDate(value)){sync();return false;}state.date='custom';state[which]=value;if(state.start>state.end)state[which==='start'?'end':'start']=value;notify();return true;}
  function calendar(which,month){
    const pop=q(which==='start'?'#startCalendar':'#endCalendar'),selected=state[which];month=month||selected.slice(0,7);
    const year=Number(month.slice(0,4)),m=Number(month.slice(5,7))-1,start=new Date(Date.UTC(year,m,1)),days=new Date(Date.UTC(year,m+1,0)).getUTCDate(),offset=(start.getUTCDay()+6)%7;
    pop.innerHTML=`<div class="calendar-head"><button class="calendar-step" data-month="-1" aria-label="上个月">‹</button><strong>${year} 年 ${m+1} 月</strong><button class="calendar-step" data-month="1" aria-label="下个月">›</button></div><div class="calendar-grid">${['一','二','三','四','五','六','日'].map(d=>`<span class="weekday">${d}</span>`).join('')}${'<span></span>'.repeat(offset)}${Array.from({length:days},(_,i)=>{const date=month+'-'+String(i+1).padStart(2,'0'),inRange=state.date==='custom'&&date>=state.start&&date<=state.end;return `<button class="calendar-day${date===selected?' selected':''}${inRange?' in-range':''}${date===C.today?' today':''}" data-day="${date}" aria-label="${date}" aria-pressed="${date===selected}">${i+1}</button>`;}).join('')}</div><button class="calendar-today" data-today>今天</button>`;
    pop.querySelectorAll('[data-month]').forEach(b=>b.onclick=()=>{const d=new Date(Date.UTC(year,m+Number(b.dataset.month),1));calendar(which,d.toISOString().slice(0,7));});
    pop.querySelectorAll('[data-day]').forEach(b=>b.onclick=()=>{commitDate(which,b.dataset.day);pop.hidePopover();q(which==='start'?'#dateStart':'#dateEnd').focus();});
    pop.querySelector('[data-today]').onclick=()=>{commitDate(which,C.today);pop.hidePopover();};
    pop.onkeydown=e=>{const current=e.target.closest('[data-day]');if(!current)return;const delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[e.key];if(delta){e.preventDefault();const d=new Date(current.dataset.day+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+delta);const iso=d.toISOString().slice(0,10);if(iso.slice(0,7)!==month)calendar(which,iso.slice(0,7));pop.querySelector(`[data-day="${iso}"]`)?.focus();}};
  }
  function mount(callback){
    let lastDay=C.today;
    const checkDay=()=>{const day=C.today;if(day!==lastDay){lastDay=day;if(!['all','custom'].includes(state.date))notify();}};
    setInterval(checkDay,60000);document.addEventListener('visibilitychange',checkDay);
    change=callback;options(q('#sortField'),C._SORT_FIELDS);options(q('#dateMode'),C._DATE_FILTERS.map(([value,label])=>[label,value]));q('#dateMode option[value="custom"]').hidden=true;options(q('#fieldName'),fields.map(([label,value])=>[label,value]));
    q('#sortField').onchange=()=>{const col=Number(q('#sortField').value);if(col!==state.sort){state.sort=col;state.desc=C._SORT_DESC_DEFAULT[col]??true;}notify();};
    q('#sortDirection').onclick=()=>{if(state.sort!==0){state.desc=!state.desc;notify();}};
    all('thead [data-sort]').forEach(b=>b.onclick=()=>{const col=Number(b.dataset.sort);state.desc=col===state.sort?!state.desc:C._SORT_DESC_DEFAULT[col];state.sort=col;notify();});
    q('#dateMode').onchange=()=>{state.date=q('#dateMode').value;notify();};q('#search').oninput=()=>{state.search=q('#search').value.trim();notify();};
    q('#fieldName').onchange=fieldChanged;q('#fieldOperator').onchange=applyEditor;q('#fieldValue').oninput=applyEditor;q('#fieldEnum').onchange=applyEditor;
    ['start','end'].forEach(which=>{const input=q(which==='start'?'#dateStart':'#dateEnd');input.onchange=()=>commitDate(which,input.value);input.onkeydown=e=>{if(e.key==='Enter')commitDate(which,input.value);};q(which==='start'?'#startCalendarTrigger':'#endCalendarTrigger').addEventListener('click',()=>calendar(which),true);});
    sync();
  }
  function reset(){state=initial();q('#search').value='';q('#fieldName').value='';error();q('#fieldValue').value='';q('#operatorWrap').hidden=true;q('#fieldValue').hidden=true;q('#enumWrap').hidden=true;sync();}
  function clear(){const sort=state.sort,desc=state.desc;reset();state.sort=sort;state.desc=desc;notify();}
  function searchReset(){state.search='';q('#search').value='';sync();}
  return {mount,filter,matches,bounds,initial,snapshot:()=>structuredClone(state),reset,clear,searchReset,active:()=>!!(state.search||state.date!=='all'||state.condition),apply:filter,commitDate};
})();
