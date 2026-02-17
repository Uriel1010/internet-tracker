import asyncio
import json
import logging
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse

from . import db
from . import monitoring
from .metrics_utils import compute_latency_metrics
from fastapi.responses import StreamingResponse
import datetime as dt
from . import monitoring as mon_mod
from . import webhooks

_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _log_level, logging.INFO), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
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
async def status(service: str | None = None):
    raw_state = monitoring.current_state(service)
    chosen_service = service
    effective_state = raw_state
    # If no explicit service requested and we received an aggregate mapping, unwrap if only one
    if service is None and isinstance(raw_state, dict) and raw_state and 'last_ok' not in raw_state:
        # aggregate case
        if len(raw_state) == 1:
            # unwrap single-service aggregate
            (only_name, only_state), = raw_state.items()
            effective_state = only_state
            chosen_service = only_name
        else:
            # pick first service alphabetically to expose as effective for legacy UI
            first_name = sorted(raw_state.keys())[0]
            effective_state = raw_state[first_name]
            chosen_service = first_name
    # Fetch last outage for chosen service (if we have a usable state with last_ok field)
    outages = await db.list_outages(limit=1, service=chosen_service) if effective_state else []
    last_outage = outages[0] if outages else None
    if last_outage:
        last_outage['start_time_local'] = db.from_utc_iso(last_outage['start_time']).isoformat()
        if last_outage.get('end_time'):
            last_outage['end_time_local'] = db.from_utc_iso(last_outage['end_time']).isoformat()
    return {
        "state": effective_state,
        "last_outage": last_outage,
        "tz": db.current_tz_name(),
        "service": chosen_service,
        "aggregate": raw_state if effective_state is not raw_state else None
    }

@app.get("/api/outages")
async def outages(service: str | None = None):
    rows = await db.list_outages(service=service)
    for r in rows:
        r['start_time_local'] = db.from_utc_iso(r['start_time']).isoformat()
        if r.get('end_time'):
            r['end_time_local'] = db.from_utc_iso(r['end_time']).isoformat()
    return rows

@app.get("/api/outages/export")
async def export_outages(service: str | None = None):
    """Export outages as CSV using ONLY local timezone timestamps.

    Columns: id,start_time_local,end_time_local,duration_seconds
    """
    rows = await db.list_outages(service=service)
    lines = ["id,service,start_time_local,end_time_local,duration_seconds"]
    for r in rows:
        start_local = db.from_utc_iso(r['start_time']).isoformat()
        end_val = r.get('end_time')
        end_local = db.from_utc_iso(end_val).isoformat() if end_val else ''
        dur = r.get('duration_seconds','')
        lines.append(f"{r['id']},{r.get('service','default')},{start_local},{end_local},{dur}")
    content = "\n".join(lines)
    headers = {
        "Content-Disposition": "attachment; filename=outages.csv",
        "Content-Type": "text/csv; charset=utf-8"
    }
    return PlainTextResponse(content, headers=headers)

