"""Durable caches and counters, kept in a small SQLite file.

Everything in here survives a redeploy on purpose. The whole point of caching a
travel time is to never pay OpenRouteService for it twice, and an in-process
dict lost the lot on every container restart — so each deploy re-burned the
daily quota re-learning what it already knew. The daily budget lives here for
the same reason: redeploying must not hand out a fresh allowance.
"""
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

CACHE_DB = os.getenv("CACHE_DB", "cache.db")
# Travel times are also kept in front of SQLite in a bounded LRU, so a busy
# request does not hit the disk hundreds of times. Bounded, because the old
# unbounded dict grew for as long as the process lived.
MEM_CACHE_SIZE = int(os.getenv("MEM_CACHE_SIZE", "20000") or 20000)
GEOCODE_TTL_DAYS = float(os.getenv("GEOCODE_TTL_DAYS", "30") or 30)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS travel_time (
    mode   TEXT NOT NULL,
    h_lat  REAL NOT NULL, h_lng REAL NOT NULL,
    t_lat  REAL NOT NULL, t_lng REAL NOT NULL,
    -- NULL means "asked, and there is no route": cached so we never ask again.
    seconds REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mode, h_lat, h_lng, t_lat, t_lng)
);
CREATE TABLE IF NOT EXISTS geocode (
    query TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS budget (
    day TEXT PRIMARY KEY,
    spent INTEGER NOT NULL
);
"""

UNREACHABLE = float("inf")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    """SQLite-backed caches. Every call is short and local, so it runs inline
    on the event loop rather than dragging in an async driver."""

    def __init__(self, path: str = CACHE_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        # WAL keeps reads from blocking on the occasional write.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._mem: OrderedDict[tuple, float] = OrderedDict()

    # ---------- travel times ----------

    def get_time(self, key: tuple) -> float | None:
        """Seconds for a (mode, home, target) pair, `inf` if known-unroutable,
        or None if we have never asked."""
        if key in self._mem:
            self._mem.move_to_end(key)
            return self._mem[key]
        with self._lock:
            row = self._db.execute(
                "SELECT seconds FROM travel_time"
                " WHERE mode=? AND h_lat=? AND h_lng=? AND t_lat=? AND t_lng=?",
                key,
            ).fetchone()
        if row is None:
            return None
        secs = UNREACHABLE if row[0] is None else float(row[0])
        self._remember(key, secs)
        return secs

    def set_time(self, key: tuple, seconds: float) -> None:
        self._remember(key, seconds)
        stored = None if seconds == UNREACHABLE else float(seconds)
        with self._lock:
            self._db.execute(
                "INSERT INTO travel_time (mode, h_lat, h_lng, t_lat, t_lng, seconds, updated_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(mode, h_lat, h_lng, t_lat, t_lng)"
                " DO UPDATE SET seconds=excluded.seconds, updated_at=excluded.updated_at",
                (*key, stored, time.time()),
            )
            self._db.commit()

    def _remember(self, key: tuple, seconds: float) -> None:
        self._mem[key] = seconds
        self._mem.move_to_end(key)
        while len(self._mem) > MEM_CACHE_SIZE:
            self._mem.popitem(last=False)

    # ---------- geocoding ----------

    def get_geocode(self, query: str):
        """Cached Nominatim answer, or None. Their usage policy asks us to cache
        rather than re-ask, and place names are not exactly volatile."""
        with self._lock:
            row = self._db.execute(
                "SELECT payload, updated_at FROM geocode WHERE query=?", (query,)
            ).fetchone()
        if row is None or time.time() - row[1] > GEOCODE_TTL_DAYS * 86400:
            return None
        return json.loads(row[0])

    def set_geocode(self, query: str, payload) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO geocode (query, payload, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(query) DO UPDATE SET payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (query, json.dumps(payload), time.time()),
            )
            self._db.commit()

    # ---------- daily routing budget ----------

    def spent_today(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT spent FROM budget WHERE day=?", (_today(),)).fetchone()
        return int(row[0]) if row else 0

    def spend(self, calls: int) -> None:
        """Record routing calls against today's allowance."""
        with self._lock:
            self._db.execute(
                "INSERT INTO budget (day, spent) VALUES (?,?)"
                " ON CONFLICT(day) DO UPDATE SET spent = spent + excluded.spent",
                (_today(), calls),
            )
            # Yesterday's counters are of no use to anyone.
            self._db.execute("DELETE FROM budget WHERE day < date('now', '-7 day')")
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            times = self._db.execute("SELECT COUNT(*) FROM travel_time").fetchone()[0]
        return {"cached_pairs": times, "memory_entries": len(self._mem)}
