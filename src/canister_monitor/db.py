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
    # 2: newest-first paging across all sensors (table view)
    """
    CREATE INDEX idx_readings_ts ON readings(ts);
    """,
    # 3: ball batches and bounce tests (PLAN.md 6.2)
    """
    CREATE TABLE bounce_setups (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        drop_cm    REAL NOT NULL,
        is_itf     INTEGER NOT NULL DEFAULT 0,
        note       TEXT
    );

    CREATE TABLE ball_batches (
        id          INTEGER PRIMARY KEY,
        brand       TEXT NOT NULL COLLATE NOCASE,
        model       TEXT NOT NULL COLLATE NOCASE,
        opened_ts   REAL NOT NULL,
        n_balls     INTEGER NOT NULL,
        sample_size INTEGER NOT NULL,
        reference_override_cm REAL,
        retired_ts  REAL,
        note        TEXT
    );

    CREATE TABLE bounce_tests (
        id        INTEGER PRIMARY KEY,
        ts        REAL NOT NULL,
        batch_id  INTEGER NOT NULL REFERENCES ball_batches(id) ON DELETE CASCADE,
        setup_id  INTEGER NOT NULL REFERENCES bounce_setups(id),
        context   TEXT NOT NULL,
        temp_c    REAL,
        note      TEXT
    );
    CREATE INDEX idx_bounce_tests_batch ON bounce_tests(batch_id, ts);

    CREATE TABLE bounce_drops (
        id         INTEGER PRIMARY KEY,
        test_id    INTEGER NOT NULL REFERENCES bounce_tests(id) ON DELETE CASCADE,
        ball_no    INTEGER NOT NULL,
        rebound_cm REAL NOT NULL
    );
    CREATE INDEX idx_bounce_drops_test ON bounce_drops(test_id);

    CREATE TABLE reference_rebounds (
        id         INTEGER PRIMARY KEY,
        brand      TEXT NOT NULL COLLATE NOCASE,
        model      TEXT NOT NULL COLLATE NOCASE,
        setup_id   INTEGER NOT NULL REFERENCES bounce_setups(id),
        rebound_cm REAL NOT NULL,
        UNIQUE (brand, model, setup_id)
    );

    CREATE TABLE setup_conversions (
        id             INTEGER PRIMARY KEY,
        ts             REAL NOT NULL,
        setup_id       INTEGER NOT NULL REFERENCES bounce_setups(id),
        rebound_cm     REAL NOT NULL,
        itf_rebound_cm REAL NOT NULL,
        note           TEXT
    );

    ALTER TABLE events ADD COLUMN batch_id INTEGER REFERENCES ball_batches(id) ON DELETE SET NULL;
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


def _range_condition(
    mac: str | None, start: float | None, end: float | None, table: str = ""
) -> tuple[str, dict]:
    """SQL WHERE clause and parameters for optional MAC and time-range filters."""
    where, params = ["1=1"], {}
    if mac is not None:
        where.append(f"{table}mac = :mac")
        params["mac"] = mac
    if start is not None:
        where.append(f"{table}ts >= :start")
        params["start"] = start
    if end is not None:
        where.append(f"{table}ts <= :end")
        params["end"] = end
    return " AND ".join(where), params


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
    cond, params = _range_condition(mac, start, end)

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


@dataclass(frozen=True)
class StoredReading:
    id: int
    ts: float
    mac: str
    canister: str | None
    pressure_kpa_abs: float
    temp_c: float | None
    battery_pct: float | None
    battery_v: float | None


_STORED_COLUMNS = """r.id, r.ts, r.mac, s.canister, r.pressure_kpa_abs, r.temp_c,
                   r.battery_pct, r.battery_v"""


def all_readings(
    conn: sqlite3.Connection,
    mac: str | None = None,
    start: float | None = None,
    end: float | None = None,
) -> list[StoredReading]:
    """Every reading in the range (no downsampling), ordered by time; used for export."""
    cond, params = _range_condition(mac, start, end, table="r.")
    rows = conn.execute(
        f"""SELECT {_STORED_COLUMNS}
            FROM readings r LEFT JOIN sensors s ON s.mac = r.mac
            WHERE {cond} ORDER BY r.ts, r.id""",
        params,
    ).fetchall()
    return [StoredReading(**dict(r)) for r in rows]


def recent_readings(
    conn: sqlite3.Connection,
    mac: str | None = None,
    start: float | None = None,
    end: float | None = None,
    limit: int = 100,
    before: tuple[float, int] | None = None,
) -> list[StoredReading]:
    """One page of readings, newest first (no downsampling); used for the table view.

    ``before`` is the ``(ts, id)`` of the last row of the previous page. Paging by
    that key, not by offset, keeps pages stable while new readings arrive.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    cond, params = _range_condition(mac, start, end, table="r.")
    if before is not None:
        cond += " AND (r.ts < :before_ts OR (r.ts = :before_ts AND r.id < :before_id))"
        params.update(before_ts=before[0], before_id=before[1])
    rows = conn.execute(
        f"""SELECT {_STORED_COLUMNS}
            FROM readings r LEFT JOIN sensors s ON s.mac = r.mac
            WHERE {cond} ORDER BY r.ts DESC, r.id DESC LIMIT :limit""",
        {**params, "limit": limit},
    ).fetchall()
    return [StoredReading(**dict(r)) for r in rows]


