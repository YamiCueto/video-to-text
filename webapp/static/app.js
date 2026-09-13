"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {config:null, jobs:[], selected:null, detail:null, source:'youtube', filter:'all', dirty:false, generation:0};
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clock = seconds => { const value=Math.floor(seconds || 0); return `${Math.floor(value/60).toString().padStart(2,'0')}:${(value%60).toString().padStart(2,'0')}`; };
const date = value => new Date(value).toLocaleDateString('es', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
let toastTimer;
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').hidden=true,4500); }
async function api(path, options={}) {
  options.headers = {...options.headers, 'x-local-token':state.config?.token || ''};
  const response = await fetch(`/api${path}`, options);
  if (!response.ok) {
    let message='No se pudo completar la operación.';
    try { const body=await response.json(); message=typeof body.detail==='string'?body.detail:'Revisa los datos introducidos.'; } catch {}
    throw new Error(message);
  }
  return response.json();
}
function status(job) {
  if(job.status==='completed') return job.reviewed?['reviewed','Revisada']:['','Por revisar'];
  return {queued:['active','En cola'], running:['active','Procesando'], failed:['failed','Error'], cancelled:['cancelled','Cancelada']}[job.status] || ['',''];
}
function badge(job) {const [css,label]=status(job); return `<span class="status ${css}">${job.status==='running'?'<span class="local-dot working-dot"></span>':''}${label}</span>`;}
function renderJobs() {
  $('#library-count').textContent=state.jobs.length;
  $('#jobs-count').textContent=state.jobs.length;
  const query=$('#search-input').value.toLocaleLowerCase();
  const jobs=state.jobs.filter(job => (!query || `${job.title} ${job.url} ${job.model} ${job.imported_from || ''}`.toLocaleLowerCase().includes(query)) &&
    (state.filter==='all' || state.filter==='pending' && job.status==='completed' && !job.reviewed || state.filter==='reviewed' && job.reviewed || state.filter==='active' && ['running','queued'].includes(job.status)));
  $('#jobs-list').innerHTML=jobs.length?jobs.map(job => `<button class="job-row" data-id="${job.id}"><span class="source-icon ${job.url?'youtube':''}" aria-hidden="true">${job.url?'▷':'≋'}</span><span class="job-main"><span class="job-title">${escapeHTML(job.title)}</span><span class="job-meta"><span>${escapeHTML(date(job.created_at))}</span><span>${job.duration?clock(job.duration):job.status==='running'?escapeHTML(job.stage):'—'}</span><span>${job.model==='unknown'?'Importada':escapeHTML(job.model)}</span>${job.imported_from?`<span>${escapeHTML(job.imported_from.includes('/')?'Revisión importada':'Desde workspace')}</span>`:''}</span></span>${badge(job)}<span class="job-arrow" aria-hidden="true">↗</span></button>`).join(''):
    `<div class="empty-state"><strong>${state.jobs.length?'No encontramos coincidencias':'Aquí empieza tu biblioteca'}</strong>${state.jobs.length?'Prueba otra búsqueda o filtro.':'Añade un enlace o archivo para crear tu primera transcripción.'}</div>`;
  $('#jobs-list').querySelectorAll('[data-id]').forEach(button => button.onclick=()=>openJob(button.dataset.id));
}
function canLeave() { return !state.dirty || confirm('Tienes cambios sin guardar. ¿Quieres salir sin guardarlos?'); }
function home(scroll=false) {
  if(!canLeave()) return;
  state.dirty=false; state.selected=null; state.detail=null; state.generation++;
  const audio=$('#review-audio'); if(audio) audio.pause();
  $('#home-view').hidden=false; $('#detail-view').hidden=true; $('#breadcrumb').textContent='Biblioteca';
  history.replaceState(null,'',location.pathname);
  if(scroll) { window.scrollTo({top:0,behavior:'smooth'}); setTimeout(()=>$(state.source==='youtube'?'#url-input':'#file-input').focus(),50); }
}
function sourceTab(source) {
  state.source=source;
  $('#url-input').disabled=source!=='youtube';
  $('#file-input').disabled=source!=='file';
  for(const mode of ['youtube','file']) { $(`#${mode}-panel`).hidden=source!==mode; $(`#${mode}-tab`).classList.toggle('selected',source===mode); $(`#${mode}-tab`).setAttribute('aria-selected',String(source===mode)); }
  $('#form-error').hidden=true;
}
function modelHint() {
  const model=state.config.models.find(model=>model.id===$('#model-input').value);
  $('#model-hint').textContent=model.downloaded?`${model.id} ya está disponible en tu equipo.`:`${model.id} se descargará la primera vez. Puede tardar varios minutos.`;
}
async function createJob(event) {
  event.preventDefault(); $('#form-error').hidden=true;
  const form=new FormData($('#create-form'));
  if(state.source==='youtube') {
    const url=$('#url-input').value.trim();
    if(!url) { $('#form-error').textContent='Pega el enlace de un video de YouTube.'; $('#form-error').hidden=false; $('#url-input').focus(); return; }
    form.set('url',url);
  } else {
    const file=$('#file-input').files[0];
    if(!file) { $('#form-error').textContent='Selecciona un archivo de audio o video.'; $('#form-error').hidden=false; return; }
    if(file.size>1024**3) { $('#form-error').textContent='El archivo supera el límite de 1 GB.'; $('#form-error').hidden=false; return; }
    form.set('file',file);
  }
  const button=$('#submit-button'); button.disabled=true; button.textContent=state.source==='file'?'Subiendo archivo…':'Creando…';
  try { const job=await api('/jobs',{method:'POST',body:form}); await refreshJobs(); await openJob(job.id); toast('Transcripción añadida a la cola.'); }
  catch(error) {$('#form-error').textContent=error.message;$('#form-error').hidden=false;}
  finally {button.disabled=false;button.innerHTML='Transcribir <span aria-hidden="true">↗</span>';}
}
async function refreshJobs() {
  state.jobs=await api('/jobs'); renderJobs();
  $('#connection-label').textContent='· Conectado';
}
async function openJob(id) {
  if(!canLeave()) return;
  const generation=++state.generation;
  try {
    const job=await api(`/jobs/${id}`);
    if(generation!==state.generation) return;
    const previous=$('#review-audio');if(previous)previous.pause();
    state.selected=id; state.detail=job;state.dirty=false;
    $('#home-view').hidden=true;$('#detail-view').hidden=false;$('#breadcrumb').textContent='Revisión de transcripción';
    history.replaceState(null,'',`#${id}`);renderDetail(job);window.scrollTo({top:0,behavior:'instant'});
  } catch(error) {toast(error.message);}
}
function redo(job) {
  home(true); if(state.selected) return;
  sourceTab(job.url?'youtube':'file');
  $('#url-input').value=job.url || '';
  $('#title-input').value=job.title;
  $('#glossary-input').value=job.glossary || '';
  $('#model-input').value=job.model in Object.fromEntries(state.config.models.map(m=>[m.id,true]))?job.model:'small';
  modelHint();
  if(!job.url) toast('Selecciona de nuevo el archivo que quieres transcribir.');
}
function renderDetail(job) {
  const completed=job.status==='completed';
  const heading=`<button class="back-button" id="back-button">← Volver a la biblioteca</button><div class="detail-heading"><div><p class="eyebrow">${completed?'ESCUCHA · REVISA · EXPORTA':'TU TRANSCRIPCIÓN'}</p><h1 id="detail-title">${escapeHTML(job.title)}</h1><div class="detail-info">${badge(job)}<span>${job.model==='unknown'?'Modelo no registrado':escapeHTML(job.model)} · ${escapeHTML(state.config.languages[job.language] || 'Idioma no registrado')}</span>${job.duration?`<span>${clock(job.duration)} de audio</span>`:''}</div></div>${completed?`<div class="detail-actions"><button class="button secondary" id="copy-button">Copiar texto</button><a class="button secondary export-link" href="/api/jobs/${job.id}/download/txt" download>↓ TXT</a><a class="button primary export-link" href="/api/jobs/${job.id}/download/srt" download>↓ SRT</a></div>`:''}</div>`;
  let body='';
  if(completed) {
    body=`<div class="review-banner"><span aria-hidden="true">✦</span><div><strong>El último paso lo pones tú.</strong> Revisa nombres y términos técnicos. Los fragmentos señalados son sugerencias de revisión, no una medición de exactitud.</div></div>`;
    body+=job.has_audio?`<div class="audio-panel"><label for="review-audio">Escucha el original</label><audio id="review-audio" controls preload="metadata" src="/api/jobs/${job.id}/audio"></audio></div>`:
      `<div class="no-audio">Esta transcripción importada no incluye el audio.${job.url?' Puedes volver a procesar su enlace para escucharlo.':''} <button id="redo-button">Crear otra versión ↗</button></div>`;
    body+=`<div class="editor-toolbar"><div><h2>Transcripción</h2><p>${job.result.segments.length} fragmentos · edita el texto conservando sus tiempos</p></div><label><input id="show-original" type="checkbox"> Comparar con el original</label></div><div id="segments">`;
    body+=job.result.segments.map((segment,index)=>`<div class="segment ${segment.uncertain?'uncertain':''}" data-index="${index}"><button class="segment-time" data-start="${segment.start}" ${job.has_audio?'':'disabled'} aria-label="Escuchar desde ${clock(segment.start)}">${job.has_audio?'▷ ':''}${clock(segment.start)}</button><div><textarea class="segment-text" data-index="${index}" aria-label="Texto del fragmento ${index+1}" rows="2" maxlength="20000">${escapeHTML(segment.text)}</textarea>${segment.uncertain?'<div class="uncertain-note">Conviene escuchar este fragmento</div>':''}<div class="original-line" hidden><strong>Original: </strong>${escapeHTML(job.original_segments[index]?.text || '')}</div></div></div>`).join('');
    if(!job.result.segments.length) body+='<p class="no-text">No se detectó voz en este audio.</p>';
    body+=`</div><div class="editor-bottom"><label><input type="checkbox" id="reviewed-input" ${job.reviewed?'checked':''}> He revisado esta transcripción</label><div class="editor-save"><span class="save-label" id="save-label">Cambios guardados</span><button class="button primary" id="save-button" disabled>Guardar cambios</button></div></div><p><a class="original-link" href="/api/jobs/${job.id}/download/txt?original=true" download>Descargar el texto original sin correcciones</a></p>`;
  } else {
    const active=['running','queued'].includes(job.status);
    body=`<div class="process-card"><div class="process-symbol ${active?'working-dot':''}" aria-hidden="true">${active?'≋':job.status==='failed'?'!':'○'}</div><h2>${escapeHTML(job.stage)}</h2>${active?`<div class="progress-row"><progress max="100" ${job.progress!==null?`value="${job.progress}"`:''} aria-label="Progreso de la etapa actual"></progress><span>${job.progress!==null?`${job.progress}%`:'En curso'}</span></div><p>El progreso corresponde a la etapa actual. Puedes volver a la biblioteca; el trabajo continúa mientras la app esté encendida.</p><button id="cancel-button" class="button danger">Cancelar transcripción</button>`:`<p class="error-detail">${escapeHTML(job.error || 'El trabajo fue cancelado. Puedes crear otra versión cuando quieras.')}</p><button class="button primary" id="redo-button">Crear otra versión</button>`}</div>`;
  }
  $('#detail-view').innerHTML=heading+body;
  $('#back-button').onclick=()=>home();
  if($('#redo-button'))$('#redo-button').onclick=()=>redo(job);
  if($('#cancel-button'))$('#cancel-button').onclick=async()=>{try{await api(`/jobs/${job.id}/cancel`,{method:'POST'});await openJob(job.id);await refreshJobs();}catch(error){toast(error.message);}};
  if(!completed) return;
  $('#segments').addEventListener('input',markDirty);
  $('#reviewed-input').onchange=markDirty;
  $('#save-button').onclick=save;
  $('#show-original').onchange=event=>document.querySelectorAll('.original-line').forEach(line=>line.hidden=!event.target.checked);
  document.querySelectorAll('.segment-time').forEach(button=>button.onclick=()=>{const audio=$('#review-audio');if(audio){audio.currentTime=Number(button.dataset.start);audio.play().catch(()=>toast('No se pudo reproducir el audio.'));}});
  const audio=$('#review-audio');
  if(audio)audio.ontimeupdate=()=>document.querySelectorAll('.segment').forEach(element=>{const segment=job.result.segments[Number(element.dataset.index)];element.classList.toggle('playing',audio.currentTime>=segment.start&&audio.currentTime<segment.end);});
  $('#copy-button').onclick=async()=>{try{await navigator.clipboard.writeText(currentText());toast('Texto copiado.');}catch{toast('El navegador no permitió copiar. Descarga el TXT.');}};
  document.querySelectorAll('.export-link').forEach(link=>link.onclick=async event=>{
    if(state.dirty){event.preventDefault();const url=link.href;if(await save()){const a=document.createElement('a');a.href=url;a.download='';document.body.append(a);a.click();a.remove();}}
  });
}
function currentText(){return [...document.querySelectorAll('.segment-text')].map(input=>input.value.trim()).filter(Boolean).join(' ');}
function markDirty(){state.dirty=true;$('#save-label').textContent='Cambios sin guardar';$('#save-button').disabled=false;}
async function save(){
  if(!state.detail)return false;
  const id=state.selected;
  const body={revision:state.detail.revision,reviewed:$('#reviewed-input').checked,segments:[...document.querySelectorAll('.segment-text')].map(input=>({text:input.value}))};
  $('#save-button').disabled=true;$('#save-label').textContent='Guardando…';
  $('#detail-view').classList.add('saving');
  try {const updated=await api(`/jobs/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});state.detail=updated;state.dirty=false;$('#save-label').textContent='Cambios guardados';const oldBadge=$('.detail-info .status');if(oldBadge)oldBadge.outerHTML=badge(updated);await refreshJobs();toast('Texto y subtítulos actualizados.');return true;}
  catch(error){toast(error.message);$('#save-label').textContent='No se guardaron los cambios';$('#save-button').disabled=false;return false;}
  finally{$('#detail-view').classList.remove('saving');}
}
async function poll(){
  try{await refreshJobs();if(state.selected&&state.detail&&['queued','running'].includes(state.detail.status)){
    const id=state.selected;const job=await api(`/jobs/${id}`);if(id===state.selected){state.detail=job;renderDetail(job);}
  }}catch{$('#connection-label').textContent='· Sin conexión';}
  setTimeout(poll,1800);
}
async function init(){
  $('#submit-button').disabled=true;
  try{
    state.config=await api('/config');
    $('#model-input').innerHTML=state.config.models.map(model=>`<option value="${model.id}" ${model.id==='small'?'selected':''}>${model.id[0].toUpperCase()+model.id.slice(1)} · ${escapeHTML(model.label)}</option>`).join('');
    $('#language-input').innerHTML=Object.entries(state.config.languages).map(([id,name])=>`<option value="${id}" ${id==='es'?'selected':''}>${name}</option>`).join('');
    modelHint();await refreshJobs();$('#submit-button').disabled=false;
    if(/^[a-f0-9]{32}$/.test(location.hash.slice(1)))await openJob(location.hash.slice(1));
    setTimeout(poll,1800);
  }catch(error){$('#form-error').textContent='No se pudo conectar con la app local. Comprueba que run_web.py siga abierto y recarga la página.';$('#form-error').hidden=false;$('#connection-label').textContent='· Sin conexión';}
}
$('#new-button').onclick=()=>home(true);$('#library-button').onclick=()=>{home();if(!state.selected)$('#library-heading').scrollIntoView({behavior:'smooth'});};
$('#youtube-tab').onclick=()=>sourceTab('youtube');$('#file-tab').onclick=()=>sourceTab('file');
$('.source-tabs').onkeydown=event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();sourceTab(state.source==='youtube'?'file':'youtube');$(`#${state.source}-tab`).focus();}};
$('#create-form').onsubmit=createJob;$('#model-input').onchange=modelHint;$('#search-input').oninput=renderJobs;
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{state.filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(item=>item.classList.toggle('selected',item===button));renderJobs();});
$('#file-input').onchange=()=>$('#file-label').textContent=$('#file-input').files[0]?.name||'Arrastra tu audio o video aquí';
const drop=$('#drop-zone');['dragenter','dragover'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add('dragging');}));
['dragleave','drop'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove('dragging');}));
drop.addEventListener('drop',event=>{if(event.dataTransfer.files.length!==1){toast('Añade un archivo a la vez.');return;}$('#file-input').files=event.dataTransfer.files;$('#file-input').dispatchEvent(new Event('change'));});
window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
init();
