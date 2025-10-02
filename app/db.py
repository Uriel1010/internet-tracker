import aiosqlite
import asyncio
import logging
import os
from typing import List, Dict, Any, Optional
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DB_PATH = os.getenv("DB_PATH", "/data/data.sqlite3")
log = logging.getLogger(__name__)

_db: Optional[aiosqlite.Connection] = None

def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")

# Convert to UTC before storing
def to_utc_iso(ts: dt.datetime) -> str:
    """Convert a datetime (naive or aware) to a UTC ISO string.

    If naive, assume it is in the configured local TZ (TZ env) before converting.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=get_tz())
    return ts.astimezone(dt.timezone.utc).isoformat()

# Convert from UTC when retrieving
def from_utc_iso(ts_str: str) -> dt.datetime:
    # Stored values should always include UTC offset, but be defensive
    dt_obj = dt.datetime.fromisoformat(ts_str)
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
    return dt_obj.astimezone(get_tz())

def current_tz_name() -> str:
    tz = get_tz()
    return getattr(tz, 'key', str(tz))


INIT_SQL = """
CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_outages_start_time ON outages(start_time);
CREATE TABLE IF NOT EXISTS latency_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency_samples(ts);
"""

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
    return _db

async def init_db():
    db = await get_db()
    await db.executescript(INIT_SQL)
    await db.commit()
    # Migration: conditionally add service columns
    for table in ("outages", "latency_samples"):
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in await cur.fetchall()]
        if 'service' not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN service TEXT DEFAULT 'default'")
    await db.commit()
    # Ensure indexes on service columns
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS idx_outages_service ON outages(service)")
    except Exception:
        pass
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS idx_latency_service ON latency_samples(service)")
    except Exception:
        pass
    await db.commit()

async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

async def create_outage(start_time: dt.datetime, service: str = 'default') -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO outages (start_time, service) VALUES (?, ?)", (to_utc_iso(start_time), service)
    )
    await db.commit()
    return cur.lastrowid

async def end_outage(outage_id: int, end_time: dt.datetime, duration_seconds: float):
    db = await get_db()
    await db.execute(
        "UPDATE outages SET end_time = ?, duration_seconds = ? WHERE id = ?",
        (to_utc_iso(end_time), duration_seconds, outage_id),
    )
    await db.commit()

async def list_outages(limit: Optional[int] = None, service: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT id, start_time, end_time, duration_seconds, service FROM outages"
    params: tuple = ()
    if service:
        q += " WHERE service = ?"
        params = (service,)
    q += " ORDER BY start_time DESC"
    if limit:
        q += " LIMIT ?"
        params = params + (limit,) if params else (limit,)
    db = await get_db()
    cur = await db.execute(q, params)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def ongoing_outage(service: str = 'default') -> Optional[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, start_time, service FROM outages WHERE end_time IS NULL AND service = ? ORDER BY id DESC LIMIT 1",
        (service,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None

async def add_latency_sample(ts: dt.datetime, success: bool, latency_ms: Optional[float], service: str = 'default'):
    db = await get_db()
    await db.execute(
        "INSERT INTO latency_samples (ts, success, latency_ms, service) VALUES (?,?,?,?)",
        (to_utc_iso(ts), 1 if success else 0, latency_ms, service),
    )
    await db.commit()

async def recent_latency_samples(limit: int = 300, service: str = 'default'):
    db = await get_db()
    cur = await db.execute(
        "SELECT ts, success, latency_ms FROM latency_samples WHERE service = ? ORDER BY id DESC LIMIT ?",
        (service, limit),
    )
    rows = await cur.fetchall()
    data = [dict(r) for r in rows]
    data.reverse()  # chronological
    return data

async def prune_latency_samples(keep: int = 10000, service: str = 'default'):
    db = await get_db()
    # Find the ID of the nth most recent sample
    cursor = await db.execute(
        "SELECT id FROM latency_samples WHERE service = ? ORDER BY id DESC LIMIT 1 OFFSET ?", (service, keep)
    )
    row = await cursor.fetchone()
    if row:
        cutoff_id = row["id"]
        # Delete all samples with an ID less than or equal to the cutoff ID
        res = await db.execute(
            "DELETE FROM latency_samples WHERE id <= ? AND service = ?", (cutoff_id, service)
        )
        await db.commit()
        if res.rowcount > 0:
            log.info(f"Pruned {res.rowcount} old latency samples for service={service}")

async def latency_samples_since(ts: dt.datetime, service: str = 'default'):
    db = await get_db()
    cur = await db.execute(
        "SELECT id, ts, success, latency_ms FROM latency_samples WHERE ts >= ? AND service = ? ORDER BY id ASC",
        (to_utc_iso(ts), service),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def latest_latency_id(service: str = 'default') -> int:
    db = await get_db()
    cur = await db.execute("SELECT id FROM latency_samples WHERE service = ? ORDER BY id DESC LIMIT 1", (service,))
    row = await cur.fetchone()
    return row[0] if row else 0

async def latency_samples_after_id(last_id: int, service: str = 'default'):
    db = await get_db()
    cur = await db.execute(
        "SELECT id, ts, success, latency_ms FROM latency_samples WHERE id > ? AND service = ? ORDER BY id ASC",
        (last_id, service),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]
