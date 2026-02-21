import os
import logging
import datetime as dt
import asyncio
from typing import Optional, Dict, Any
import httpx

from . import db

log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
DEFAULT_TEST_WEBHOOK_URL = "http://192.168.31.129:58080/send-all"
TEST_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_TEST_URL", DEFAULT_TEST_WEBHOOK_URL).strip()
WEBHOOK_TIMEOUT = float(os.getenv("ALERT_WEBHOOK_TIMEOUT", "5"))
SEND_END_EVENT = os.getenv("ALERT_WEBHOOK_SEND_END", "true").lower() in {"1", "true", "yes", "on"}
# New: allow disabling start event (user requested only end notifications)
SEND_START_EVENT = os.getenv("ALERT_WEBHOOK_SEND_START", "false").lower() in {"1", "true", "yes", "on"}
DEDUPE_WINDOW_SECONDS = float(os.getenv("ALERT_WEBHOOK_DEDUP_WINDOW_SECONDS", "300"))
DEFAULT_LAN_TELEGRAM_URL = "http://192.168.31.129:58080/send-all"

_last_success: Optional[dt.datetime] = None
_last_error: Optional[str] = None
_last_status_code: Optional[int] = None
_last_event: Optional[str] = None

_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()
_recent_outage_events: Dict[str, dt.datetime] = {}


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)
    return _client


def configured() -> bool:
    return bool(_target_url())


def _target_url() -> str:
    return WEBHOOK_URL or DEFAULT_LAN_TELEGRAM_URL


def status() -> Dict[str, Any]:
    return {
        "configured": configured(),
        "url": _target_url() if configured() else None,
        "last_success": _last_success.isoformat() if _last_success else None,
        "last_error": _last_error,
        "last_status_code": _last_status_code,
        "last_event": _last_event,
        "send_end_event": SEND_END_EVENT,
        "send_start_event": SEND_START_EVENT,
        "dedup_window_seconds": DEDUPE_WINDOW_SECONDS,
        "test_url": TEST_WEBHOOK_URL or None,
        "timeout_seconds": WEBHOOK_TIMEOUT,
    }


def _event_key(payload: Dict[str, Any]) -> Optional[str]:
    event = payload.get("event")
    outage_id = payload.get("outage_id")
    if not event or outage_id is None:
        return None
    service = payload.get("service") or "default"
    return f"{event}:{service}:{outage_id}"


def _should_send(payload: Dict[str, Any]) -> bool:
    if DEDUPE_WINDOW_SECONDS <= 0:
        return True
    key = _event_key(payload)
    if not key:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=DEDUPE_WINDOW_SECONDS)
    stale_keys = [k for k, ts in _recent_outage_events.items() if ts < cutoff]
    for stale_key in stale_keys:
        _recent_outage_events.pop(stale_key, None)
    prev = _recent_outage_events.get(key)
    if prev and prev >= cutoff:
        return False
    _recent_outage_events[key] = now
    return True


async def _post(payload: Dict[str, Any]):
    global _last_success, _last_error, _last_status_code, _last_event
    if not configured():
        return
    try:
        async with _lock:  # serialize sends
            if not _should_send(payload):
                log.info("Webhook deduplicated event=%s service=%s outage_id=%s",
                         payload.get("event"), payload.get("service"), payload.get("outage_id"))
                _last_event = payload.get("event")
                _last_status_code = None
                _last_error = None
                return
            client = await _get_client()
            outbound = {"text": payload.get("text", "")}
            resp = await client.post(_target_url(), json=outbound)
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


def _local_display(dt_obj: dt.datetime) -> str:
    return db.from_utc_iso(db.to_utc_iso(dt_obj)).strftime("%Y-%m-%d • %H:%M")


def _duration_display(duration: float) -> str:
    return f"{duration:.6f}"


def _outage_text(start_time: dt.datetime, end_time: dt.datetime, duration: float) -> str:
    return (
        f"from {_local_display(start_time)} to {_local_display(end_time)} "
        f"for {_duration_display(duration)} seconds"
    )


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
        "text": _outage_text(start_time, start_time, 0.0),
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
        "text": _outage_text(start_time, end_time, duration),
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


async def fire_test_webhook() -> Dict[str, Any]:
    """Send a synthetic outage.end message to the configured test webhook URL."""
    if not TEST_WEBHOOK_URL:
        raise RuntimeError("Test webhook URL not configured")
    now = dt.datetime.now(dt.timezone.utc)
    fake_state = type(
        "TestState",
        (),
        {
            "config": type(
                "Cfg",
                (),
                {
                    "name": "test-webhook",
                    "target": "webhook-test",
                    "method": "manual",
                    "interval": 0.0,
                    "fail_threshold": 0,
                    "recover_threshold": 0,
                },
            )()
        },
    )()
    payload = outage_end_payload(fake_state, 4242, now - dt.timedelta(seconds=15), now, 15.0)
    try:
        async with _lock:
            client = await _get_client()
            resp = await client.post(TEST_WEBHOOK_URL, json={"text": payload.get("text", "")})
    except Exception as exc:
        raise RuntimeError(f"Test webhook send failed: {exc}") from exc
    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "response_text": resp.text[:200] if resp.text else "",
        "payload": payload,
        "test_url": TEST_WEBHOOK_URL,
    }
