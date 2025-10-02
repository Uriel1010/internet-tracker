import os
import logging
import datetime as dt
import asyncio
from typing import Optional, Dict, Any
import httpx

from . import db

log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
WEBHOOK_TIMEOUT = float(os.getenv("ALERT_WEBHOOK_TIMEOUT", "5"))
SEND_END_EVENT = os.getenv("ALERT_WEBHOOK_SEND_END", "true").lower() in {"1", "true", "yes", "on"}
# New: allow disabling start event (user requested only end notifications)
SEND_START_EVENT = os.getenv("ALERT_WEBHOOK_SEND_START", "false").lower() in {"1", "true", "yes", "on"}

_last_success: Optional[dt.datetime] = None
_last_error: Optional[str] = None
_last_status_code: Optional[int] = None
_last_event: Optional[str] = None

_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)
    return _client


def configured() -> bool:
    return bool(WEBHOOK_URL)


def status() -> Dict[str, Any]:
    return {
        "configured": configured(),
        "url": WEBHOOK_URL if configured() else None,
        "last_success": _last_success.isoformat() if _last_success else None,
        "last_error": _last_error,
        "last_status_code": _last_status_code,
        "last_event": _last_event,
    "send_end_event": SEND_END_EVENT,
    "send_start_event": SEND_START_EVENT,
        "timeout_seconds": WEBHOOK_TIMEOUT,
    }


async def _post(payload: Dict[str, Any]):
    global _last_success, _last_error, _last_status_code, _last_event
    if not configured():
        return
    try:
        async with _lock:  # serialize sends
            client = await _get_client()
            resp = await client.post(WEBHOOK_URL, json=payload)
        _last_status_code = resp.status_code
        if 200 <= resp.status_code < 300:
            _last_success = dt.datetime.now(dt.timezone.utc)
            _last_error = None
        else:
            _last_error = f"HTTP {resp.status_code} body={resp.text[:200]}"
        _last_event = payload.get("event")
        log.info("Webhook sent event=%s status=%s", payload.get("event"), resp.status_code)
    except Exception as e:
        _last_error = str(e)
        _last_status_code = None
        _last_event = payload.get("event")
        log.warning("Webhook send failed event=%s error=%s", payload.get("event"), e)


def _local_iso(dt_obj: dt.datetime) -> str:
    # Convert via db helpers to respect TZ env
    return db.from_utc_iso(db.to_utc_iso(dt_obj)).isoformat()


def outage_start_payload(service_state, outage_id: int, start_time: dt.datetime) -> Dict[str, Any]:
    cfg = service_state.config
    return {
        "event": "outage.start",
        "service": cfg.name,
        "outage_id": outage_id,
        "start_time": db.to_utc_iso(start_time),
        "start_time_local": _local_iso(start_time),
        "target": cfg.target,
        "method": cfg.method,
        "interval": cfg.interval,
        "fail_threshold": cfg.fail_threshold,
        "recover_threshold": cfg.recover_threshold,
    }


def outage_end_payload(service_state, outage_id: int, start_time: dt.datetime, end_time: dt.datetime, duration: float) -> Dict[str, Any]:
    cfg = service_state.config
    return {
        "event": "outage.end",
        "service": cfg.name,
        "outage_id": outage_id,
        "start_time": db.to_utc_iso(start_time),
        "start_time_local": _local_iso(start_time),
        "end_time": db.to_utc_iso(end_time),
        "end_time_local": _local_iso(end_time),
        "duration_seconds": duration,
        "target": cfg.target,
        "method": cfg.method,
        "interval": cfg.interval,
        "fail_threshold": cfg.fail_threshold,
        "recover_threshold": cfg.recover_threshold,
    }


def notify_outage_start(service_state, outage_id: int, start_time: dt.datetime):
    if not configured() or not SEND_START_EVENT:
        return
    payload = outage_start_payload(service_state, outage_id, start_time)
    asyncio.create_task(_post(payload))


def notify_outage_end(service_state, outage_id: int, start_time: dt.datetime, end_time: dt.datetime, duration: float):
    if not configured() or not SEND_END_EVENT:
        return
    payload = outage_end_payload(service_state, outage_id, start_time, end_time, duration)
    asyncio.create_task(_post(payload))


async def test_fire(event: str = "start"):
    if not configured():
        raise RuntimeError("Webhook not configured")
    now = dt.datetime.now(dt.timezone.utc)
    fake_state = type("FakeState", (), {"config": type("Cfg", (), {"name": "test", "target": "example", "method": "ping", "interval": 1.0, "fail_threshold": 2, "recover_threshold": 2})()})()
    if event == "end":
        payload = outage_end_payload(fake_state, 0, now - dt.timedelta(seconds=42), now, 42.0)
        await _post(payload)
        return payload
    else:
        payload = outage_start_payload(fake_state, 0, now)
        if not SEND_START_EVENT:
            # Indicate skipped due to configuration
            payload["skipped"] = True
            payload["reason"] = "start events disabled"
            return payload
        await _post(payload)
        return payload


async def fire_example_outage():
    """Simulate a full outage lifecycle and send webhook(s).

    Behavior:
      * Always constructs a start + end payload spanning 37s.
      * Sends start only if SEND_START_EVENT is true (mirrors real logic).
      * Always sends end if SEND_END_EVENT true.
    Returns a dict with 'start_sent' (bool), 'end_sent' (bool), and the payloads.
    """
    if not configured():
        raise RuntimeError("Webhook not configured")
    now = dt.datetime.now(dt.timezone.utc)
    start_time = now - dt.timedelta(seconds=37)
    end_time = now
    fake_state = type("FakeState", (), {"config": type("Cfg", (), {"name": "example", "target": "example", "method": "ping", "interval": 1.0, "fail_threshold": 2, "recover_threshold": 2})()})()
    start_payload = outage_start_payload(fake_state, 9999, start_time)
    end_payload = outage_end_payload(fake_state, 9999, start_time, end_time, 37.0)
    start_sent = False
    end_sent = False
    if SEND_START_EVENT:
        asyncio.create_task(_post(start_payload))
        start_sent = True
    if SEND_END_EVENT:
        asyncio.create_task(_post(end_payload))
        end_sent = True
    return {
        "start_sent": start_sent,
        "end_sent": end_sent,
        "start": start_payload,
        "end": end_payload,
        "mode": {
            "send_start_event": SEND_START_EVENT,
            "send_end_event": SEND_END_EVENT,
        }
    }
