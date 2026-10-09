"""JSON API and CSV export. All pressures in kPa, temperatures in degC, times in UTC
epoch seconds; unit conversion happens in the browser."""

import csv
import io
import math
import re
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, request

from canister_monitor import db
from canister_monitor.compensation import (
    compensated_abs_kpa,
    compensated_gauge_kpa,
    gauge_kpa,
    kpa_to_psi,
)
from canister_monitor.config import MAC_RE
from canister_monitor.web import get_db

api = Blueprint("api", __name__)

MAX_POINTS_LIMIT = 20_000
EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NOTE_MAX = 1000


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


@api.errorhandler(ApiError)
def _api_error(e: ApiError):
    return jsonify({"error": e.message}), e.status


def _ctx() -> dict:
    return current_app.extensions["canister"]


# --- parameter parsing -----------------------------------------------------------


def _float_arg(name: str) -> float | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ApiError(f"{name} must be a number (UTC epoch seconds), got {raw!r}") from None
    if not math.isfinite(value):
        raise ApiError(f"{name} must be finite")
    return value


def _range_args() -> tuple[float | None, float | None]:
    start, end = _float_arg("from"), _float_arg("to")
    if start is not None and end is not None and start > end:
        raise ApiError("from must not be after to")
    return start, end


def _mac(value: object, name: str = "mac") -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not MAC_RE.match(value.upper()):
        raise ApiError(f"{name} must look like AA:BB:CC:DD:EE:FF, got {value!r}")
    return value.upper()


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ApiError(f"{name} must be between {lo} and {hi}")
    return value


# --- helpers ---------------------------------------------------------------------


def _canister_name(mac: str, stored: str | None) -> str | None:
    """Config is the source of truth; fall back to the name stored at collection time."""
    return _ctx()["config"].canister_for(mac) or stored


def _pressures(abs_kpa: float | None, temp_c: float | None) -> dict:
    site = _ctx()["config"].site
    if abs_kpa is None:
        return {
            "pressure_kpa_abs": None,
            "gauge_kpa": None,
            "compensated_kpa_abs": None,
            "compensated_gauge_kpa": None,
        }
    return {
        "pressure_kpa_abs": abs_kpa,
        "gauge_kpa": gauge_kpa(abs_kpa, site.atmospheric_kpa),
        "compensated_kpa_abs": compensated_abs_kpa(abs_kpa, temp_c, site.reference_temp_c),
        "compensated_gauge_kpa": compensated_gauge_kpa(
            abs_kpa, temp_c, site.atmospheric_kpa, site.reference_temp_c
        ),
    }


# --- endpoints -------------------------------------------------------------------


@api.get("/api/canisters")
def canisters():
    config, now = _ctx()["config"], _ctx()["clock"]()
    stale_after_s = config.web.stale_after_minutes * 60
    known = {s.mac: s for s in db.latest_readings(get_db())}
    configured = [s.mac for s in config.sensors]
    order = configured + sorted(m for m in known if m not in configured)

    entries = []
    for mac in order:
        s = known.get(mac)
        name = _canister_name(mac, s.canister if s else None)
        reading_ts = s.reading_ts if s else None
        heard = reading_ts if reading_ts is not None else (s.last_seen if s else None)
        entries.append(
            {
                "mac": mac,
                "canister": name,
                "assigned": name is not None,
                "decoder": s.decoder if s else None,
                "first_seen": s.first_seen if s else None,
                "last_seen": s.last_seen if s else None,
                "rssi": s.last_rssi if s else None,
                "stale": heard is None or now - heard > stale_after_s,
                "reading": None
                if reading_ts is None
                else {
                    "ts": reading_ts,
                    "age_s": now - reading_ts,
                    **_pressures(s.pressure_kpa_abs, s.temp_c),
                    "temp_c": s.temp_c,
                    "battery_pct": s.battery_pct,
                    "battery_v": s.battery_v,
                    "flags": s.flags,
                },
                "trend": None,  # Phase 7
            }
        )
    return jsonify(
        {
            "now": now,
            "settings": {
                "atmospheric_kpa": config.site.atmospheric_kpa,
                "reference_temp_c": config.site.reference_temp_c,
                "stale_after_s": stale_after_s,
                "default_units": config.web.default_units,
            },
            "canisters": entries,
        }
    )


