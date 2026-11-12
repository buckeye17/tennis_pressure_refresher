"""JSON API for charge sessions and play (PLAN.md section 6.4): a batch's time in a
canister, the bounce estimated from the pressure it sat at, and how long to keep
the canister pumped up. Pressures are gauge kPa, times UTC epoch seconds, and
rebounds are shares of the batch's new-ball rebound (1.0 is like new)."""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime

from flask import Blueprint, jsonify

from canister_monitor import charge, db
from canister_monitor.bounce import NewBallReference, new_ball_reference, summarize
from canister_monitor.compensation import KPA_PER_PSI, compensated_gauge_kpa, gauge_kpa, psi_to_kpa
from canister_monitor.web import get_db
from canister_monitor.web.api import ApiError, _ctx, _mac
from canister_monitor.web.bounce_api import NOTE_MAX, _batch, _body, _number, _text, _ts

charge_api = Blueprint("charge_api", __name__)

SERIES_POINTS = 600  # per session: readings are averaged into this many steps at most
CACHE_SETTLE_S = 600.0  # a session that ended this long ago won't get more readings
CACHE_MAX = 256
FUTURE_SLACK_S = 300.0  # a phone's clock may run a little ahead of the Pi's
MAX_PLAY_HOURS = 24.0


@charge_api.errorhandler(ApiError)
def _api_error(e: ApiError):
    return jsonify({"error": e.message}), e.status


# --- gathering ---------------------------------------------------------------------------


def _series(mac: str | None, start: float, end: float | None, now: float) -> list[tuple]:
    """A canister's temperature-compensated gauge pressure over a session, as
    (ts, kPa), raw gauge where a reading has no temperature. A finished session's
    readings can't change, so they're kept between requests."""
    if mac is None:
        return []
    cache = _ctx().setdefault("charge_series", {})
    key = (mac, start, end)
    settled = end is not None and now - end > CACHE_SETTLE_S
    if settled and key in cache:
        return cache[key]
    site = _ctx()["config"].site
    points = db.readings_between(
        get_db(), mac=mac, start=start, end=now if end is None else end, max_points=SERIES_POINTS
    )
    series = []
    for p in points:
        comp = compensated_gauge_kpa(
            p.pressure_kpa_abs, p.temp_c, site.atmospheric_kpa, site.reference_temp_c
        )
        if comp is None:
            comp = gauge_kpa(p.pressure_kpa_abs, site.atmospheric_kpa)
        series.append((p.ts, comp))
    if settled:
        if len(cache) >= CACHE_MAX:
            cache.clear()
        cache[key] = series
    return series


@dataclass
class _Test:
    id: int
    ts: float
    setup_id: int
    context: str
    mean_cm: float


@dataclass
class _Work:
    """One batch, gathered: its tests at the setup it's tested with, what it
    rebounded when new, and the pressure and play it has been through."""

    batch: db.Batch
    setup_id: int | None = None
    tests: list[_Test] = field(default_factory=list)
    reference: NewBallReference | None = None
    sessions: list[tuple[db.ChargeSession, list[tuple]]] = field(default_factory=list)
    plays: list[db.Play] = field(default_factory=list)
    track: charge.Track = field(default_factory=charge.Track)

    @property
    def model_key(self) -> tuple:
        return (self.batch.brand.casefold(), self.batch.model.casefold(), self.setup_id)

    @property
    def play_steps(self) -> list[tuple[float, float | None]]:
        return [(p.ts, p.hours) for p in self.plays]

    @property
    def ratios(self) -> list[tuple[float, float]]:
        if self.reference is None:
            return []
        return [(t.ts, t.mean_cm / self.reference.rebound_cm) for t in self.tests]


