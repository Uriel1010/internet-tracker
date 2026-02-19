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
import shutil
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
    last_check_time: Optional[dt.datetime] = None
    last_ok_time: Optional[dt.datetime] = None
    last_error: Optional[str] = None
    first_failure_time: Optional[dt.datetime] = None
    stop_event: Optional[asyncio.Event] = None
    task: Optional[asyncio.Task] = None

_services: Dict[str, ServiceState] = {}
_global_stop: Optional[asyncio.Event] = None
_speedtest_task: Optional[asyncio.Task] = None
_speedtest_stop: Optional[asyncio.Event] = None

SPEEDTEST_ENABLED = os.getenv("SPEEDTEST_ENABLED", "false").lower() in ("1", "true", "yes", "on")
SPEEDTEST_INTERVAL = int(os.getenv("SPEEDTEST_INTERVAL", "1800"))  # 30m default
SPEEDTEST_SERVICE = os.getenv("SPEEDTEST_SERVICE", "default")
SPEEDTEST_TIMEOUT = int(os.getenv("SPEEDTEST_TIMEOUT", "90"))
SPEEDTEST_RETRIES = int(os.getenv("SPEEDTEST_RETRIES", "2"))

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
            state.last_error = None
        else:
            err = stderr.decode(errors="ignore")
            state.last_error = err.strip() or "ping failed"
            log.warning("Ping failed service=%s target=%s: %s", state.config.name, target, err.strip())
        return success
    except Exception as e:
        state.last_error = str(e)
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
                state.last_error = None
                return True
            state.last_error = f"http status {r.status_code}"
            return False
    except Exception as e:
        state.last_error = str(e)
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
            state.last_check_time = now
            state.last_ok = ok
            state.check_count += 1
            try:
                await db.add_latency_sample(now, ok, state.last_latency_ms if ok else None, cfg.name)
            except Exception as e:
                log.error("Add sample failed service=%s: %s", cfg.name, e)
            if ok:
                state.last_ok_time = now
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

def _speedtest_targets() -> List[str]:
    configured = list_services()
    raw = (SPEEDTEST_SERVICE or "default").strip()
    if raw.lower() in ("all", "*"):
        if configured:
            return configured
        return ["default"]
    if "," in raw:
        targets = [s.strip() for s in raw.split(",") if s.strip()]
        return targets or (configured if configured else ["default"])
    if configured and raw not in configured:
        # Auto-heal common misconfig: SPEEDTEST_SERVICE points to missing service.
        log.warning(
            "Speedtest service target '%s' is not configured. Falling back to all configured services=%s",
            raw, configured,
        )
        return configured
    return [raw]

async def _run_speedtest_once(service: str = 'default'):
    cmd = shutil.which("speedtest-cli") or shutil.which("speedtest")
    if not cmd:
        msg = "speedtest command missing"
        log.warning("Speedtest skipped service=%s category=command_missing msg=%s", service, msg)
        return {"ok": False, "service": service, "category": "command_missing", "error": msg}
    attempt = 0
    started = dt.datetime.now(dt.timezone.utc)
    while attempt <= SPEEDTEST_RETRIES:
        attempt += 1
        try:
            log.info("Speedtest started service=%s attempt=%d timeout=%ss", service, attempt, SPEEDTEST_TIMEOUT)
            proc = await asyncio.create_subprocess_exec(
                cmd,
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SPEEDTEST_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise asyncio.TimeoutError("speedtest timed out")
            if proc.returncode != 0:
                err = stderr.decode(errors="ignore").strip()
                category = "return_code"
                log.warning(
                    "Speedtest failed service=%s category=%s attempt=%d rc=%s err=%s",
                    service, category, attempt, proc.returncode, err
                )
                if attempt <= SPEEDTEST_RETRIES:
                    await asyncio.sleep(min(10, 2 ** attempt))
                    continue
                return {"ok": False, "service": service, "category": category, "error": err}

            payload = json.loads(stdout.decode(errors="ignore"))
            download_bps = payload.get("download")
            upload_bps = payload.get("upload")
            ping_ms = payload.get("ping")
            server_name = (payload.get("server") or {}).get("name")
            download_mbps = (float(download_bps) / 1_000_000.0) if download_bps is not None else None
            upload_mbps = (float(upload_bps) / 1_000_000.0) if upload_bps is not None else None
            now = dt.datetime.now(dt.timezone.utc)
            try:
                await db.add_speedtest_sample(
                    now,
                    download_mbps=download_mbps,
                    upload_mbps=upload_mbps,
                    ping_ms=float(ping_ms) if ping_ms is not None else None,
                    server_name=server_name,
                    service=service,
                )
            except Exception as e:
                log.error("Speedtest DB write failed service=%s category=db_write_error err=%s", service, e)
                return {"ok": False, "service": service, "category": "db_write_error", "error": str(e)}
            duration = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            log.info(
                "Speedtest succeeded service=%s down=%.2fMbps up=%.2fMbps ping=%s server=%s duration=%.2fs persisted=true",
                service,
                download_mbps or 0.0,
                upload_mbps or 0.0,
                ping_ms,
                server_name,
                duration,
            )
            return {
                "ok": True,
                "service": service,
                "ts": now.isoformat(),
                "download_mbps": download_mbps,
                "upload_mbps": upload_mbps,
                "ping_ms": float(ping_ms) if ping_ms is not None else None,
                "server_name": server_name,
            }
        except asyncio.TimeoutError as e:
            category = "timeout"
            log.warning("Speedtest failed service=%s category=%s attempt=%d err=%s", service, category, attempt, e)
            if attempt <= SPEEDTEST_RETRIES:
                await asyncio.sleep(min(10, 2 ** attempt))
                continue
            return {"ok": False, "service": service, "category": category, "error": str(e)}
        except json.JSONDecodeError as e:
            category = "parse_error"
            log.warning("Speedtest failed service=%s category=%s attempt=%d err=%s", service, category, attempt, e)
            return {"ok": False, "service": service, "category": category, "error": str(e)}
        except Exception as e:
            category = "execution_error"
            log.warning("Speedtest failed service=%s category=%s attempt=%d err=%s", service, category, attempt, e)
            if attempt <= SPEEDTEST_RETRIES:
                await asyncio.sleep(min(10, 2 ** attempt))
                continue
            return {"ok": False, "service": service, "category": category, "error": str(e)}
    return {"ok": False, "service": service, "category": "unknown", "error": "unreachable"}

async def _speedtest_loop():
    global _speedtest_stop
    log.info(
        "Speedtest loop started interval=%ss target=%s timeout=%ss retries=%s",
        SPEEDTEST_INTERVAL, SPEEDTEST_SERVICE, SPEEDTEST_TIMEOUT, SPEEDTEST_RETRIES
    )
    while _speedtest_stop and not _speedtest_stop.is_set():
        targets = _speedtest_targets()
        log.info("Speedtest scheduled targets=%s", targets)
        for svc in targets:
            try:
                await _run_speedtest_once(svc)
            except Exception as e:
                log.error("Speedtest loop error service=%s err=%s", svc, e)
        try:
            await asyncio.wait_for(_speedtest_stop.wait(), timeout=max(60, SPEEDTEST_INTERVAL))
        except asyncio.TimeoutError:
            pass
    log.info("Speedtest loop stopped")

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

def _legacy_config_for(name: str) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        method=os.getenv("CHECK_METHOD", "ping"),
        target=os.getenv("TARGET_HOST", "8.8.8.8"),
        interval=float(os.getenv("CHECK_INTERVAL", "1")),
        fail_threshold=int(os.getenv("FAIL_THRESHOLD", "2")),
        recover_threshold=int(os.getenv("RECOVER_THRESHOLD", "2")),
    )

