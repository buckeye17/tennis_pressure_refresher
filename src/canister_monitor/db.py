"""SQLite storage: connection setup, migrations and queries (PLAN.md section 7).

The collector and web processes each open their own connection. WAL mode lets
the web process read while the collector writes. Every write function commits
its own transaction.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from canister_monitor.decoders.base import Advert, Reading

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE sensors (
        mac          TEXT PRIMARY KEY,
        canister     TEXT,
        decoder      TEXT,
        first_seen   REAL NOT NULL,
        last_seen    REAL NOT NULL,
        last_rssi    INTEGER
    );

    CREATE TABLE raw_adverts (
        id           INTEGER PRIMARY KEY,
        ts           REAL NOT NULL,
        mac          TEXT NOT NULL,
        name         TEXT,
        rssi         INTEGER,
        mfr_json     TEXT,
        service_uuids_json TEXT,
        service_data_json  TEXT
    );
    CREATE INDEX idx_raw_mac_ts ON raw_adverts(mac, ts);

    CREATE TABLE readings (
        id               INTEGER PRIMARY KEY,
        ts               REAL NOT NULL,
        mac              TEXT NOT NULL REFERENCES sensors(mac),
        pressure_kpa_abs REAL NOT NULL,
        temp_c           REAL,
        battery_pct      REAL,
        battery_v        REAL,
        flags_json       TEXT,
        decoder          TEXT NOT NULL,
        raw_advert_id    INTEGER REFERENCES raw_adverts(id)
    );
    CREATE INDEX idx_readings_mac_ts ON readings(mac, ts);

    CREATE TABLE events (
        id        INTEGER PRIMARY KEY,
        ts        REAL NOT NULL,
        mac       TEXT,
        kind      TEXT NOT NULL,
        note      TEXT
    );
    CREATE INDEX idx_events_ts ON events(ts);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


# --- connection and migrations ----------------------------------------------


def connect(path: str | Path, migrate_schema: bool = True) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and a busy timeout.

    Creates the parent directory and applies pending migrations by default.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # FULL: a committed reading survives power loss, which matters on a Pi.
    conn.execute("PRAGMA synchronous=FULL")
    if migrate_schema:
        migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations newer than the stored version. Safe to call repeatedly."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this code (v{SCHEMA_VERSION})"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        # executescript commits first, so wrap each migration in its own transaction.
        try:
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version - 1]}\n"
                f"INSERT INTO schema_version (version) VALUES ({version});\nCOMMIT;"
            )
        except sqlite3.Error:
            conn.rollback()
            raise
    return SCHEMA_VERSION


# --- writes -----------------------------------------------------------------


@dataclass(frozen=True)
class SensorSeen:
    mac: str
    ts: float
    rssi: int | None
    decoder: str | None = None
    canister: str | None = None


_UPSERT_SENSOR = """
    INSERT INTO sensors (mac, canister, decoder, first_seen, last_seen, last_rssi)
    VALUES (:mac, :canister, :decoder, :ts, :ts, :rssi)
    ON CONFLICT(mac) DO UPDATE SET
        canister   = COALESCE(excluded.canister, canister),
        decoder    = COALESCE(excluded.decoder, decoder),
        first_seen = MIN(first_seen, excluded.first_seen),
        last_rssi  = CASE WHEN excluded.last_seen >= last_seen
                          THEN excluded.last_rssi ELSE last_rssi END,
        last_seen  = MAX(last_seen, excluded.last_seen)
