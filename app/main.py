import asyncio
import json
import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse

from . import db
from . import monitoring
from .metrics_utils import compute_latency_metrics
from fastapi.responses import StreamingResponse
import datetime as dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Internet Connectivity Tracker")

@app.on_event("startup")
async def startup():
    log.info("Application startup")
    await db.init_db()
    await monitoring.start()

@app.on_event("shutdown")
async def shutdown():
    log.info("Application shutdown")
    await monitoring.stop()
    await db.close_db()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/")
async def index():
    return FileResponse("app/static/index.html")

@app.get("/api/status")
async def status():
    state = monitoring.current_state()
    outages = await db.list_outages(limit=1)
    last_outage = outages[0] if outages else None
    if last_outage:
        last_outage['start_time_local'] = db.from_utc_iso(last_outage['start_time']).isoformat()
        if last_outage.get('end_time'):
            last_outage['end_time_local'] = db.from_utc_iso(last_outage['end_time']).isoformat()
    return {"state": state, "last_outage": last_outage, "tz": db.current_tz_name()}

@app.get("/api/outages")
async def outages():
    rows = await db.list_outages()
    for r in rows:
        r['start_time_local'] = db.from_utc_iso(r['start_time']).isoformat()
        if r.get('end_time'):
            r['end_time_local'] = db.from_utc_iso(r['end_time']).isoformat()
    return rows

@app.get("/api/outages/export")
async def export_outages():
    """Export outages as CSV using ONLY local timezone timestamps.

    Columns: id,start_time_local,end_time_local,duration_seconds
    """
    rows = await db.list_outages()
    lines = ["id,start_time_local,end_time_local,duration_seconds"]
    for r in rows:
        start_local = db.from_utc_iso(r['start_time']).isoformat()
        end_val = r.get('end_time')
        end_local = db.from_utc_iso(end_val).isoformat() if end_val else ''
        dur = r.get('duration_seconds','')
        lines.append(f"{r['id']},{start_local},{end_local},{dur}")
    content = "\n".join(lines)
    headers = {
        "Content-Disposition": "attachment; filename=outages.csv",
        "Content-Type": "text/csv; charset=utf-8"
    }
    return PlainTextResponse(content, headers=headers)

@app.get("/api/metrics")
async def metrics(limit: int = 300, range: str = "5m"):
    # Determine since timestamp based on range
    now = dt.datetime.now(dt.timezone.utc)
    rng = range.lower()
    delta_map = {"5m": dt.timedelta(minutes=5), "1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24)}
    if rng in delta_map:
        since_ts = now - delta_map[rng]
        samples = await db.latency_samples_since(since_ts)
    else:
        samples = await db.recent_latency_samples(limit=limit)
    enriched = []
    for s in samples:
        enriched.append({**s, 'ts_local': db.from_utc_iso(s['ts']).isoformat()})
    metrics_data = compute_latency_metrics(samples)
    metrics_data["samples"] = enriched
    metrics_data["range"] = rng
    metrics_data["tz"] = db.current_tz_name()
    return metrics_data

@app.get("/api/metrics/export.csv")
async def metrics_export_csv(range: str = "5m"):
    now = dt.datetime.now(dt.timezone.utc)
    rng = range.lower()
    delta_map = {"5m": dt.timedelta(minutes=5), "1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24)}
    if rng in delta_map:
        since_ts = now - delta_map[rng]
        samples = await db.latency_samples_since(since_ts)
    else:
        samples = await db.recent_latency_samples(limit=10000)
    lines = ["id,ts_utc,ts_local,success,latency_ms"]
    for i,s in enumerate(samples, start=1):
        local_ts = db.from_utc_iso(s['ts']).isoformat()
        lines.append(f"{i},{s['ts']},{local_ts},{int(s['success'])},{s['latency_ms'] if s['latency_ms'] is not None else ''}")
    content = "\n".join(lines)
    headers = {"Content-Disposition": f"attachment; filename=metrics_{rng}.csv"}
    return PlainTextResponse(content, headers=headers)

@app.get("/api/stream/samples")
async def stream_samples():
    async def event_generator():
        last_id = await db.latest_latency_id()
        while True:
            await asyncio.sleep(1)
            new_samples = await db.latency_samples_after_id(last_id)
            if new_samples:
                last_id = new_samples[-1]["id"]
                for s in new_samples:
                    payload = dict(s)
                    payload['ts_local'] = db.from_utc_iso(s['ts']).isoformat()
                    # SSE format: double newline, data: prefix, must be json
                    yield f"data: {json.dumps(payload)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/debug/counts")
async def debug_counts():
    """Lightweight counts of samples and outages for troubleshooting UI not updating."""
    import aiosqlite
    conn = await db.get_db()
    cur1 = await conn.execute("SELECT COUNT(*) FROM latency_samples")
    samples = (await cur1.fetchone())[0]
    cur2 = await conn.execute("SELECT COUNT(*) FROM outages")
    outages = (await cur2.fetchone())[0]
    return {"samples": samples, "outages": outages}

@app.get("/api/debug/state")
async def debug_state():
    return monitoring.current_state()

@app.get("/api/debug/first-samples")
async def debug_first_samples(limit: int = 5):
    conn = await db.get_db()
    cur = await conn.execute("SELECT id, ts, success, latency_ms FROM latency_samples ORDER BY id ASC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]