# --- events (annotations) ----------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    mac: str | None
    kind: str
    note: str | None
    batch_id: int | None = None  # balls_added / balls_removed: which ball batch


def insert_event(
    conn: sqlite3.Connection,
    ts: float,
    kind: str,
    note: str | None = None,
    mac: str | None = None,
    batch_id: int | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO events (ts, mac, kind, note, batch_id) VALUES (?, ?, ?, ?, ?)",
            (ts, mac, kind, note, batch_id),
        )
        return cur.lastrowid


def list_events(
    conn: sqlite3.Connection, start: float | None = None, end: float | None = None
) -> list[Event]:
    rows = conn.execute(
        """SELECT id, ts, mac, kind, note, batch_id FROM events
           WHERE (:start IS NULL OR ts >= :start) AND (:end IS NULL OR ts <= :end)
           ORDER BY ts, id""",
        {"start": start, "end": end},
    ).fetchall()
    return [Event(**dict(r)) for r in rows]


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Delete an annotation; returns False if it didn't exist."""
    with conn:
        return conn.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0


FILL_KINDS = ("fill", "leak_test")  # a leak test starts with a fill too
# A leak test ends at the next of these (for its canister or all), or now.
LEAK_TEST_END_KINDS = ("fill", "leak_test", "vent", "balls_added")


def latest_fill_ts(conn: sqlite3.Connection, mac: str, now: float) -> float | None:
    """Time of the most recent fill (or leak test) for this sensor (or for all canisters)."""
    return conn.execute(
        f"""SELECT MAX(ts) FROM events
            WHERE kind IN ({",".join("?" * len(FILL_KINDS))})
              AND (mac = ? OR mac IS NULL) AND ts <= ?""",
        (*FILL_KINDS, mac, now),
    ).fetchone()[0]


def leak_test_spans(conn: sqlite3.Connection, mac: str, now: float) -> list[tuple[float, float]]:
    """(start, end) of each leak test of this sensor's canister, oldest first.

    A test runs from its "leak_test" event to the next fill, leak test, vent or
    balls_added event for the same canister or all canisters, or to ``now``.
    """
    rows = conn.execute(
        """SELECT ts, kind FROM events
           WHERE (mac = ? OR mac IS NULL) AND ts <= ? ORDER BY ts, id""",
        (mac, now),
    ).fetchall()
    spans = []
    for i, (ts, kind) in enumerate(rows):
        if kind != "leak_test":
            continue
        end = next((t for t, k in rows[i + 1 :] if k in LEAK_TEST_END_KINDS and t > ts), now)
        spans.append((ts, end))
    return spans


# --- balls and bounce tests (Phase 8, PLAN.md 6.2) ----------------------------------
#
# Lengths are in cm. Validation happens in the API; these functions only store.


@dataclass(frozen=True)
class BounceSetup:
    id: int
    name: str
    drop_cm: float
    is_itf: bool
    note: str | None
    used: bool  # referenced by a test, reference value or conversion: can't be deleted


def insert_setup(
    conn: sqlite3.Connection, name: str, drop_cm: float, is_itf: bool, note: str | None = None
) -> int:
    """Raises sqlite3.IntegrityError if the name is taken."""
    with conn:
        return conn.execute(
            "INSERT INTO bounce_setups (name, drop_cm, is_itf, note) VALUES (?, ?, ?, ?)",
            (name, drop_cm, int(is_itf), note),
        ).lastrowid


def list_setups(conn: sqlite3.Connection) -> list[BounceSetup]:
    rows = conn.execute(
        """SELECT s.id, s.name, s.drop_cm, s.is_itf, s.note,
                  EXISTS (SELECT 1 FROM bounce_tests WHERE setup_id = s.id)
                  OR EXISTS (SELECT 1 FROM reference_rebounds WHERE setup_id = s.id)
                  OR EXISTS (SELECT 1 FROM setup_conversions WHERE setup_id = s.id) AS used
           FROM bounce_setups s ORDER BY s.id"""
    ).fetchall()
    return [
        BounceSetup(r["id"], r["name"], r["drop_cm"], bool(r["is_itf"]), r["note"], bool(r["used"]))
        for r in rows
    ]