"""


def upsert_sensors_seen(conn: sqlite3.Connection, seen: Iterable[SensorSeen]) -> None:
    """Create or update sensor rows in one transaction (the collector batches these)."""
    with conn:
        conn.executemany(_UPSERT_SENSOR, [s.__dict__ for s in seen])


def upsert_sensor_seen(
    conn: sqlite3.Connection,
    mac: str,
    ts: float,
    rssi: int | None,
    decoder: str | None = None,
    canister: str | None = None,
) -> None:
    upsert_sensors_seen(conn, [SensorSeen(mac, ts, rssi, decoder, canister)])


def _hex_json(data: dict) -> str:
    """{key: bytes} as JSON {"<key>": "<HEX>"}; int company IDs become string keys."""
    return json.dumps({str(k): bytes(v).hex().upper() for k, v in data.items()})


def insert_raw_advert(conn: sqlite3.Connection, adv: Advert) -> int:
    """Store an advertisement's raw bytes; returns the new row id."""
    with conn:
        cur = conn.execute(
            """INSERT INTO raw_adverts
               (ts, mac, name, rssi, mfr_json, service_uuids_json, service_data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                adv.ts,
                adv.mac,
                adv.name,
                adv.rssi,
                _hex_json(adv.manufacturer_data),
                json.dumps(list(adv.service_uuids)),
                _hex_json(adv.service_data),
            ),
        )
        return cur.lastrowid


def insert_reading(conn: sqlite3.Connection, reading: Reading, raw_advert_id: int | None) -> int:
    """Store a decoded reading, creating its sensor row if needed; returns the row id."""
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO sensors (mac, decoder, first_seen, last_seen)
               VALUES (?, ?, ?, ?)""",
            (reading.mac, reading.decoder, reading.ts, reading.ts),
        )
        cur = conn.execute(
            """INSERT INTO readings
               (ts, mac, pressure_kpa_abs, temp_c, battery_pct, battery_v, flags_json,
                decoder, raw_advert_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reading.ts,
                reading.mac,
                reading.pressure_kpa_abs,
                reading.temp_c,
                reading.battery_pct,
                reading.battery_v,
                json.dumps(reading.flags, sort_keys=True),
                reading.decoder,
                raw_advert_id,
            ),
        )
        return cur.lastrowid


# --- reads ------------------------------------------------------------------


@dataclass(frozen=True)
class SensorStatus:
    """A sensor row plus its most recent reading (reading fields None if it has none)."""

    mac: str
    canister: str | None
    decoder: str | None
    first_seen: float
    last_seen: float
    last_rssi: int | None
    reading_ts: float | None
    pressure_kpa_abs: float | None
    temp_c: float | None
    battery_pct: float | None
    battery_v: float | None
    flags: dict


def latest_readings(conn: sqlite3.Connection) -> list[SensorStatus]:
    """One entry per known sensor, with its newest reading, ordered by MAC."""
    rows = conn.execute(
        """SELECT s.mac, s.canister, s.decoder, s.first_seen, s.last_seen, s.last_rssi,
                  r.ts AS reading_ts, r.pressure_kpa_abs, r.temp_c, r.battery_pct,
                  r.battery_v, r.flags_json
           FROM sensors s
           LEFT JOIN readings r ON r.id = (
               SELECT id FROM readings WHERE mac = s.mac ORDER BY ts DESC, id DESC LIMIT 1)
           ORDER BY s.mac"""
    ).fetchall()
    return [
        SensorStatus(
            **{k: row[k] for k in row.keys() if k != "flags_json"},
            flags=json.loads(row["flags_json"]) if row["flags_json"] else {},
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SeriesPoint:
    """One chart point: a single reading, or the average of a time bucket."""

    mac: str
    ts: float
    pressure_kpa_abs: float
    temp_c: float | None
    count: int


def readings_between(
    conn: sqlite3.Connection,
    mac: str | None = None,
    start: float | None = None,
    end: float | None = None,
    max_points: int = 2000,
) -> list[SeriesPoint]:
    """Time series for one sensor (or all), ordered by MAC then time.

    If a sensor has more than ``max_points`` readings in the range, its readings
    are averaged into ``max_points`` equal-width time buckets.
    """
    if max_points < 1:
        raise ValueError("max_points must be at least 1")
    where, params = ["1=1"], {}
    if mac is not None:
        where.append("mac = :mac")
        params["mac"] = mac
    if start is not None:
        where.append("ts >= :start")
        params["start"] = start
    if end is not None:
        where.append("ts <= :end")
        params["end"] = end
    cond = " AND ".join(where)

    points: list[SeriesPoint] = []
    stats = conn.execute(
        f"SELECT mac, COUNT(*) AS n, MIN(ts) AS t0, MAX(ts) AS t1 FROM readings "
        f"WHERE {cond} GROUP BY mac ORDER BY mac",
        params,
    ).fetchall()
    for s in stats:
        p = {**params, "mac": s["mac"]}
        sensor_cond = cond if mac is not None else f"{cond} AND mac = :mac"
        if s["n"] <= max_points:
            rows = conn.execute(
                f"SELECT mac, ts, pressure_kpa_abs, temp_c, 1 AS count FROM readings "
                f"WHERE {sensor_cond} ORDER BY ts, id",
                p,
            ).fetchall()
        else:
            t0 = start if start is not None else s["t0"]
            t1 = end if end is not None else s["t1"]
            p.update(t0=t0, width=max((t1 - t0) / max_points, 1e-9), last=max_points - 1)
            rows = conn.execute(
                f"""SELECT mac, AVG(ts) AS ts, AVG(pressure_kpa_abs) AS pressure_kpa_abs,
                           AVG(temp_c) AS temp_c, COUNT(*) AS count
                    FROM readings WHERE {sensor_cond}
                    GROUP BY MIN(CAST((ts - :t0) / :width AS INTEGER), :last)
                    ORDER BY ts""",
                p,
            ).fetchall()
        points.extend(SeriesPoint(**dict(r)) for r in rows)
    return points


def newest_reading_ts(conn: sqlite3.Connection) -> float | None:
    return conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]


# --- events (annotations) ----------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    mac: str | None
    kind: str
    note: str | None


def insert_event(
    conn: sqlite3.Connection, ts: float, kind: str, note: str | None = None, mac: str | None = None
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO events (ts, mac, kind, note) VALUES (?, ?, ?, ?)", (ts, mac, kind, note)
        )
        return cur.lastrowid


def list_events(
    conn: sqlite3.Connection, start: float | None = None, end: float | None = None
) -> list[Event]:
    rows = conn.execute(
        """SELECT id, ts, mac, kind, note FROM events
           WHERE (:start IS NULL OR ts >= :start) AND (:end IS NULL OR ts <= :end)
           ORDER BY ts, id""",
        {"start": start, "end": end},
    ).fetchall()
    return [Event(**dict(r)) for r in rows]


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Delete an annotation; returns False if it didn't exist."""
    with conn:
        return conn.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0