def _gather(now: float) -> list[_Work]:
    conn = get_db()
    tests: dict[int, list[_Test]] = {}
    for t in db.list_tests(conn):
        mean = summarize((d.ball_no, d.rebound_cm) for d in t.drops).mean_cm
        if mean is not None:
            tests.setdefault(t.batch_id, []).append(_Test(t.id, t.ts, t.setup_id, t.context, mean))
    sessions: dict[int, list[db.ChargeSession]] = {}
    for s in db.list_sessions(conn):
        sessions.setdefault(s.batch_id, []).append(s)
    plays: dict[int, list[db.Play]] = {}
    for p in db.list_plays(conn):
        plays.setdefault(p.batch_id, []).append(p)
    entered = {
        (r.brand.casefold(), r.model.casefold(), r.setup_id): r.rebound_cm
        for r in db.list_references(conn)
    }

    work = []
    for b in db.list_batches(conn):
        mine = tests.get(b.id, [])
        w = _Work(b, setup_id=mine[-1].setup_id if mine else None, plays=plays.get(b.id, []))
        w.tests = [t for t in mine if t.setup_id == w.setup_id]
        w.sessions = [
            (s, _series(s.mac, s.start_ts, s.end_ts, now)) for s in sessions.get(b.id, [])
        ]
        w.track = charge.build_track((s.start_ts, s.end_ts, series) for s, series in w.sessions)
        work.append(w)

    def new_means(batch_id: int, setup_id: int | None) -> list[float]:
        return [
            t.mean_cm
            for t in tests.get(batch_id, [])
            if t.context == "new" and t.setup_id == setup_id
        ]

    for w in work:
        if w.setup_id is None:
            continue
        brand, model = w.model_key[:2]
        same_model = [
            cm
            for o in work
            if o is not w and o.model_key[:2] == (brand, model)
            for cm in new_means(o.batch.id, w.setup_id)
        ]
        w.reference = new_ball_reference(
            new_means(w.batch.id, w.setup_id),
            same_model,
            w.batch.reference_override_cm,
            entered.get(w.model_key),
        )
    return work


def _fit_models(work: list[_Work], prior: charge.Params) -> dict[tuple, charge.Params]:
    """One fit per brand, model and setup, from every pair of consecutive tests."""
    pairs: dict[tuple, list[charge.Pair]] = {}
    for w in work:
        ratios = w.ratios
        for (t0, r0), (t1, r1) in zip(ratios, ratios[1:], strict=False):
            p = charge.pair(r0, t0, r1, t1, w.track, w.play_steps, prior)
            if p is not None:
                pairs.setdefault(w.model_key, []).append(p)
    return {key: charge.fit(ps, prior) for key, ps in pairs.items()}


# --- the overview ------------------------------------------------------------------------


def _slower_faster(params: charge.Params) -> tuple[charge.Params, charge.Params]:
    """The model one sd either side of its rebound-per-pressure, for likely ranges."""
    floor = 0.1 * params.per_kpa
    return (
        replace(params, per_kpa=max(params.per_kpa - params.per_kpa_sd, floor)),
        replace(params, per_kpa=params.per_kpa + params.per_kpa_sd),
    )