@app.get("/api/metrics")
async def metrics(limit: int = 300, range: str = "5m", service: str | None = 'default'):
    # Determine since timestamp based on range
    now = dt.datetime.now(dt.timezone.utc)
    rng = range.lower()
    delta_map = {"5m": dt.timedelta(minutes=5), "1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24)}
    if rng in delta_map:
        since_ts = now - delta_map[rng]
        samples = await db.latency_samples_since(since_ts, service=service or 'default')
    else:
        samples = await db.recent_latency_samples(limit=limit, service=service or 'default')
    enriched = []
    for s in samples:
        enriched.append({**s, 'ts_local': db.from_utc_iso(s['ts']).isoformat()})
    metrics_data = compute_latency_metrics(samples)
    # Add raw total samples count (without range filter) for debugging perceived drops
    try:
        conn = await db.get_db()
        cur = await conn.execute("SELECT COUNT(*) FROM latency_samples WHERE service = ?", (service or 'default',))
        metrics_data["total_samples"] = (await cur.fetchone())[0]
    except Exception:
        metrics_data["total_samples"] = None
    metrics_data["service"] = service or 'default'
    metrics_data["samples"] = enriched
    metrics_data["range"] = rng
    metrics_data["tz"] = db.current_tz_name()
    return metrics_data

@app.get("/api/metrics/export.csv")
async def metrics_export_csv(range: str = "5m", service: str | None = 'default'):
    now = dt.datetime.now(dt.timezone.utc)
    rng = range.lower()
    delta_map = {"5m": dt.timedelta(minutes=5), "1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24)}
    if rng in delta_map:
        since_ts = now - delta_map[rng]
        samples = await db.latency_samples_since(since_ts, service=service or 'default')
    else:
        samples = await db.recent_latency_samples(limit=10000, service=service or 'default')
    lines = ["id,service,ts_utc,ts_local,success,latency_ms"]
    for i,s in enumerate(samples, start=1):
        local_ts = db.from_utc_iso(s['ts']).isoformat()
        lines.append(f"{i},{service or 'default'},{s['ts']},{local_ts},{int(s['success'])},{s['latency_ms'] if s['latency_ms'] is not None else ''}")
    content = "\n".join(lines)
    headers = {"Content-Disposition": f"attachment; filename=metrics_{rng}.csv"}
    return PlainTextResponse(content, headers=headers)

@app.get("/api/stream/samples")
async def stream_samples(service: str | None = 'default'):
    async def event_generator():
        last_id = await db.latest_latency_id(service=service or 'default')
        while True:
            await asyncio.sleep(1)
            new_samples = await db.latency_samples_after_id(last_id, service=service or 'default')
            if new_samples:
                last_id = new_samples[-1]["id"]
                for s in new_samples:
                    payload = dict(s)
                    payload['ts_local'] = db.from_utc_iso(s['ts']).isoformat()
                    payload['service'] = service or 'default'
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

@app.get("/api/debug/outage-counters")
async def debug_outage_counters(service: str | None = 'default'):
    """Expose internal counters & thresholds to diagnose missing outages.

    Returns:
      consec_fail, consec_success, current_outage_id, fail_threshold, recover_threshold,
      interval, method, target
    """
    # Access module-level vars directly (introspective diagnostics only)
    # Access per-service counters if available (multi-service); fallback to legacy keys if absent
    try:
        counters = monitoring.get_counters(service or 'default')
        if counters:
            return counters
    except Exception:
        pass
    # Legacy fallback (if single-service old attributes exist)
    return {"service": service or 'default'}

@app.get("/api/debug/recent-samples")
async def debug_recent_samples(limit: int = 50, service: str | None = 'default'):
    """Return the most recent raw samples (chronological) for correlation with outage logic."""
    # Reuse existing helper but we need id ordering preserved
    conn = await db.get_db()
    cur = await conn.execute("SELECT id, ts, success, latency_ms FROM latency_samples WHERE service = ? ORDER BY id DESC LIMIT ?", (service or 'default', limit))
    rows = await cur.fetchall()
    data = [dict(r) for r in rows]
    data.reverse()
    return data

@app.get("/api/debug/ongoing-outage")
async def debug_ongoing_outage(service: str | None = 'default'):
    """Return the currently open outage row (if any)."""
    outage = await db.ongoing_outage(service or 'default')
    if outage:
        outage['start_time_local'] = db.from_utc_iso(outage['start_time']).isoformat()
    return outage or {}

@app.get("/api/debug/first-samples")
async def debug_first_samples(limit: int = 5):
    conn = await db.get_db()
    cur = await conn.execute("SELECT id, ts, success, latency_ms FROM latency_samples ORDER BY id ASC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/services")
async def services_summary():
    names = monitoring.list_services()
    states = monitoring.current_state()
    return {"services": names, "states": states}

@app.get("/api/trends")
async def trends(days: int = 30, service: str | None = 'default'):
    # Keep this endpoint read-only and bounded for predictable payloads.
    days = max(7, min(days, 180))
    svc = service or 'default'
    conn = await db.get_db()

    latency_cur = await conn.execute(
        """
        SELECT
            date(ts) AS day,
            COUNT(*) AS samples,
            SUM(success) AS successes,
            SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
            AVG(CASE WHEN success = 1 THEN latency_ms END) AS avg_latency_ms
        FROM latency_samples
        WHERE service = ? AND date(ts) >= date('now', ?)
        GROUP BY date(ts)
        ORDER BY day ASC
        """,
        (svc, f"-{days} days"),
    )
    latency_rows = [dict(r) for r in await latency_cur.fetchall()]

    outages_cur = await conn.execute(
        """
        SELECT
            date(start_time) AS day,
            COUNT(*) AS outage_count,
            SUM(COALESCE(duration_seconds, 0)) AS downtime_seconds
        FROM outages
        WHERE service = ? AND date(start_time) >= date('now', ?)
        GROUP BY date(start_time)
        ORDER BY day ASC
        """,
        (svc, f"-{days} days"),
    )
    outage_rows = [dict(r) for r in await outages_cur.fetchall()]
    outage_by_day = {r["day"]: r for r in outage_rows}
    speedtest_cur = await conn.execute(
        """
        SELECT
            date(ts) AS day,
            AVG(download_mbps) AS avg_download_mbps,
            AVG(upload_mbps) AS avg_upload_mbps,
            AVG(ping_ms) AS avg_ping_ms,
            COUNT(*) AS runs
        FROM speedtest_samples
        WHERE service = ? AND date(ts) >= date('now', ?)
        GROUP BY date(ts)
        ORDER BY day ASC
        """,
        (svc, f"-{days} days"),
    )
    speedtest_rows = [dict(r) for r in await speedtest_cur.fetchall()]
    speedtest_by_day = {r["day"]: r for r in speedtest_rows}

    end_day = dt.datetime.now(dt.timezone.utc).date()
    start_day = end_day - dt.timedelta(days=days - 1)
    latency_by_day = {r["day"]: r for r in latency_rows}
    series = []
    for i in range(days):
        day = (start_day + dt.timedelta(days=i)).isoformat()
        lat = latency_by_day.get(day, {})
        outages_day = outage_by_day.get(day, {})
        speedtest_day = speedtest_by_day.get(day, {})
        samples = int(lat.get("samples") or 0)
        successes = int(lat.get("successes") or 0)
        failures = int(lat.get("failures") or 0)
        loss_pct = (failures / samples * 100.0) if samples else 0.0
        series.append({
            "day": day,
            "samples": samples,
            "successes": successes,
            "failures": failures,
            "packet_loss_pct": loss_pct,
            "avg_latency_ms": lat.get("avg_latency_ms"),
            "outages": int(outages_day.get("outage_count") or 0),
            "downtime_seconds": float(outages_day.get("downtime_seconds") or 0.0),
            "avg_download_mbps": speedtest_day.get("avg_download_mbps"),
            "avg_upload_mbps": speedtest_day.get("avg_upload_mbps"),
            "avg_speedtest_ping_ms": speedtest_day.get("avg_ping_ms"),
            "speedtest_runs": int(speedtest_day.get("runs") or 0),
        })

    total_samples = sum(s["samples"] for s in series)
    total_failures = sum(s["failures"] for s in series)
    total_outages = sum(s["outages"] for s in series)
    total_downtime = sum(s["downtime_seconds"] for s in series)
    overall_loss = (total_failures / total_samples * 100.0) if total_samples else 0.0
    avg_daily_samples = (total_samples / days) if days else 0.0

    worst_loss_day = max(series, key=lambda s: s["packet_loss_pct"]) if series else None
    peak_samples_day = max(series, key=lambda s: s["samples"]) if series else None
    spikes = []
    spike_threshold = avg_daily_samples * 1.5
    for row in series:
        if row["samples"] >= 50 and row["samples"] > spike_threshold:
            spikes.append({
                "day": row["day"],
                "samples": row["samples"],
                "ratio_vs_avg": (row["samples"] / avg_daily_samples) if avg_daily_samples else None,
            })

    summary = {
        "total_samples": total_samples,
        "total_failures": total_failures,
        "overall_packet_loss_pct": overall_loss,
        "total_outages": total_outages,
        "total_downtime_seconds": total_downtime,
        "avg_daily_samples": avg_daily_samples,
        "worst_loss_day": worst_loss_day,
        "peak_samples_day": peak_samples_day,
        "spike_days": len(spikes),
        "speedtest_runs": sum(s["speedtest_runs"] for s in series),
    }

    return {
        "service": svc,
        "days": days,
        "tz": "UTC",
        "series": series,
        "summary": summary,
        "spikes": spikes,
    }

@app.get("/api/webhook/status")
async def webhook_status():
    return webhooks.status()

@app.post("/api/webhook/test")
async def webhook_test(event: str = "start"):
    try:
        payload = await webhooks.test_fire("end" if event.lower() == "end" else "start")
        return {"sent": True, "payload": payload, "status": webhooks.status()}
    except Exception as e:
        return {"sent": False, "error": str(e), "status": webhooks.status()}

@app.post("/api/webhook/example-outage")
async def webhook_example_outage():
    """Fire a synthetic outage covering ~37s to test end-only (or start+end) delivery.

    Returns structure with flags start_sent/end_sent and the payloads used.
    """
    try:
        result = await webhooks.fire_example_outage()
        return {"ok": True, **result, "status": webhooks.status()}
    except Exception as e:
        return {"ok": False, "error": str(e), "status": webhooks.status()}


@app.post("/api/webhook/test-external")
async def webhook_test_external():
    try:
        result = await webhooks.fire_test_webhook()
        return {**result, "status": webhooks.status()}
    except Exception as e:
        return {"ok": False, "error": str(e), "status": webhooks.status()}
