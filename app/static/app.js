let latencyChart, lossChart, jitterChart;
let currentRange = localStorage.getItem('metricsRange') || '5m';
let sse; // EventSource
let lastChartRender = 0;
let paused = false;
let worker; // Web Worker instance
let decimationTarget = parseInt(localStorage.getItem('decimationTarget')||'400',10);
let lastWorkerUpdate = 0;
let fallbackArmed = false;
let consecutiveFallbacks = 0;
let lastFallbackLog = 0;
let sseReconnectAttempts = 0;
const MAX_SSE_RETRY_DELAY = 15000; // 15s cap

function $(sel){ return document.querySelector(sel); }

function setStatus(state){
  const pill = $('#status');
  if(state.last_ok === true){ pill.textContent='Online'; pill.className='status-pill online'; }
  else if(state.last_ok === false){ pill.textContent='Offline'; pill.className='status-pill offline'; }
  else { pill.textContent='Unknown'; pill.className='status-pill unknown'; }
  const latency = $('#latency');
  latency.textContent = state.last_latency_ms ? `Latency: ${state.last_latency_ms.toFixed(1)} ms` : '';
}

async function fetchStatus(){
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    setStatus(data.state);
    if(data.tz){
      const footer = document.querySelector('footer p');
      if(footer && !footer.textContent.includes(data.tz)){
        footer.textContent += ` | TZ: ${data.tz}`;
      }
      // Update outages table headers to reflect local timezone
      const startHead = document.getElementById('outage-start-header');
      const endHead = document.getElementById('outage-end-header');
      if(startHead && endHead){
        startHead.textContent = `Start (${data.tz})`;
        endHead.textContent = `End (${data.tz})`;
      }
    }
  } catch(e){ /* ignore */ }
}

async function fetchOutages(){
  try {
    const r = await fetch('/api/outages');
    const data = await r.json();
    const tbody = document.querySelector('#outages-table tbody');
    tbody.innerHTML='';
    data.forEach(o=>{
      const tr = document.createElement('tr');
      const startLocal = o.start_time_local || o.start_time;
      const endLocal = o.end_time_local || o.end_time;
      tr.innerHTML = `<td>${o.id}</td><td>${startLocal?new Date(startLocal).toLocaleString():''}</td><td>${endLocal?new Date(endLocal).toLocaleString():''}</td><td>${o.duration_seconds==null?'':o.duration_seconds.toFixed(2)}</td>`;
      tbody.appendChild(tr);
    });
  } catch(e){ /* ignore */ }
}