def _batch_json(w: _Work, params: charge.Params, now: float, charge_kpa: float) -> dict:
    stale_after_s = _ctx()["config"].web.stale_after_minutes * 60
    ratios, plays = w.ratios, w.play_steps
    ref = w.reference

    def ratio_at(t: float, p: charge.Params = params) -> float | None:
        """The estimate at ``t``: from the latest test before it, along the pressure since."""
        before = [(ts, r) for ts, r in ratios if ts <= t]
        if not before:
            return None
        return charge.follow(before[-1][1], before[-1][0], [t], w.track, plays, p)[0]

    open_session = next((s for s, _ in w.sessions if s.end_ts is None), None)
    series_now = next((series for s, series in w.sessions if s.end_ts is None), [])
    flags = []
    kpa_now = None
    if open_session is not None:
        if open_session.mac is None:
            flags.append("no_sensor")
        elif not series_now:
            flags.append("no_readings")
        elif now - series_now[-1][0] > stale_after_s:
            flags.append("stale")
        else:
            kpa_now = series_now[-1][1]
    if ratios and any(hours is None for ts, hours in plays if ts > ratios[-1][0]):
        flags.append("play_hours_missing")

    in_canister = open_session is not None
    slower, faster = _slower_faster(params)
    r_now = ratio_at(now)
    advice = charge.advise(r_now, kpa_now, in_canister, params, charge_kpa)
    slow = charge.advise(ratio_at(now, slower), kpa_now, in_canister, slower, charge_kpa)
    fast = charge.advise(ratio_at(now, faster), kpa_now, in_canister, faster, charge_kpa)

    def hold(a: charge.Advice) -> float | None:
        return 0.0 if a.status == charge.LOWER_NOW else a.hold_s

    r_range = [r for r in (ratio_at(now, slower), ratio_at(now, faster)) if r is not None]

    sessions = []
    for n, (s, series) in enumerate(w.sessions):
        prev_end = w.sessions[n - 1][0].end_ts if n else -math.inf
        next_start = w.sessions[n + 1][0].start_ts if n + 1 < len(w.sessions) else math.inf
        before = [t for t in w.tests if (prev_end or -math.inf) <= t.ts <= s.start_ts]
        after = [] if s.end_ts is None else [t for t in w.tests if s.end_ts <= t.ts <= next_start]
        pressure = charge.summarize_pressure(series, now if s.end_ts is None else s.end_ts)

        def test_json(t: _Test, played_from: float, played_to: float) -> dict:
            return {
                "test_id": t.id,
                "ts": t.ts,
                "context": t.context,
                "ratio": None if ref is None else t.mean_cm / ref.rebound_cm,
                # Play between the test and the session blurs what the session did.
                "played": any(played_from < p.ts <= played_to for p in w.plays),
            }

        sessions.append(
            {
                **s.__dict__,
                "pressure": None if pressure is None else pressure.__dict__,
                "before": test_json(before[-1], before[-1].ts, s.start_ts) if before else None,
                "after": test_json(after[0], s.end_ts, after[0].ts) if after else None,
                "start_ratio": ratio_at(s.start_ts),
                "end_ratio": ratio_at(now if s.end_ts is None else s.end_ts),
            }
        )

    marks = [t for s, _ in w.sessions for t in (s.start_ts, s.end_ts) if t is not None]
    line_ts, line_r = charge.estimate_line(
        ratios, now, w.track, plays, params, [*marks, *(p.ts for p in w.plays)]
    )
    known_hours = [p.hours for p in w.plays if p.hours is not None]
    return {
        "batch_id": w.batch.id,
        "setup_id": w.setup_id,
        "reference": None if ref is None else ref.__dict__,
        "in_canister": in_canister,
        "mac": open_session.mac if open_session else None,
        "pressure_kpa": kpa_now,
        "flags": flags,
        "ratio": r_now,
        "ratio_range": [min(r_range), max(r_range)] if r_range else None,
        "advice": {
            "status": advice.status,
            "hold_s": advice.hold_s,
            # Likely range, one sd of the rebound-per-pressure either way; a null end: or longer.
            "hold_s_range": [hold(fast), hold(slow)],
            "charge_s": advice.charge_s,
            "charge_s_range": [fast.charge_s, slow.charge_s],
            "drift_per_day": advice.drift_per_day,
        },
        "tests": [
            {
                "id": t.id,
                "ts": t.ts,
                "context": t.context,
                "mean_cm": t.mean_cm,
                "ratio": None if ref is None else t.mean_cm / ref.rebound_cm,
            }
            for t in w.tests
        ],
        "sessions": sessions,
        "plays": [p.__dict__ for p in w.plays],
        "estimate": {"ts": line_ts, "ratio": line_r},
        "totals": {
            "sessions": len(w.sessions),
            "canister_days": sum(
                ((now if s.end_ts is None else s.end_ts) - s.start_ts) / charge.DAY_S
                for s, _ in w.sessions
            ),
            "plays": len(w.plays),
            "play_hours": sum(known_hours),
            "plays_without_hours": len(w.plays) - len(known_hours),
        },
        "model": {
            "uptake_per_day": params.k_per_day,
            "rebound_pct_per_psi": params.per_kpa * KPA_PER_PSI * 100,
            "rebound_pct_per_psi_sd": params.per_kpa_sd * KPA_PER_PSI * 100,
            "play_loss_pct_per_hour": params.play_per_hour * 100,
            "pairs": params.pairs,  # test pairs it was learned from; 0: the configured values
        },
    }


@charge_api.get("/api/charge")
def charge_all():
    """Every batch's sessions, play, estimated bounce and advice, in one go."""
    config, now = _ctx()["config"], _ctx()["clock"]()
    cfg = config.charge
    prior = charge.configured(
        cfg.uptake_per_day, cfg.rebound_pct_per_psi, cfg.play_loss_pct_per_hour, cfg.maintenance_psi
    )
    charge_kpa = psi_to_kpa(cfg.charge_psi)
    work = _gather(now)
    models = _fit_models(work, prior)
    return jsonify(
        {
            "now": now,
            "settings": {
                "maintenance_kpa": prior.maintenance_kpa,
                "charge_kpa": charge_kpa,
                "margin_kpa": charge.MARGIN_KPA,
                "like_new_tolerance_pct": config.bounce.like_new_tolerance_pct,
            },
            "batches": [
                _batch_json(w, models.get(w.model_key, prior), now, charge_kpa) for w in work
            ],
        }
    )


# --- sessions ----------------------------------------------------------------------------


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")


def _past(value: object, name: str) -> float:
    """A timestamp, defaulting to now; not in the future."""
    ts = _ts(value, name)
    if ts > _ctx()["clock"]() + FUTURE_SLACK_S:
        raise ApiError(f"{name} is in the future")
    return ts