def delete_setup(conn: sqlite3.Connection, setup_id: int) -> bool:
    """Delete an unused setup. Raises sqlite3.IntegrityError if anything uses it."""
    with conn:
        return conn.execute("DELETE FROM bounce_setups WHERE id = ?", (setup_id,)).rowcount > 0


@dataclass(frozen=True)
class Batch:
    id: int
    brand: str
    model: str
    opened_ts: float
    n_balls: int
    sample_size: int  # balls numbered 1..sample_size get tested
    reference_override_cm: float | None
    retired_ts: float | None
    note: str | None


BATCH_FIELDS = (
    "brand",
    "model",
    "opened_ts",
    "n_balls",
    "sample_size",
    "reference_override_cm",
    "retired_ts",
    "note",
)


def insert_batch(conn: sqlite3.Connection, **fields) -> int:
    cols = [f for f in BATCH_FIELDS if f in fields]
    with conn:
        return conn.execute(
            f"INSERT INTO ball_batches ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)})",
            fields,
        ).lastrowid


def update_batch(conn: sqlite3.Connection, batch_id: int, **fields) -> bool:
    cols = [f for f in BATCH_FIELDS if f in fields]
    if not cols:
        return get_batch(conn, batch_id) is not None
    with conn:
        return (
            conn.execute(
                f"UPDATE ball_batches SET {', '.join(f'{c} = :{c}' for c in cols)} WHERE id = :id",
                {**fields, "id": batch_id},
            ).rowcount
            > 0
        )


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Batch | None:
    row = conn.execute(
        f"SELECT id, {', '.join(BATCH_FIELDS)} FROM ball_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    return None if row is None else Batch(**dict(row))


def list_batches(conn: sqlite3.Connection) -> list[Batch]:
    rows = conn.execute(
        f"SELECT id, {', '.join(BATCH_FIELDS)} FROM ball_batches ORDER BY opened_ts DESC, id DESC"
    ).fetchall()
    return [Batch(**dict(r)) for r in rows]


def delete_batch(conn: sqlite3.Connection, batch_id: int) -> bool:
    """Delete a batch and its tests; events that named it keep their note."""
    with conn:
        return conn.execute("DELETE FROM ball_batches WHERE id = ?", (batch_id,)).rowcount > 0


def highest_ball_tested(conn: sqlite3.Connection, batch_id: int) -> int | None:
    return conn.execute(
        """SELECT MAX(d.ball_no) FROM bounce_drops d
           JOIN bounce_tests t ON t.id = d.test_id WHERE t.batch_id = ?""",
        (batch_id,),
    ).fetchone()[0]


def batch_locations(conn: sqlite3.Connection) -> dict[int, tuple[str, str | None]]:
    """Latest (kind, mac) of each batch's balls_added / balls_removed event."""
    rows = conn.execute(
        """SELECT batch_id, kind, mac FROM events
           WHERE batch_id IS NOT NULL AND kind IN ('balls_added', 'balls_removed')
           ORDER BY ts, id"""
    ).fetchall()
    return {r["batch_id"]: (r["kind"], r["mac"]) for r in rows}


@dataclass(frozen=True)
class BounceDrop:
    ball_no: int
    rebound_cm: float


@dataclass(frozen=True)
class BounceTest:
    id: int
    ts: float
    batch_id: int
    setup_id: int
    context: str  # new | before | after | after_play | other
    temp_c: float | None
    note: str | None
    drops: tuple[BounceDrop, ...]


def _insert_drops(conn: sqlite3.Connection, test_id: int, drops: Iterable[BounceDrop]) -> None:
    conn.executemany(
        "INSERT INTO bounce_drops (test_id, ball_no, rebound_cm) VALUES (?, ?, ?)",
        [(test_id, d.ball_no, d.rebound_cm) for d in drops],
    )


def insert_test(
    conn: sqlite3.Connection,
    ts: float,
    batch_id: int,
    setup_id: int,
    context: str,
    temp_c: float | None,
    note: str | None,
    drops: Iterable[BounceDrop],
) -> int:
    """Store a bounce test and its drops in one transaction."""
    with conn:
        test_id = conn.execute(
            """INSERT INTO bounce_tests (ts, batch_id, setup_id, context, temp_c, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, batch_id, setup_id, context, temp_c, note),
        ).lastrowid
        _insert_drops(conn, test_id, drops)
        return test_id


def replace_test(
    conn: sqlite3.Connection,
    test_id: int,
    ts: float,
    batch_id: int,
    setup_id: int,
    context: str,
    temp_c: float | None,
    note: str | None,
    drops: Iterable[BounceDrop],
) -> bool:
    """Overwrite a test and all its drops; False if it doesn't exist."""
    with conn:
        updated = conn.execute(
            """UPDATE bounce_tests SET ts = ?, batch_id = ?, setup_id = ?, context = ?,
                   temp_c = ?, note = ?
               WHERE id = ?""",
            (ts, batch_id, setup_id, context, temp_c, note, test_id),
        ).rowcount
        if not updated:
            return False
        conn.execute("DELETE FROM bounce_drops WHERE test_id = ?", (test_id,))
        _insert_drops(conn, test_id, drops)
        return True


def list_tests(conn: sqlite3.Connection, batch_id: int | None = None) -> list[BounceTest]:
    """Tests oldest first, each with its drops in ball order."""
    tests = conn.execute(
        """SELECT id, ts, batch_id, setup_id, context, temp_c, note FROM bounce_tests
           WHERE (:batch IS NULL OR batch_id = :batch) ORDER BY ts, id""",
        {"batch": batch_id},
    ).fetchall()
    drops: dict[int, list[BounceDrop]] = {}
    for r in conn.execute(
        """SELECT d.test_id, d.ball_no, d.rebound_cm FROM bounce_drops d
           JOIN bounce_tests t ON t.id = d.test_id
           WHERE (:batch IS NULL OR t.batch_id = :batch) ORDER BY d.ball_no, d.id""",
        {"batch": batch_id},
    ):
        drops.setdefault(r["test_id"], []).append(BounceDrop(r["ball_no"], r["rebound_cm"]))
    return [BounceTest(**dict(t), drops=tuple(drops.get(t["id"], ()))) for t in tests]


def delete_test(conn: sqlite3.Connection, test_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM bounce_tests WHERE id = ?", (test_id,)).rowcount > 0


@dataclass(frozen=True)
class Reference:
    """A user-entered new-ball rebound for a brand and model at one setup."""

    id: int
    brand: str
    model: str
    setup_id: int
    rebound_cm: float


def upsert_reference(
    conn: sqlite3.Connection, brand: str, model: str, setup_id: int, rebound_cm: float
) -> int:
    """Set the value for (brand, model, setup), replacing any earlier one; returns its id."""
    with conn:
        conn.execute(
            """INSERT INTO reference_rebounds (brand, model, setup_id, rebound_cm)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (brand, model, setup_id)
               DO UPDATE SET rebound_cm = excluded.rebound_cm""",
            (brand, model, setup_id, rebound_cm),
        )
        return conn.execute(
            "SELECT id FROM reference_rebounds WHERE brand = ? AND model = ? AND setup_id = ?",
            (brand, model, setup_id),
        ).fetchone()[0]


def list_references(conn: sqlite3.Connection) -> list[Reference]:
    rows = conn.execute(
        "SELECT id, brand, model, setup_id, rebound_cm FROM reference_rebounds "
        "ORDER BY brand, model, setup_id"
    ).fetchall()
    return [Reference(**dict(r)) for r in rows]


def delete_reference(conn: sqlite3.Connection, reference_id: int) -> bool:
    with conn:
        return (
            conn.execute("DELETE FROM reference_rebounds WHERE id = ?", (reference_id,)).rowcount
            > 0
        )


@dataclass(frozen=True)
class Conversion:
    """One ball's rebound at a user setup and at ITF conditions, measured the same day."""

    id: int
    ts: float
    setup_id: int
    rebound_cm: float
    itf_rebound_cm: float
    note: str | None


def insert_conversion(
    conn: sqlite3.Connection,
    ts: float,
    setup_id: int,
    rebound_cm: float,
    itf_rebound_cm: float,
    note: str | None = None,
) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO setup_conversions (ts, setup_id, rebound_cm, itf_rebound_cm, note)
               VALUES (?, ?, ?, ?, ?)""",
            (ts, setup_id, rebound_cm, itf_rebound_cm, note),
        ).lastrowid


def list_conversions(conn: sqlite3.Connection) -> list[Conversion]:
    rows = conn.execute(
        "SELECT id, ts, setup_id, rebound_cm, itf_rebound_cm, note FROM setup_conversions "
        "ORDER BY setup_id, ts, id"
    ).fetchall()
    return [Conversion(**dict(r)) for r in rows]


def delete_conversion(conn: sqlite3.Connection, conversion_id: int) -> bool:
    with conn:
        return (
            conn.execute("DELETE FROM setup_conversions WHERE id = ?", (conversion_id,)).rowcount
            > 0
        )
