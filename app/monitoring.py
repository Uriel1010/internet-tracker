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
from . import webhooks

log = logging.getLogger(__name__)

INTERVAL = float(os.getenv("CHECK_INTERVAL", "1"))
TARGET = os.getenv("TARGET_HOST", "8.8.8.8")
METHOD = os.getenv("CHECK_METHOD", "ping")
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "2"))
RECOVER_THRESHOLD = int(os.getenv("RECOVER_THRESHOLD", "2"))

"""Multi-service monitoring module.

Supports legacy single-service mode (env vars CHECK_INTERVAL, TARGET_HOST, CHECK_METHOD, FAIL_THRESHOLD, RECOVER_THRESHOLD)
or multi-service mode via MULTI_SERVICES env containing JSON list of service definitions:

MULTI_SERVICES='[
  {"name":"dns","method":"ping","target":"8.8.8.8","interval":1,"fail_threshold":2,"recover_threshold":2},
  {"name":"web","method":"http","target":"https://example.com","interval":5}
]'

Fields:
  name (str, required)
  method: ping|http (default ping)
  target: host or URL (required)
  interval: seconds (default 1)
  fail_threshold: consecutive fails to open outage (default 2)
  recover_threshold: consecutive successes to close outage (default 2)
"""

import asyncio
import datetime as dt
import logging
import os
import platform
import re
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, List
import httpx
from . import db

log = logging.getLogger(__name__)

@dataclass
class ServiceConfig:
    name: str
    method: str = "ping"
    target: str = "8.8.8.8"
    interval: float = 1.0
    fail_threshold: int = 2
    recover_threshold: int = 2

@dataclass
class ServiceState:
    config: ServiceConfig
    consec_fail: int = 0
    consec_success: int = 0
    current_outage_id: Optional[int] = None
    last_ok: Optional[bool] = None
    last_latency_ms: Optional[float] = None
    check_count: int = 0
    first_failure_time: Optional[dt.datetime] = None
    stop_event: Optional[asyncio.Event] = None
    task: Optional[asyncio.Task] = None

_services: Dict[str, ServiceState] = {}
_global_stop: Optional[asyncio.Event] = None

def _parse_ping_time(out: str) -> Optional[float]:
    match = re.search(r"time[=<]([\d.]+)\s*ms", out, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None

async def _ping(target: str, state: ServiceState) -> bool:
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    cmd = ["ping", count_flag, "1", timeout_flag, "1", target]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="ignore")
        success = proc.returncode == 0
        if success:
            state.last_latency_ms = _parse_ping_time(out)
        else:
            err = stderr.decode(errors="ignore")
            log.warning("Ping failed service=%s target=%s: %s", state.config.name, target, err.strip())
        return success
    except Exception as e:
        log.error("Error running ping command for service=%s target=%s: %s", state.config.name, target, e)
        return False

async def _http_check(target: str, state: ServiceState) -> bool:
    url = target
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"http://{url}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            start = dt.datetime.utcnow()
            r = await client.get(url)
            if 200 <= r.status_code < 400:
                state.last_latency_ms = (dt.datetime.utcnow() - start).total_seconds()*1000
                return True
            return False
    except Exception as e:
        log.warning("HTTP check failed service=%s url=%s: %s", state.config.name, url, e)
        return False

