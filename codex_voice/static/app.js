const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {saved: {}, draft: {}, dirty: new Set(), events: [], status: {}, page: 'voice', testID: null};
const labels = {voice:'语音设置', rules:'提醒规则', history:'播报记录'};
const speakerNames = {zh_female_xiaohe_uranus_bigtts:'小何 · 女声', zh_female_vv_uranus_bigtts:'Vivi · 女声'};
const statusNames = {played:'已播放', pending:'等待播放', preparing:'正在组织语言', synthesizing:'正在合成', playing:'正在播放', failed:'播放失败', uncertain:'播放状态待确认', cancelled:'已取消', expired:'已过期'};
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get('token') || sessionStorage.getItem('codex-voice-token') || '';
if (token) sessionStorage.setItem('codex-voice-token', token);
history.replaceState(null, '', location.pathname + location.search);
let toastTimer;

function toast(message, error=false) {
  clearTimeout(toastTimer); const el=$('#toast'); el.textContent=message; el.classList.toggle('error', error); el.hidden=false;
  toastTimer=setTimeout(()=>el.hidden=true,4500);
}
function connectionError(message) {
  $('#connect-message').textContent=message; $('#connect-error').hidden=false; $('#app').hidden=true;
}
async function api(path, data) {
  const result=await fetch('/api/'+path, {method:data===undefined?'GET':'POST', headers:{Authorization:'Bearer '+token, ...(data===undefined?{}:{'Content-Type':'application/json'})}, body:data===undefined?undefined:JSON.stringify(data), signal:AbortSignal.timeout(10000)});
  const value=await result.json();
  if(result.status===401) connectionError(value.error);
  if(!result.ok) throw new Error(value.error || '本机服务暂时不可用');
  return value;
}
function go(page) {
  if(!labels[page]) return;
  state.page=page;
  $$('.page').forEach(el=>el.hidden=el.id!=='page-'+page);
  $$('.nav-item').forEach(el=>{const active=el.dataset.page===page;el.classList.toggle('active',active);el.setAttribute('aria-current',active?'page':'false');});
  $('#breadcrumb').textContent=labels[page];window.scrollTo(0,0);
}
function changed(key,value) {
  state.draft[key]=value;
  if(JSON.stringify(value)===JSON.stringify(state.saved[key])) state.dirty.delete(key); else state.dirty.add(key);
  $('#save-bar').hidden=state.dirty.size===0;
  $('#preview-tip').textContent=state.dirty.size?'请保存修改后再试听。':'修改后保存，即可用于后续提醒。';
}
function receiveSettings(data, reset=false) {
  state.saved=data.settings;
  if(reset) {state.draft=structuredClone(data.settings);state.dirty.clear();}
  else for(const [key,value] of Object.entries(data.settings)) if(!state.dirty.has(key)) state.draft[key]=structuredClone(value);
  $('#volc-key-status').textContent=data.credentials.volc?'已配置':'尚未配置';
  $('#ds-key-status').textContent=data.credentials.deepseek?'已配置':'尚未配置';
  renderFields();
}
function renderFields() {
  $$('[data-field]').forEach(el=>{const value=state.draft[el.dataset.field];if(el.type==='checkbox')el.checked=!!value;else el.value=value??'';});
  const speaker=state.draft.speaker;
  $('#speaker-select').value=speakerNames[speaker]?speaker:'custom';$('#custom-speaker').hidden=!!speakerNames[speaker];
  $('#volume-value').textContent=Math.round(state.draft.volume*100)+'%';
  $('#reminder-minutes').value=+(state.draft.reminder_seconds/60).toFixed(2);
  $('#age-minutes').value=+(state.draft.max_age_seconds/60).toFixed(2);
  renderProvider();
  $('#save-bar').hidden=state.dirty.size===0;renderQuiet();
  $('#preview-tip').textContent=state.dirty.size?'请保存修改后再试听。':'修改后保存，即可用于后续提醒。';
}

function renderProvider() {
  const codex=state.draft.text_provider==='codex';
  $('#codex-model-field').hidden=!codex;
  $('#ds-provider-field').hidden=codex;$('#ds-model-field').hidden=codex;
  $('#ds-key-field').hidden=codex||state.draft.deepseek_provider!=='deepseek-official';
}

