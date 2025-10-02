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

async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

async def create_outage(start_time: dt.datetime) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO outages (start_time) VALUES (?)", (to_utc_iso(start_time),)
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

async def list_outages(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    q = "SELECT id, start_time, end_time, duration_seconds FROM outages ORDER BY start_time DESC"
    if limit:
        q += " LIMIT ?"
    db = await get_db()
    cur = await db.execute(q, (limit,) if limit else ())
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def ongoing_outage() -> Optional[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, start_time FROM outages WHERE end_time IS NULL ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    return dict(row) if row else None

async def add_latency_sample(ts: dt.datetime, success: bool, latency_ms: Optional[float]):
    db = await get_db()
    await db.execute(
        "INSERT INTO latency_samples (ts, success, latency_ms) VALUES (?,?,?)",
        (to_utc_iso(ts), 1 if success else 0, latency_ms),
    )
    await db.commit()

async def recent_latency_samples(limit: int = 300):
    db = await get_db()
    cur = await db.execute(
        "SELECT ts, success, latency_ms FROM latency_samples ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    data = [dict(r) for r in rows]
    data.reverse()  # chronological
    return data

async def prune_latency_samples(keep: int = 10000):
    db = await get_db()
    # Find the ID of the nth most recent sample
    cursor = await db.execute(
        "SELECT id FROM latency_samples ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
    )
    row = await cursor.fetchone()
    if row:
        cutoff_id = row["id"]
        # Delete all samples with an ID less than or equal to the cutoff ID
        res = await db.execute(
            "DELETE FROM latency_samples WHERE id <= ?", (cutoff_id,)
        )
        await db.commit()
        if res.rowcount > 0:
            log.info(f"Pruned {res.rowcount} old latency samples")

async def latency_samples_since(ts: dt.datetime):
    db = await get_db()
    cur = await db.execute(
        "SELECT id, ts, success, latency_ms FROM latency_samples WHERE ts >= ? ORDER BY id ASC",
        (to_utc_iso(ts),),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def latest_latency_id() -> int:
    db = await get_db()
    cur = await db.execute("SELECT id FROM latency_samples ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    return row[0] if row else 0

async def latency_samples_after_id(last_id: int):
    db = await get_db()
    cur = await db.execute(
        "SELECT id, ts, success, latency_ms FROM latency_samples WHERE id > ? ORDER BY id ASC",
        (last_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]
