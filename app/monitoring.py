"""Simplified monitoring loop implementation."""
import asyncio
import datetime as dt
import logging
import os
import platform
import re
from typing import Optional
import httpx
from . import db

log = logging.getLogger(__name__)

INTERVAL = float(os.getenv("CHECK_INTERVAL", "1"))
TARGET = os.getenv("TARGET_HOST", "8.8.8.8")
METHOD = os.getenv("CHECK_METHOD", "ping")
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "2"))
RECOVER_THRESHOLD = int(os.getenv("RECOVER_THRESHOLD", "2"))

consec_fail = 0
consec_success = 0
current_outage_id: Optional[int] = None
last_check_ok: Optional[bool] = None
last_latency_ms: Optional[float] = None
check_count = 0
_stop_event: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None

def _parse_ping_time(out: str) -> Optional[float]:
    match = re.search(r"time[=<]([\d.]+)\s*ms", out, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None

async def _ping() -> bool:
    global last_latency_ms
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    cmd = ["ping", count_flag, "1", timeout_flag, "1", TARGET]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="ignore")
        success = proc.returncode == 0
        if success:
            last_latency_ms = _parse_ping_time(out)
        else:
            err = stderr.decode(errors="ignore")
            log.warning("Ping failed to %s: %s", TARGET, err.strip())
        return success
    except Exception as e:
        log.error("Error running ping command: %s", e)
        return False

async def _http_check() -> bool:
    global last_latency_ms
    url = TARGET
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"http://{url}"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            start = dt.datetime.utcnow()
            r = await client.get(url)
            if 200 <= r.status_code < 400:
                last_latency_ms = (dt.datetime.utcnow() - start).total_seconds()*1000
                return True
            return False
    except Exception as e:
        log.warning("HTTP check to %s failed: %s", url, e)
        return False

async def _check() -> bool:
    return await _http_check() if METHOD == "http" else await _ping()

async def _loop():
    global consec_fail, consec_success, current_outage_id, last_check_ok, check_count
    log.info("Simplified monitor loop started interval=%.2fs target=%s method=%s", INTERVAL, TARGET, METHOD)
    # Diagnostic sample
    try:
        ts = dt.datetime.now(dt.timezone.utc)
        await db.add_latency_sample(ts, True, 0.0)
        log.info("Inserted diagnostic sample ts=%s", ts)
    except Exception as e:
        log.error("Diagnostic insert failed: %s", e)
    while not _stop_event.is_set():  # type: ignore
        try:
            start = dt.datetime.utcnow()
            ok = await _check()
            now = dt.datetime.now(dt.timezone.utc)
            last_check_ok = ok
            check_count += 1
            try:
                await db.add_latency_sample(now, ok, last_latency_ms if ok else None)
            except Exception as e:
                log.error("Add sample failed: %s", e)
            if ok:
                consec_success += 1
                consec_fail = 0
                if current_outage_id is not None and consec_success >= RECOVER_THRESHOLD:
                    outage = await db.ongoing_outage()
                    if outage and outage['id'] == current_outage_id:
                        start_time = dt.datetime.fromisoformat(outage['start_time'])
                        duration = (now - start_time).total_seconds()
                        await db.end_outage(current_outage_id, now, duration)
                    current_outage_id = None
            else:
                consec_fail += 1
                consec_success = 0
                if current_outage_id is None and consec_fail >= FAIL_THRESHOLD:
                    current_outage_id = await db.create_outage(now)
            if check_count % 20 == 0:
                log.info("Stats: checks=%d last_ok=%s latency=%.2f consec_ok=%d consec_fail=%d", check_count, ok, (last_latency_ms or 0.0), consec_success, consec_fail)
            elapsed = (dt.datetime.utcnow() - start).total_seconds()
            await asyncio.sleep(max(0, INTERVAL - elapsed))
            if check_count % 500 == 0:
                try:
                    await db.prune_latency_samples()
                except Exception as e:
                    log.error("Prune failed: %s", e)
        except Exception as loop_err:
            log.exception("Loop exception: %s", loop_err)
            await asyncio.sleep(INTERVAL)

async def start():
    global _stop_event, _task
    if _task is not None:
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop())

async def stop():
    if _stop_event is None:
        return
    _stop_event.set()
    if _task:
        await _task

def current_state():
    return {
        "interval": INTERVAL,
        "target": TARGET,
        "method": METHOD,
        "last_ok": last_check_ok,
        "ongoing_outage": current_outage_id is not None,
        "last_latency_ms": last_latency_ms,
        "consecutive_failures": consec_fail,
        "consecutive_successes": consec_success,
        "checks": check_count,
    }