async def _service_loop(state: ServiceState):
    cfg = state.config
    log.info("Service loop start name=%s target=%s method=%s interval=%.2f", cfg.name, cfg.target, cfg.method, cfg.interval)
    # Resume ongoing outage if present
    try:
        ongoing = await db.ongoing_outage(cfg.name)
        if ongoing:
            state.current_outage_id = ongoing['id']
            try:
                state.first_failure_time = dt.datetime.fromisoformat(ongoing['start_time'])
            except Exception:
                state.first_failure_time = None
            log.info("Resumed outage service=%s id=%s", cfg.name, ongoing['id'])
    except Exception as e:
        log.error("Resume outage failed service=%s: %s", cfg.name, e)
    # diagnostic sample
    try:
        ts = dt.datetime.now(dt.timezone.utc)
        await db.add_latency_sample(ts, True, 0.0, cfg.name)
    except Exception as e:
        log.error("Diagnostic sample insert failed service=%s: %s", cfg.name, e)
    while not state.stop_event.is_set():  # type: ignore
        start_clock = dt.datetime.utcnow()
        try:
            if cfg.method == "http":
                ok = await _http_check(cfg.target, state)
            else:
                ok = await _ping(cfg.target, state)
            now = dt.datetime.now(dt.timezone.utc)
            state.last_ok = ok
            state.check_count += 1
            try:
                await db.add_latency_sample(now, ok, state.last_latency_ms if ok else None, cfg.name)
            except Exception as e:
                log.error("Add sample failed service=%s: %s", cfg.name, e)
            if ok:
                state.consec_success += 1
                if state.consec_fail > 0 and state.first_failure_time and state.consec_fail < cfg.fail_threshold:
                    log.debug("Failure streak cleared before threshold service=%s fails=%d", cfg.name, state.consec_fail)
                state.consec_fail = 0
                state.first_failure_time = None
                if state.current_outage_id is not None and state.consec_success >= cfg.recover_threshold:
                    outage = await db.ongoing_outage(cfg.name)
                    if outage and outage['id'] == state.current_outage_id:
                        start_time = dt.datetime.fromisoformat(outage['start_time'])
                        duration = (now - start_time).total_seconds()
                        await db.end_outage(state.current_outage_id, now, duration)
                        log.info("Outage ended service=%s id=%s duration=%.2fs", cfg.name, state.current_outage_id, duration)
                        try:
                            webhooks.notify_outage_end(state, state.current_outage_id, start_time, now, duration)
                        except Exception as e:
                            log.debug("Webhook outage end notify failed service=%s err=%s", cfg.name, e)
                    state.current_outage_id = None
            else:
                state.consec_fail += 1
                state.consec_success = 0
                if state.consec_fail == 1:
                    state.first_failure_time = now
                if state.current_outage_id is None and state.consec_fail >= cfg.fail_threshold:
                    outage_start = state.first_failure_time or now
                    state.current_outage_id = await db.create_outage(outage_start, cfg.name)
                    log.info("Outage started service=%s id=%s start=%s (threshold=%d)", cfg.name, state.current_outage_id, outage_start.isoformat(), cfg.fail_threshold)
                    try:
                        webhooks.notify_outage_start(state, state.current_outage_id, outage_start)
                    except Exception as e:
                        log.debug("Webhook outage start notify failed service=%s err=%s", cfg.name, e)
            if state.check_count % 20 == 0:
                log.info("Stats service=%s checks=%d last_ok=%s latency=%.2f consec_ok=%d consec_fail=%d", cfg.name, state.check_count, ok, (state.last_latency_ms or 0.0), state.consec_success, state.consec_fail)
            elapsed = (dt.datetime.utcnow() - start_clock).total_seconds()
            await asyncio.sleep(max(0, cfg.interval - elapsed))
            if state.check_count % 500 == 0:
                try:
                    await db.prune_latency_samples(service=cfg.name)
                except Exception as e:
                    log.error("Prune failed service=%s: %s", cfg.name, e)
        except Exception as e:
            log.exception("Loop exception service=%s: %s", cfg.name, e)
            await asyncio.sleep(cfg.interval)

def _load_configs() -> List[ServiceConfig]:
    raw = os.getenv("MULTI_SERVICES")
    if not raw:
        # Legacy single config
        return [ServiceConfig(
            name="default",
            method=os.getenv("CHECK_METHOD", "ping"),
            target=os.getenv("TARGET_HOST", "8.8.8.8"),
            interval=float(os.getenv("CHECK_INTERVAL", "1")),
            fail_threshold=int(os.getenv("FAIL_THRESHOLD", "2")),
            recover_threshold=int(os.getenv("RECOVER_THRESHOLD", "2")),
        )]
    try:
        data = json.loads(raw)
        configs: List[ServiceConfig] = []
        for entry in data:
            configs.append(ServiceConfig(
                name=entry.get("name"),
                method=entry.get("method", "ping"),
                target=entry.get("target"),
                interval=float(entry.get("interval", 1)),
                fail_threshold=int(entry.get("fail_threshold", 2)),
                recover_threshold=int(entry.get("recover_threshold", 2)),
            ))
        return [c for c in configs if c.name and c.target]
    except Exception as e:
        log.error("Failed to parse MULTI_SERVICES: %s", e)
        return []

async def start():
    global _services, _global_stop
    if _services:  # already started
        return
    cfgs = _load_configs()
    if not cfgs:
        log.warning("No service configs loaded; nothing to monitor")
        return
    _global_stop = asyncio.Event()
    for cfg in cfgs:
        state = ServiceState(config=cfg, stop_event=asyncio.Event())
        _services[cfg.name] = state
        state.task = asyncio.create_task(_service_loop(state))

async def stop():
    for st in _services.values():
        if st.stop_event:
            st.stop_event.set()
    await asyncio.gather(*[st.task for st in _services.values() if st.task], return_exceptions=True)
    _services.clear()

def list_services() -> List[str]:
    return list(_services.keys())

def current_state(service: Optional[str] = None):
    if service:
        st = _services.get(service)
        if not st:
            return {}
        return _state_dict(st)
    # aggregate / multi
    return { name: _state_dict(st) for name, st in _services.items() }

def _state_dict(st: ServiceState):
    return {
        "name": st.config.name,
        "interval": st.config.interval,
        "target": st.config.target,
        "method": st.config.method,
        "last_ok": st.last_ok,
        "ongoing_outage": st.current_outage_id is not None,
        "last_latency_ms": st.last_latency_ms,
        "consecutive_failures": st.consec_fail,
        "consecutive_successes": st.consec_success,
        "checks": st.check_count,
        "fail_threshold": st.config.fail_threshold,
        "recover_threshold": st.config.recover_threshold,
    }

def get_counters(service: str):
    st = _services.get(service)
    if not st:
        return {}
    return {
        "consec_fail": st.consec_fail,
        "consec_success": st.consec_success,
        "current_outage_id": st.current_outage_id,
        "fail_threshold": st.config.fail_threshold,
        "recover_threshold": st.config.recover_threshold,
        "interval": st.config.interval,
        "method": st.config.method,
        "target": st.config.target,
        "service": service,
    }