async def start():
    global _services, _global_stop, _speedtest_task, _speedtest_stop
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
    if SPEEDTEST_ENABLED:
        _speedtest_stop = asyncio.Event()
        _speedtest_task = asyncio.create_task(_speedtest_loop())

async def stop():
    global _speedtest_task, _speedtest_stop
    for st in _services.values():
        if st.stop_event:
            st.stop_event.set()
    await asyncio.gather(*[st.task for st in _services.values() if st.task], return_exceptions=True)
    if _speedtest_stop:
        _speedtest_stop.set()
    if _speedtest_task:
        await asyncio.gather(_speedtest_task, return_exceptions=True)
        _speedtest_task = None
        _speedtest_stop = None
    _services.clear()

async def start_service(name: str):
    if name in _services:
        st = _services[name]
        return {"service": name, "already_running": bool(st.task and not st.task.done())}
    cfg = _legacy_config_for(name)
    state = ServiceState(config=cfg, stop_event=asyncio.Event())
    _services[name] = state
    state.task = asyncio.create_task(_service_loop(state))
    log.info("Dynamically started service=%s target=%s method=%s interval=%.2f", cfg.name, cfg.target, cfg.method, cfg.interval)
    return {"service": name, "already_running": False}

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
        "last_check_time": st.last_check_time.isoformat() if st.last_check_time else None,
        "last_ok_time": st.last_ok_time.isoformat() if st.last_ok_time else None,
        "last_error": st.last_error,
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

def service_runtime(service: str):
    st = _services.get(service)
    if not st:
        return {"configured": False, "running": False}
    task_running = bool(st.task and not st.task.done())
    return {"configured": True, "running": task_running}

async def run_single_check(service: str):
    st = _services.get(service)
    if not st:
        raise ValueError(f"service_not_configured:{service}")
    cfg = st.config
    if cfg.method == "http":
        ok = await _http_check(cfg.target, st)
    else:
        ok = await _ping(cfg.target, st)
    now = dt.datetime.now(dt.timezone.utc)
    st.last_check_time = now
    st.last_ok = ok
    st.check_count += 1
    if ok:
        st.last_ok_time = now
        st.consec_success += 1
        st.consec_fail = 0
    else:
        st.consec_fail += 1
        st.consec_success = 0
    try:
        await db.add_latency_sample(now, ok, st.last_latency_ms if ok else None, cfg.name)
    except Exception as e:
        log.error("Add sample failed during check-once service=%s: %s", service, e)
        raise
    return {
        "service": service,
        "ok": ok,
        "latency_ms": st.last_latency_ms if ok else None,
        "ts": now.isoformat(),
        "checks": st.check_count,
    }

async def run_speedtest_once(service: str):
    return await _run_speedtest_once(service)