function ensureCharts(){
  if(!latencyChart){
    const ctx = $('#latencyChart');
    latencyChart = new Chart(ctx,{type:'line',data:{labels:[],datasets:[{label:'Latency (ms)',data:[],borderColor:'#4db3ff',tension:.25,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,plugins:{decimation:{enabled:true,algorithm:'lttb',samples:400}},animation:false,scales:{x:{ticks:{display:false}},y:{beginAtZero:true}}}});
  }
  if(!lossChart){
    const ctx = $('#lossChart');
    lossChart = new Chart(ctx,{type:'bar',data:{labels:[],datasets:[{label:'Loss (%)',data:[],backgroundColor:'#ff6384'}]},options:{responsive:true,maintainAspectRatio:false,plugins:{decimation:{enabled:true,algorithm:'min-max'}},animation:false,scales:{x:{ticks:{display:false}},y:{beginAtZero:true,max:100}}}});
  }
  if(!jitterChart){
    const ctx = $('#jitterChart');
    jitterChart = new Chart(ctx,{type:'line',data:{labels:[],datasets:[{label:'Jitter (ms)',data:[],borderColor:'#ffc658',tension:.25,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,plugins:{decimation:{enabled:true,algorithm:'lttb',samples:300}},animation:false,scales:{x:{ticks:{display:false}},y:{beginAtZero:true}}}});
  }
}

function updateQuickMetrics(m){
  const wrap = $('#quick-metrics');
  wrap.innerHTML = '';
  const entries = [
    ['Avg', fmt(m.avg_latency_ms,' ms')],
    ['Min', fmt(m.min_latency_ms,' ms')],
    ['Max', fmt(m.max_latency_ms,' ms')],
    ['Jitter', fmt(m.jitter_avg_abs_ms,' ms')],
    ['Loss', m.packet_loss_pct!=null? m.packet_loss_pct.toFixed(1)+' %':'-'],
    ['Samples', m.count]
  ];
  for(const [k,v] of entries){
    const div = document.createElement('div');
    div.className='metric';
    div.innerHTML=`<h4>${k}</h4><div class="val">${v}</div>`;
    wrap.appendChild(div);
  }
  const lossEl = $('#packet-loss');
  lossEl.textContent = `Packet Loss: ${m.packet_loss_pct!=null? m.packet_loss_pct.toFixed(2):'-'} %`;
}

function fmt(val,suf=''){ return val==null?'-':val.toFixed(1)+suf; }

function updateMetricsTable(m){
  const tbody = $('#metrics-tbody');
  tbody.innerHTML='';
  const rows = [
    ['Samples', m.count],
    ['Successes', m.successes],
    ['Failures', m.failures],
    ['Packet Loss %', m.packet_loss_pct!=null? m.packet_loss_pct.toFixed(2):'-'],
    ['Avg Latency ms', m.avg_latency_ms?.toFixed(2) ?? '-'],
    ['Min Latency ms', m.min_latency_ms?.toFixed(2) ?? '-'],
    ['Max Latency ms', m.max_latency_ms?.toFixed(2) ?? '-'],
    ['Jitter avg abs ms', m.jitter_avg_abs_ms?.toFixed(2) ?? '-']
  ];
  for(const [k,v] of rows){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${k}</td><td>${v}</td>`;
    tbody.appendChild(tr);
  }
}

// Web worker handles trimming & decimation; no local trimming needed now.

async function fetchMetrics() {
  // Request snapshot from worker for current range
  if (worker) {
    worker.postMessage({ type: 'snapshot' });
  }
  const r = await fetch(`/api/metrics?range=${encodeURIComponent(currentRange)}&limit=5000`);
  const m = await r.json();
}

function initTabs(){
  document.querySelectorAll('.tab').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const tab = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab').forEach(b=>{b.classList.toggle('active', b===btn); b.setAttribute('aria-selected', b===btn);});
      document.querySelectorAll('.tab-panel').forEach(p=>{
        const active = p.id === 'tab-'+tab;
        p.classList.toggle('active',active);
        p.hidden = !active;
      });
      if(tab==='analytics'){
        fetchMetrics();
        // Force a snapshot again after short delay in case worker not yet ready
        setTimeout(()=>{ if(worker){ console.debug('Force snapshot after tab open'); worker.postMessage({type:'snapshot'});} }, 1000);
        // If still no update logged after 2s, log a warning
        setTimeout(()=>{
          if(!lastWorkerUpdate){ console.warn('No worker updates after opening analytics – will trigger fallback snapshot'); fetchMetrics(); }
        }, 2000);
      }
      if(tab==='outages'){ fetchOutages(); }
    });
  });
}

function init(){
  initWorker();
  initTabs();
  fetchStatus();
  fetchOutages();
  fetchMetrics();
  armFallback();
  setInterval(fetchStatus, 1000);
  setInterval(()=> { if(!sse) fetchMetrics(); }, 20000);
  $('#refresh').addEventListener('click', fetchOutages);
  $('#export').addEventListener('click', ()=> window.location='/api/outages/export');
  setInterval(fetchOutages, 20000);
  initRangeButtons();
  initDecimationSlider();
  initPauseButton();
  startSSE();
}

function initRangeButtons(){
  document.querySelectorAll('.range-btn').forEach(btn=>{
    if(btn.dataset.range === currentRange) btn.classList.add('active');
    btn.addEventListener('click', ()=>{
      if(btn.id === 'downloadCsv'){
        window.location = `/api/metrics/export.csv?range=${currentRange}`;
        return;
      }
      const r = btn.dataset.range;
      if(!r) return;
      currentRange = r;
      localStorage.setItem('metricsRange', r);
      document.querySelectorAll('.range-btn[data-range]').forEach(b=>b.classList.toggle('active', b===btn));
      fetchMetrics();
    });
  });
}

function startSSE(){
  try {
    sse = new EventSource('/api/stream/samples');
    sse.onopen = ()=> { console.debug('SSE connected'); sseReconnectAttempts = 0; };
    sse.onmessage = ev => {
      if(paused) return;
      try {
        const sample = JSON.parse(ev.data);
        if(worker){ worker.postMessage({ type:'add', payload:{ sample } }); }
        else console.warn('Sample received but worker missing');
      } catch(e){ console.warn('Failed to parse SSE event', e, ev.data); }
    };
    sse.onerror = (e)=> {
      console.error('SSE error', e);
      if(sse){ sse.close(); sse=null; }
      scheduleSSEReconnect();
    };
  } catch(e){ console.error('SSE init failed', e); sse=null; }
}

function scheduleSSEReconnect(){
  sseReconnectAttempts++;
  const delay = Math.min(1000 * Math.pow(1.5, sseReconnectAttempts), MAX_SSE_RETRY_DELAY);
  console.debug(`Scheduling SSE reconnect in ${Math.round(delay)}ms (attempt ${sseReconnectAttempts})`);
  setTimeout(()=>{
    if(sse) return; // already connected
    startSSE();
    // After reconnect, pull a fresh metrics snapshot to backfill any missed gap
    setTimeout(()=>{ if(worker) fetchAndReseedIfNeeded(); }, 1200);
  }, delay);
}

async function fetchAndReseedIfNeeded(){
  try {
    const r = await fetch(`/api/metrics?range=${encodeURIComponent(currentRange)}&limit=5000`);
    const m = await r.json();
    if(m.samples && m.samples.length){
      console.debug('Reseeding worker after reconnect with', m.samples.length, 'samples');
      worker.postMessage({ type:'replaceAll', payload:{ samples: m.samples }});
    }
  } catch(e){ console.warn('Reseed fetch failed', e); }
}

function updateChartsFromWorker(samples) {
  ensureCharts();
  const now = Date.now();
  if (now - lastChartRender < 750) return; // slightly faster but still throttled
  lastChartRender = now;
  if (!samples || samples.length === 0) {
    // Do NOT clear existing charts; just skip this update (prevents blink)
    return;
  }
  const labels = samples.map(s=> new Date(s.ts).toLocaleTimeString());
  const latencies = samples.map(s => s.success && s.latency_ms != null ? s.latency_ms : null);
  const lossPoints = samples.map(s => s.success ? 0 : 100);
  const jitterSeries = [];
  for (let i = 1; i < latencies.length; i++) {
    if (latencies[i] != null && latencies[i - 1] != null) {
      jitterSeries.push(Math.abs(latencies[i] - latencies[i - 1]));
    } else {
      jitterSeries.push(null);
    }
  }
  jitterSeries.unshift(null);
  latencyChart.data.labels = labels;
  latencyChart.data.datasets[0].data = latencies;
  lossChart.data.labels = labels;
  lossChart.data.datasets[0].data = lossPoints;
  jitterChart.data.labels = labels;
  jitterChart.data.datasets[0].data = jitterSeries;
  latencyChart.update();
  lossChart.update();
  jitterChart.update();
}

// Worker-derived metrics come through messages; local derivation removed.

async function seedWorkerIfEmpty(){
  // Fetch backend metrics as seed if worker has not produced any update yet
  if(lastWorkerUpdate !== 0) return; // already have updates
  try {
    const r = await fetch(`/api/metrics?range=${encodeURIComponent(currentRange)}&limit=1000`);
    const m = await r.json();
    if(m.samples && m.samples.length){
      console.debug('Seeding worker with', m.samples.length, 'samples from backend');
      if(worker){ worker.postMessage({ type:'bulkAdd', payload:{ samples: m.samples }}); }
    } else {
      console.debug('Seed fetch returned no samples');
    }
  } catch(e){ console.warn('Seed worker fetch failed', e); }
}

function initWorker(){
  try {
    const version = Date.now(); // cache-busting simple version
    worker = new Worker(`/static/worker.js?v=${version}`);
    console.debug('Worker created');
  } catch(e){
    console.error('Failed to start worker', e);
    return;
  }
  worker.onmessage = ev => {
    const { type, payload } = ev.data;
    if(type==='update' || type==='snapshot'){
      if(!payload){ console.warn('Worker empty payload', ev.data); return; }
      if(!payload.metrics){ console.warn('Worker payload missing metrics', payload); return; }
      const firstUpdate = lastWorkerUpdate === 0;
      lastWorkerUpdate = Date.now();
      updateQuickMetrics(payload.metrics);
      updateMetricsTable(payload.metrics);
      updateChartsFromWorker(payload.samples||[]);
      if(type==='snapshot') console.debug('Worker snapshot', {samples: payload.samples?.length, metrics: payload.metrics});
      if(firstUpdate && (!payload.samples || payload.samples.length===0)){
        // Immediately attempt a seed fetch if first update delivered zero samples
        seedWorkerIfEmpty();
      }
    } else {
      console.debug('Worker unsolicited message', ev.data);
    }
  };
  worker.onerror = e => console.error('Worker error', e);
  worker.postMessage({ type:'setRange', payload:{ range: currentRange }});
  worker.postMessage({ type:'setDecimation', payload:{ target: decimationTarget }});
  // Attempt early seed
  setTimeout(()=> seedWorkerIfEmpty(), 300);
}

function armFallback(){
  if(fallbackArmed) return;
  fallbackArmed = true;
  setInterval(async ()=>{
    const since = Date.now()-lastWorkerUpdate;
    if(lastWorkerUpdate===0 || since > 5000){
      const now = Date.now();
      if(now - lastFallbackLog > 4000){
        console.warn('No worker updates in >5s; performing direct metrics fetch fallback');
        lastFallbackLog = now;
      }
      try {
        const r = await fetch(`/api/metrics?range=${encodeURIComponent(currentRange)}&limit=800`);
        const m = await r.json();
        updateQuickMetrics(m);
        updateMetricsTable(m);
        updateChartsFromWorker(m.samples||[]);
        consecutiveFallbacks++;
        if(consecutiveFallbacks % 3 === 0){
          if(worker && m.samples && m.samples.length){
            worker.postMessage({ type:'replaceAll', payload:{ samples: m.samples }});
          }
        }
      } catch(e){ console.error('Fallback metrics fetch failed', e); }
    } else {
      consecutiveFallbacks = 0;
    }
  }, 4000);
}

function initPauseButton(){
  const container = document.querySelector('.range-controls');
  if(!container) return;
  let btn = document.createElement('button');
  btn.id='pauseToggle';
  btn.className='range-btn secondary';
  const updateLabel=()=>{ btn.textContent = paused? 'Resume' : 'Pause'; };
  updateLabel();
  btn.addEventListener('click', ()=>{ paused=!paused; updateLabel(); });
  container.prepend(btn);
}

function initDecimationSlider(){
  const container = document.querySelector('.range-controls');
  if(!container) return;
  const wrap = document.createElement('div');
  wrap.style.display='flex';
  wrap.style.alignItems='center';
  wrap.style.gap='4px';
  wrap.style.marginRight='auto';
  const label = document.createElement('span');
  label.style.fontSize='0.55rem';
  label.style.opacity='0.7';
  label.textContent = 'Detail';
  const slider = document.createElement('input');
  slider.type='range';
  slider.min='100'; slider.max='2000'; slider.step='50';
  slider.value=decimationTarget.toString();
  slider.style.width='90px';
  const valSpan = document.createElement('span');
  valSpan.style.fontSize='0.55rem';
  valSpan.textContent = decimationTarget;
  slider.addEventListener('input',()=>{
    decimationTarget = parseInt(slider.value,10);
    valSpan.textContent = decimationTarget;
    localStorage.setItem('decimationTarget', decimationTarget.toString());
    if(worker){ worker.postMessage({ type:'setDecimation', payload:{ target: decimationTarget }}); }
  });
  wrap.appendChild(label);
  wrap.appendChild(slider);
  wrap.appendChild(valSpan);
  container.prepend(wrap);
}

document.addEventListener('DOMContentLoaded', init);