function renderStatus(data) {
  state.status=data;
  const active=data.running&&data.enabled;
  const label=!data.running?'连接中':data.enabled?'运行中':'已暂停';
  $('#side-status').textContent=label;$('#side-dot').classList.toggle('offline',!active);
  $('#toggle span').textContent=data.enabled?'暂停提醒':'恢复提醒';
  $('#toggle use').setAttribute('href',data.enabled?'#i-pause':'#i-play');
  if(!state.dirty.has('enabled')) {state.saved.enabled=data.enabled;state.draft.enabled=data.enabled;}
}
function node(tag,className,text) {const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;}
function icon(name) {const el=document.createElementNS('http://www.w3.org/2000/svg','svg');const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#i-'+name);el.append(use);return el;}
function eventRow(event,compact=false) {
  const row=node('article','event-row');const circle=node('div','event-icon');circle.append(icon(event.kind==='test'?'wave':event.status==='played'?'check':event.kind.startsWith('waiting')?'bell':'clock'));row.append(circle);
  const body=node('div','event-body');body.append(node('h3','',event.title));
  if(compact) body.append(node('p','',event.speech||({cancelled:'事项已发生变化，提醒已取消。',expired:'消息已过保留时间，已跳过播报。',failed:'语音未播放成功，可在记录中查看详情。',uncertain:'播放状态待确认，请查看详细记录。'}[event.status]||'消息已记录，等待播报。')));
  else if(event.speech||event.error) {const detail=node('details');detail.append(node('summary','',event.error?'查看播放详情':'查看播报内容'));detail.append(node('p','',event.speech||event.error));if(event.error&&event.speech)detail.append(node('p','',event.error));body.append(detail);}
  row.append(body);
  const meta=node('div','event-meta');let style=['failed','uncertain'].includes(event.status)?'bad':['preparing','synthesizing','playing','pending'].includes(event.status)?'warn':['cancelled','expired'].includes(event.status)?'neutral':'';
  meta.append(node('span','badge '+style,statusNames[event.status]||event.status));const date=new Date(event.created*1000);meta.append(node('time','',date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})));row.append(meta);return row;
}
function renderEvents(events) {
  state.events=events;$('#history-count').textContent=events.length;$('#event-count').textContent=events.length+' 条';
  renderHistory();
  if(state.testID) {const test=events.find(e=>e.id===state.testID);if(test&&['played','failed','uncertain','expired'].includes(test.status)){toast(test.status==='played'?'试听已播放完成':'试听未成功，请在播报记录中查看详情。',test.status!=='played');state.testID=null;$$('.test-button').forEach(b=>b.disabled=false);}}
}
function renderHistory() {
  const value=$('#history-filter').value;
  const groups={pending:['pending','preparing','synthesizing','playing'],failed:['failed','uncertain'],cancelled:['cancelled','expired']};
  const events=state.events.filter(e=>value==='all'||(groups[value]||[value]).includes(e.status));
  const box=$('#history-events');box.replaceChildren();if(!events.length)box.append(node('div','empty','这里还没有对应的播报记录。'));else events.forEach(e=>box.append(eventRow(e)));
}
function renderQuiet() {
  const list=$('#quiet-list');list.replaceChildren();const windows=state.draft.quiet_hours||[];$('#quiet-empty').hidden=windows.length>0;
  windows.forEach((times,index)=>{const row=node('div','quiet-row');row.append(node('span','','每天'));for(let i=0;i<2;i++){if(i===1)row.append(node('span','','至'));const input=node('input');input.type='time';input.value=times[i];input.setAttribute('aria-label',i===0?'免打扰开始时间':'免打扰结束时间');input.addEventListener('change',()=>{const value=structuredClone(state.draft.quiet_hours);value[index][i]=input.value;changed('quiet_hours',value);});row.append(input);}const remove=node('button','button small','移除');remove.addEventListener('click',()=>{changed('quiet_hours',windows.filter((_,i)=>i!==index));renderQuiet();});row.append(remove);list.append(row);});
}
async function save() {
  if(!state.dirty.size)return;
  const button=$('#save');button.disabled=true;
  try {const changes=Object.fromEntries([...state.dirty].map(key=>[key,state.draft[key]]));const result=await api('config',changes);receiveSettings(result,true);toast('设置已保存，将应用到后续提醒。');await refresh();}
  catch(error){toast(error.message,true);}finally{button.disabled=false;}
}
let refreshing=false;
async function refresh() {
  if(refreshing)return;refreshing=true;
  try {const results=await Promise.allSettled([api('status'),api('events')]);if(results[0].status==='fulfilled')renderStatus(results[0].value);else {$('#side-status').textContent='暂时无法连接';$('#side-dot').classList.add('offline');}if(results[1].status==='fulfilled')renderEvents(results[1].value.events);}
  finally{refreshing=false;}
}
$$('[data-page]').forEach(button=>button.addEventListener('click',()=>go(button.dataset.page)));
$$('[data-go]').forEach(button=>button.addEventListener('click',()=>go(button.dataset.go)));
$$('[data-field]').forEach(input=>input.addEventListener(input.tagName==='SELECT'||input.type==='checkbox'?'change':'input',()=>{const key=input.dataset.field;let value=input.type==='checkbox'?input.checked:input.type==='range'||input.type==='number'?Number(input.value):input.value;changed(key,value);if(key==='volume')$('#volume-value').textContent=Math.round(value*100)+'%';if(key==='deepseek_provider'||key==='text_provider')renderProvider();}));
$('#speaker-select').addEventListener('change',()=>{const value=$('#speaker-select').value;$('#custom-speaker').hidden=value!=='custom';if(value!=='custom')changed('speaker',value);else $('[data-field="speaker"]').focus();});
$('#reminder-minutes').addEventListener('input',()=>changed('reminder_seconds',Number($('#reminder-minutes').value)*60));
$('#age-minutes').addEventListener('input',()=>changed('max_age_seconds',Number($('#age-minutes').value)*60));
$('#add-quiet').addEventListener('click',()=>{changed('quiet_hours',[...(state.draft.quiet_hours||[]),['23:00','08:00']]);renderQuiet();});
$('#history-filter').addEventListener('change',renderHistory);
$('#save').addEventListener('click',save);
$('#discard').addEventListener('click',()=>{state.draft=structuredClone(state.saved);state.dirty.clear();renderFields();$('#preview-tip').textContent='修改后保存，即可用于后续提醒。';});
$('#toggle').addEventListener('click',async()=>{try{const result=await api('config',{enabled:!state.status.enabled});receiveSettings(result);await refresh();toast(result.settings.enabled?'提醒已恢复。':'提醒已暂停，记录仍会继续更新。');}catch(error){toast(error.message,true);}});
$$('.test-button').forEach(button=>button.addEventListener('click',async()=>{if(state.dirty.size){toast('请先保存修改，再试听新配置。');return;}$$('.test-button').forEach(b=>b.disabled=true);try{const result=await api('test',{});state.testID=result.event_id;toast('正在准备试听，稍后会从本机播放。');await refresh();}catch(error){toast(error.message,true);$$('.test-button').forEach(b=>b.disabled=false);}}));
$$('[data-credential]').forEach(button=>button.addEventListener('click',async()=>{const key=button.dataset.credential;const input=key==='volc_api_key'?$('#volc-key'):$('#ds-key');if(!input.value.trim()){toast('请先输入新的 API Key。',true);return;}button.disabled=true;try{await api('credentials',{[key]:input.value});input.value='';receiveSettings(await api('config'));toast('凭据已安全保存。');}catch(error){toast(error.message,true);}finally{button.disabled=false;}}));
$('#reload').addEventListener('click',()=>location.reload());
document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key==='s'){event.preventDefault();save();}});
window.addEventListener('beforeunload',event=>{if(state.dirty.size){event.preventDefault();event.returnValue='';}});
(async()=>{if(!token){connectionError('请从顶部菜单栏打开设置，或运行 codex-voice ui。');return;}try{receiveSettings(await api('config'),true);go(new URLSearchParams(location.search).get('page')||'voice');await refresh();setInterval(refresh,3000);}catch(error){connectionError(error.message);}})();
