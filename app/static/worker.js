// Web Worker: handles sample buffering, metrics, and downsampling

// Circular buffer implementation
class CircularBuffer {
  constructor(capacity){
    this.capacity = capacity;
    this.buffer = new Array(capacity);
    this.start = 0; // index of oldest
    this.length = 0; // number of valid entries
  }
  push(item){
    if(this.length < this.capacity){
      this.buffer[(this.start + this.length) % this.capacity] = item;
      this.length++;
    } else { // overwrite oldest
      this.buffer[this.start] = item;
      this.start = (this.start + 1) % this.capacity;
    }
  }
  toArray(){
    const arr = new Array(this.length);
    for(let i=0;i<this.length;i++){
      arr[i] = this.buffer[(this.start + i) % this.capacity];
    }
    return arr;
  }
  trimBefore(timestampIso){
    // Remove samples older than timestampIso by advancing start
    if(this.length === 0) return;
    let removed = 0;
    while(this.length > 0){
      const first = this.buffer[this.start];
      if(!first || Date.parse(first.ts) >= Date.parse(timestampIso)) break;
      this.start = (this.start + 1) % this.capacity;
      this.length--;
      removed++;
    }
    return removed;
  }
}

const RANGE_WINDOWS = { '5m': 5*60*1000, '1h': 60*60*1000, '24h': 24*60*60*1000 };
const MAX_CAP = 50000; // upper bound for circular buffer
let buffer = new CircularBuffer(MAX_CAP);
let currentRange = '5m';
let decimationTarget = 400; // default sample target after decimation

function computeMetrics(samples){
  const latencies = [];
  let successes = 0;
  for(const s of samples){
    if(s.success){
      successes++;
      if(s.latency_ms != null) latencies.push(s.latency_ms);
    }
  }
  const total = samples.length;
  const failures = total - successes;
  const packetLoss = total ? (failures/total*100) : 0;
  let avg=null,min=null,max=null,jitter=null;
  if(latencies.length){
    min = Math.min(...latencies);
    max = Math.max(...latencies);
    avg = latencies.reduce((a,b)=>a+b,0)/latencies.length;
    if(latencies.length>1){
      let sumDiff=0; let c=0;
      for(let i=1;i<latencies.length;i++){ sumDiff += Math.abs(latencies[i]-latencies[i-1]); c++; }
      jitter = c? sumDiff/c : null;
    }
  }
  return { count: total, successes, failures, packet_loss_pct: packetLoss, avg_latency_ms: avg, min_latency_ms: min, max_latency_ms: max, jitter_avg_abs_ms: jitter };
}

function lttbDownsample(data, target){
  if(data.length <= target) return data;
  const bucketSize = (data.length - 2) / (target - 2);
  const sampled = [data[0]];
  let a = 0;
  for(let i=0;i<target-2;i++){
    const rangeStart = Math.floor((i+1)*bucketSize)+1;
    const rangeEnd = Math.floor((i+2)*bucketSize)+1;
    const range = data.slice(rangeStart, rangeEnd);
    const avgRangeStart = Math.floor(i*bucketSize)+1;
    const avgRangeEnd = Math.floor((i+1)*bucketSize)+1;
    const avgRange = data.slice(avgRangeStart, avgRangeEnd);
    let avgX=0, avgY=0, avgCount=0;
    for(const p of avgRange){ if(p.latency_ms!=null){ avgX+=Date.parse(p.ts); avgY+=p.latency_ms; avgCount++; } }
    if(!avgCount){ avgX = Date.parse(data[a].ts); avgY = data[a].latency_ms||0; avgCount=1; }
    avgX/=avgCount; avgY/=avgCount;
    let maxArea=-1, chosen=null;
    for(const p of range){
      const area = Math.abs((Date.parse(data[a].ts)-avgX)*( (p.latency_ms||0) - avgY ) - (Date.parse(p.ts)-avgX)*( (data[a].latency_ms||0)-avgY ));
      if(area>maxArea){ maxArea=area; chosen=p; }
    }
    if(chosen) sampled.push(chosen);
    a = rangeStart;
  }
  sampled.push(data[data.length-1]);
  return sampled;
}

function filterRange(arr){
  if(currentRange==='all') return arr;
  const span = RANGE_WINDOWS[currentRange];
  if(!span) return arr;
  const cutoff = Date.now()-span;
  return arr.filter(s => Date.parse(s.ts) >= cutoff);
}

function prepareSnapshot(){
  const full = filterRange(buffer.toArray());
  const decimated = lttbDownsample(full, decimationTarget);
  const metrics = computeMetrics(full);
  return { metrics, samples: decimated };
}

function handleMessage(e){
  const { type, payload } = e.data;
  if(type==='add'){
    buffer.push(payload.sample);
    // Trim old data if beyond range
    if(currentRange!=='all'){
      const span = RANGE_WINDOWS[currentRange];
      if(span){
        const cutoffIso = new Date(Date.now()-span).toISOString();
        buffer.trimBefore(cutoffIso);
      }
    }
  } else if(type==='bulkAdd') {
    // Seed buffer only if it's currently empty to avoid duplicate inflation
    if(buffer.length === 0 && payload && Array.isArray(payload.samples)){
      for(const s of payload.samples){ buffer.push(s); }
      if(currentRange!=='all'){
        const span = RANGE_WINDOWS[currentRange];
        if(span){
          const cutoffIso = new Date(Date.now()-span).toISOString();
          buffer.trimBefore(cutoffIso);
        }
      }
    }
  } else if(type==='replaceAll') {
    if(payload && Array.isArray(payload.samples)){
      buffer = new CircularBuffer(MAX_CAP);
      for(const s of payload.samples){ buffer.push(s); }
      if(currentRange!=='all'){
        const span = RANGE_WINDOWS[currentRange];
        if(span){
          const cutoffIso = new Date(Date.now()-span).toISOString();
          buffer.trimBefore(cutoffIso);
        }
      }
    }
  } else if(type==='setRange'){
    currentRange = payload.range;
  } else if(type==='setDecimation'){
    decimationTarget = payload.target;
  } else if(type==='snapshot'){
    const snap = prepareSnapshot();
    postMessage({ type:'snapshot', payload: snap });
    return;
  }
  // For streaming updates send lightweight update every time
  const snap = prepareSnapshot();
  postMessage({ type:'update', payload: snap });
}

self.onmessage = handleMessage;