def _check_times(batch_id: int, start: float, end: float | None, exclude: int | None) -> None:
    """A batch is in one place at a time: its sessions can't overlap."""
    if end is not None and end <= start:
        raise ApiError("the session must end after it starts")
    for s in db.list_sessions(get_db(), batch_id):
        if s.id == exclude:
            continue
        if start < (math.inf if s.end_ts is None else s.end_ts) and s.start_ts < (
            math.inf if end is None else end
        ):
            which = "the session still running" if s.end_ts is None else "the session"
            raise ApiError(
                f"this overlaps {which} that started {_when(s.start_ts)}: "
                "end or change that one first",
                409,
            )


def _opened_ts(mac: str | None, start: float) -> float | None:
    """The collector ends a running session when its canister reads empty. A session
    entered or moved after the fact may have that reading behind it already: this is
    when, going by the readings stored, or None."""
    if mac is None:
        return None
    open_kpa_abs = _ctx()["config"].site.atmospheric_kpa + charge.OPEN_KPA
    return db.session_opened_ts(get_db(), mac, start, open_kpa_abs)


@charge_api.post("/api/charge/sessions")
def create_session():
    """Put a batch in a canister. ``start_ts`` defaults to now; give ``end_ts`` too
    to record a session that is already over. Without one, a session whose canister
    has read empty since it was pumped up comes back ended at that reading."""
    body = _body({"batch_id", "mac", "start_ts", "end_ts", "note"}, {"batch_id"})
    batch = _batch(body["batch_id"])
    start = _past(body.get("start_ts"), "start_ts")
    end = None if body.get("end_ts") is None else _past(body["end_ts"], "end_ts")
    mac = _mac(body.get("mac"))
    note = _text(body.get("note"), "note", NOTE_MAX, optional=True)
    if end is None:
        end = _opened_ts(mac, start)
    _check_times(batch.id, start, end, None)
    session_id = db.insert_session(get_db(), batch.id, start, mac=mac, end_ts=end, note=note)
    return jsonify(db.get_session(get_db(), session_id).__dict__), 201


@charge_api.patch("/api/charge/sessions/<int:session_id>")
def update_session(session_id: int):
    """Change a session. ``end_ts`` ends it (the balls came out) at any time in the
    past; null reopens it, and it then stays open until its canister next reads
    empty. Moving a running session's start or canister checks the readings again."""
    current = db.get_session(get_db(), session_id)
    if current is None:
        raise ApiError(f"session {session_id} not found", 404)
    body = _body({"mac", "start_ts", "end_ts", "note"})
    fields = {}
    if "mac" in body:
        fields["mac"] = _mac(body["mac"])
    if "start_ts" in body:
        fields["start_ts"] = _past(body["start_ts"], "start_ts")
    if "end_ts" in body:
        fields["end_ts"] = None if body["end_ts"] is None else _past(body["end_ts"], "end_ts")
    if "note" in body:
        fields["note"] = _text(body["note"], "note", NOTE_MAX, optional=True)
    merged = {**current.__dict__, **fields}
    moved = (merged["mac"], merged["start_ts"]) != (current.mac, current.start_ts)
    if moved and merged["end_ts"] is None and "end_ts" not in body:
        opened = _opened_ts(merged["mac"], merged["start_ts"])
        if opened is not None:
            merged["end_ts"] = fields["end_ts"] = opened
    _check_times(current.batch_id, merged["start_ts"], merged["end_ts"], session_id)
    db.update_session(get_db(), session_id, **fields)
    return jsonify(db.get_session(get_db(), session_id).__dict__)


@charge_api.delete("/api/charge/sessions/<int:session_id>")
def delete_session(session_id: int):
    if not db.delete_session(get_db(), session_id):
        raise ApiError(f"session {session_id} not found", 404)
    return "", 204


# --- play --------------------------------------------------------------------------------


@charge_api.post("/api/charge/plays")
def create_play():
    """The batch was played with. ``hours`` is optional, but the model can only
    learn what play costs from play that has them."""
    body = _body({"batch_id", "ts", "hours", "note"}, {"batch_id"})
    batch = _batch(body["batch_id"])
    hours = body.get("hours")
    if hours is not None:
        hours = _number(hours, "hours", 0.0, MAX_PLAY_HOURS)
        if hours <= 0:
            raise ApiError("hours must be above 0, or left out")
    args = {
        "ts": _past(body.get("ts"), "ts"),
        "hours": hours,
        "note": _text(body.get("note"), "note", NOTE_MAX, optional=True),
    }
    play_id = db.insert_play(get_db(), batch.id, **args)
    return jsonify(db.Play(play_id, batch.id, **args).__dict__), 201


@charge_api.delete("/api/charge/plays/<int:play_id>")
def delete_play(play_id: int):
    if not db.delete_play(get_db(), play_id):
        raise ApiError(f"play {play_id} not found", 404)
    return "", 204