@api.get("/api/readings")
def readings():
    """Columnar series per sensor (uPlot-friendly). Buckets are averaged in absolute
    pressure and temperature, then compensated."""
    mac = _mac(request.args.get("mac"))
    start, end = _range_args()
    max_points = _int_arg("max_points", 2000, 1, MAX_POINTS_LIMIT)
    points = db.readings_between(get_db(), mac=mac, start=start, end=end, max_points=max_points)
    stored_names = {s.mac: s.canister for s in db.latest_readings(get_db())}

    series: dict[str, dict] = {}
    for p in points:
        s = series.get(p.mac)
        if s is None:
            s = series[p.mac] = {
                "mac": p.mac,
                "canister": _canister_name(p.mac, stored_names.get(p.mac)),
                "ts": [],
                "gauge_kpa": [],
                "compensated_gauge_kpa": [],
                "temp_c": [],
                "count": [],
            }
        values = _pressures(p.pressure_kpa_abs, p.temp_c)
        s["ts"].append(p.ts)
        s["gauge_kpa"].append(values["gauge_kpa"])
        s["compensated_gauge_kpa"].append(values["compensated_gauge_kpa"])
        s["temp_c"].append(p.temp_c)
        s["count"].append(p.count)
    return jsonify({"from": start, "to": end, "series": list(series.values())})


@api.get("/api/events")
def events():
    start, end = _range_args()
    return jsonify({"events": [e.__dict__ for e in db.list_events(get_db(), start, end)]})


@api.post("/api/events")
def create_event():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("request body must be a JSON object")
    unknown = set(body) - {"ts", "mac", "kind", "note"}
    if unknown:
        raise ApiError(f"unknown field(s): {', '.join(sorted(unknown))}")

    ts = body.get("ts")
    if ts is None:
        ts = _ctx()["clock"]()
    elif isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        raise ApiError("ts must be a number (UTC epoch seconds)")
    kind = body.get("kind")
    if not isinstance(kind, str) or not EVENT_KIND_RE.match(kind):
        raise ApiError("kind must be a short lowercase word, e.g. note, fill, vent, balls_added")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise ApiError(f"note must be text of at most {NOTE_MAX} characters")
    mac = _mac(body.get("mac"))

    event_id = db.insert_event(get_db(), float(ts), kind, note or None, mac)
    return jsonify(db.Event(event_id, float(ts), mac, kind, note or None).__dict__), 201


@api.delete("/api/events/<int:event_id>")
def delete_event(event_id: int):
    if not db.delete_event(get_db(), event_id):
        raise ApiError(f"event {event_id} not found", 404)
    return "", 204


CSV_COLUMNS = [
    "local_time",
    "utc_epoch",
    "canister",
    "mac",
    "gauge_psi",
    "abs_psi",
    "compensated_gauge_psi",
    "temp_c",
    "battery_pct",
    "battery_v",
]


def _fmt(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


@api.get("/api/export.csv")
def export_csv():
    mac = _mac(request.args.get("mac"))
    start, end = _range_args()
    rows = db.all_readings(get_db(), mac=mac, start=start, end=end)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        p = _pressures(r.pressure_kpa_abs, r.temp_c)
        comp = p["compensated_gauge_kpa"]
        writer.writerow(
            [
                datetime.fromtimestamp(r.ts).astimezone().isoformat(timespec="seconds"),
                f"{r.ts:.3f}",
                _canister_name(r.mac, r.canister) or "",
                r.mac,
                _fmt(kpa_to_psi(p["gauge_kpa"]), 2),
                _fmt(kpa_to_psi(r.pressure_kpa_abs), 2),
                _fmt(None if comp is None else kpa_to_psi(comp), 2),
                _fmt(r.temp_c, 1),
                _fmt(r.battery_pct, 0),
                _fmt(r.battery_v, 2),
            ]
        )
    stamp = datetime.fromtimestamp(_ctx()["clock"]()).strftime("%Y%m%d-%H%M")
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="canisters-{stamp}.csv"'},
    )


@api.get("/healthz")
def healthz():
    now = _ctx()["clock"]()
    try:
        newest = db.newest_reading_ts(get_db())
    except Exception as e:  # report, don't crash, so monitoring sees the failure
        return jsonify({"ok": False, "db": str(_ctx()["db_path"]), "error": str(e)}), 503
    return jsonify(
        {
            "ok": True,
            "db": str(_ctx()["db_path"]),
            "newest_reading_age_s": None if newest is None else now - newest,
        }
    )
