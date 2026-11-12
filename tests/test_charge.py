import math
import sqlite3
from dataclasses import replace

import pytest

from canister_monitor import bounce, charge, db
from canister_monitor.compensation import psi_to_kpa
from canister_monitor.config import ConfigError, parse_config
from canister_monitor.decoders.base import Reading
from canister_monitor.web import create_app

NOW = 1_700_000_000.0
DAY = 86_400.0
HOUR = 3_600.0
ATM = 101.325
MAC1 = "30:94:A8:11:11:11"
CONFIG = parse_config({"sensors": [{"mac": MAC1, "canister": "Canister 1"}]})

# 0.03 /day, 1.3% of new per psi, 0.3% per hour of play, maintenance at 14 psi.
P = charge.configured(0.03, 1.3, 0.3, 14.0)
MAINT = psi_to_kpa(14.0)
CHARGE = psi_to_kpa(30.0)


def settle(kpa: float) -> float:
    """Where r ends up if the pressure stays at ``kpa``."""
    return 1.0 + P.per_kpa * (kpa - MAINT)


# --- following the pressure --------------------------------------------------------------


def test_configured_converts_the_units_of_the_config():
    assert P.per_kpa == pytest.approx(0.013 / 6.894757)
    assert P.per_kpa_sd == pytest.approx(0.5 * P.per_kpa)
    assert P.play_per_hour == pytest.approx(0.003)
    assert P.maintenance_kpa == pytest.approx(96.527, abs=1e-3)
    assert P.pairs == 0


def test_balls_hold_at_maintenance_pressure_and_fade_outside_a_canister():
    held = charge.build_track([(0.0, None, [(60.0, MAINT)])])
    assert charge.follow(1.0, 0.0, [30 * DAY], held, [], P) == [pytest.approx(1.0)]

    out = charge.Track()  # never in a canister: 0 psi around the balls
    [r] = charge.follow(1.0, 0.0, [10 * DAY], out, [], P)
    assert r == pytest.approx(settle(0.0) + (1 - settle(0.0)) * math.exp(-0.3))
    assert settle(0.0) == pytest.approx(1 - 0.182)  # 14 psi at 1.3% per psi
    [r] = charge.follow(1.0, 0.0, [3000 * DAY], out, [], P)
    assert r == pytest.approx(settle(0.0))  # fully flat in the end


def test_follow_steps_through_a_session_exactly():
    # In at day 1 at 30 psi, lowered to 14 psi at day 4, out at day 9.
    track = charge.build_track([(1 * DAY, 9 * DAY, [(1 * DAY + 60, CHARGE), (4 * DAY, MAINT)])])
    assert track.at(0.0) == 0.0 and track.at(2 * DAY) == CHARGE
    assert track.at(5 * DAY) == MAINT and track.at(9 * DAY) == 0.0

    def leg(r, kpa, days):
        return settle(kpa) + (r - settle(kpa)) * math.exp(-0.03 * days)

    r1 = leg(0.95, 0.0, 1)
    r4 = leg(r1, CHARGE, 3)
    r9 = leg(r4, MAINT, 5)
    r12 = leg(r9, 0.0, 3)
    got = charge.follow(0.95, 0.0, [1 * DAY, 4 * DAY, 9 * DAY, 12 * DAY], track, [], P)
    assert got == [pytest.approx(v) for v in (r1, r4, r9, r12)]
    assert r4 > r1 and r12 < r9
    # Asking for more times in between doesn't change where it ends up.
    dense = charge.follow(0.95, 0.0, [d * DAY / 7 for d in range(1, 85)], track, [], P)
    assert dense[-1] == pytest.approx(r12)
    # Starting part-way through gives the same as arriving there.
    assert charge.follow(r4, 4 * DAY, [12 * DAY], track, [], P) == [pytest.approx(r12)]


def test_play_takes_bounce_off_when_it_happens():
    held = charge.build_track([(0.0, None, [(0.0, MAINT)])])
    plays = [(0.0, 5.0), (2 * DAY, 2.0), (3 * DAY, None), (4 * DAY, 1.5)]
    got = charge.follow(1.0, 0.0, [1 * DAY, 2 * DAY, 5 * DAY], held, plays, P)
    assert got[0] == pytest.approx(1.0)  # play at the test's own time came before it
    assert got[1] == pytest.approx(1.0 - 2 * 0.003)  # play at t has happened by t
    # At maintenance pressure the loss is slowly made up; play with no hours takes nothing.
    after_first = 1 + (-0.006) * math.exp(-0.03 * 2)
    assert got[2] == pytest.approx(1 + (after_first - 1 - 0.0045) * math.exp(-0.03))


def test_a_canister_without_readings_stops_the_estimate():
    track = charge.build_track([(2 * DAY, 5 * DAY, [])])
    assert track.kpa == (None, 0.0)
    got = charge.follow(1.0, 0.0, [1 * DAY, 2 * DAY, 3 * DAY, 8 * DAY], track, [], P)
    assert got[0] is not None and got[1] is not None
    assert got[2:] == [None, None]  # can't pick up again after the gap either


def test_build_track_uses_only_readings_inside_each_session():
    track = charge.build_track(
        [
            (100.0, 200.0, [(90.0, 50.0), (130.0, 210.0), (150.0, 205.0), (200.0, 99.0)]),
            (300.0, None, [(300.0, 150.0), (340.0, 149.0)]),
        ]
    )
    # The first reading also covers the time from the start; the end drops to 0.
    assert track.ts == (100.0, 130.0, 150.0, 200.0, 300.0, 340.0)
    assert track.kpa == (210.0, 210.0, 205.0, 0.0, 150.0, 149.0)
    assert track.at(250.0) == 0.0 and track.at(1e9) == 149.0


def test_pressure_summary_weights_by_time():
    s = charge.summarize_pressure([(0.0, 200.0), (100.0, 220.0), (300.0, 100.0)], until=1300.0)
    assert (s.start_kpa, s.peak_kpa, s.end_kpa, s.readings) == (200.0, 220.0, 100.0, 3)
    assert s.mean_kpa == pytest.approx((200 * 100 + 220 * 200 + 100 * 1000) / 1300)
    assert charge.summarize_pressure([], until=5.0) is None
    assert charge.summarize_pressure([(7.0, 123.0)], until=7.0).mean_kpa == 123.0


def test_estimate_line_restarts_at_each_test():
    track = charge.build_track([(2 * DAY, None, [(2 * DAY, CHARGE)])])
    tests = [(0.0, 1.0), (4 * DAY, 0.93)]
    xs, ys = charge.estimate_line(tests, 6 * DAY, track, [], P, marks=[2 * DAY], points=12)
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    assert xs[0] == 0.0 and ys[0] == 1.0 and xs[-1] == 6 * DAY
    assert 2 * DAY in xs  # the session's start gets its own point: the line turns there
    i = xs.index(4 * DAY)
    assert ys[i] == 0.93  # the second test's own point, not the model's guess
    assert xs[i - 1] == 4 * DAY - 1  # the line before it stops just short
    assert ys[i - 1] == pytest.approx(charge.follow(1.0, 0.0, [4 * DAY - 1], track, [], P)[0])
    assert ys[-1] == pytest.approx(charge.follow(0.93, 4 * DAY, [6 * DAY], track, [], P)[0])
    assert charge.estimate_line([], 6 * DAY, track, [], P) == ([], [])
    assert charge.estimate_line([(7 * DAY, 1.0)], 6 * DAY, track, [], P) == ([], [])


# --- advice ------------------------------------------------------------------------------


def test_time_to_new_is_when_follow_gets_there():
    t = charge.time_to_new(0.95, CHARGE, P)
    track = charge.build_track([(0.0, None, [(0.0, CHARGE)])])
    assert charge.follow(0.95, 0.0, [t], track, [], P) == [pytest.approx(1.0)]
    assert 6 * DAY < t < 9 * DAY  # 5% short at 16 psi over: about a week
    assert charge.time_to_new(1.0, CHARGE, P) == 0.0
    assert charge.time_to_new(1.02, 0.0, P) == 0.0
    assert charge.time_to_new(0.95, MAINT, P) is None  # maintenance pressure only holds
    assert charge.time_to_new(0.95, 0.0, P) is None


@pytest.mark.parametrize(
    "r, psi, in_canister, status",
    [
        (None, 30.0, True, charge.UNKNOWN),
        (0.96, 0.0, False, charge.OUT),
        (0.96, None, True, charge.NO_PRESSURE),
        (0.96, 30.0, True, charge.CHARGING),
        (0.96, 16.5, True, charge.CHARGING),  # just over the margin: slow, but charging
        (1.0, 30.0, True, charge.LOWER_NOW),
        (1.03, 30.0, True, charge.LOWER_NOW),
        (1.0, 14.0, True, charge.HOLDING),
        (0.99, 15.5, True, charge.HOLDING),  # within 2 psi of maintenance, 1% short: fine
        (1.03, 12.5, True, charge.HOLDING),
        (0.96, 14.0, True, charge.RAISE),
        (0.96, 5.0, True, charge.RAISE),
        (1.0, 5.0, True, charge.LOW),
    ],
)
def test_advice_statuses(r, psi, in_canister, status):
    kpa = None if psi is None else psi_to_kpa(psi)
    assert charge.advise(r, kpa, in_canister, P, CHARGE).status == status


def test_advice_times_and_drift():
    a = charge.advise(0.95, CHARGE, True, P, CHARGE)
    assert a.hold_s == a.charge_s == pytest.approx(charge.time_to_new(0.95, CHARGE, P))
    assert a.drift_per_day == pytest.approx(0.03 * (settle(CHARGE) - 0.95))

    slow = charge.advise(0.95, psi_to_kpa(17.0), True, P, CHARGE)
    assert slow.status == charge.CHARGING and slow.hold_s > 3 * slow.charge_s

    out = charge.advise(0.95, None, False, P, CHARGE)
    assert out.hold_s is None and out.charge_s == pytest.approx(a.charge_s)
    assert out.drift_per_day == pytest.approx(0.03 * (settle(0.0) - 0.95))  # losing
    assert out.drift_per_day < 0

    done = charge.advise(1.01, CHARGE, True, P, CHARGE)
    assert (done.hold_s, done.charge_s) == (0.0, 0.0)
    assert charge.advise(1.0, MAINT, True, P, CHARGE).drift_per_day == pytest.approx(0.0)


# --- learning from tests -----------------------------------------------------------------


def make_pair(truth, r0, t0, t1, track, plays=(), noise=0.0):
    r1 = charge.follow(r0, t0, [t1], track, list(plays), truth)[0]
    return charge.pair(r0, t0, r1 + noise, t1, track, list(plays), P)


def test_pair_is_linear_in_what_is_learned():
    track = charge.build_track([(1 * DAY, 6 * DAY, [(1 * DAY, CHARGE), (3 * DAY, MAINT)])])
    plays = [(7 * DAY, 2.0)]
    truth = replace(P, per_kpa=1.7 * P.per_kpa, play_per_hour=0.006)
    p = make_pair(truth, 0.94, 0.0, 8 * DAY, track, plays)
    decay = math.exp(-0.03 * 8)
    assert p.r1 == pytest.approx(
        1 + (0.94 - 1) * decay + truth.per_kpa * p.x - truth.play_per_hour * p.h
    )
    assert 1.9 < p.h < 2.0  # 2 h of play, a day of it made up since
    # Nothing to learn from: no time, a canister with no readings, play with no hours.
    assert charge.pair(1.0, 5.0, 0.99, 5.0, track, [], P) is None
    blind = charge.build_track([(1 * DAY, 6 * DAY, [])])
    assert charge.pair(1.0, 0.0, 0.99, 8 * DAY, blind, [], P) is None
    assert charge.pair(1.0, 0.0, 0.99, 8 * DAY, track, [(7 * DAY, None)], P) is None
    assert charge.pair(1.0, 0.0, 0.99, 6 * DAY, track, [(7 * DAY, None)], P) is not None


def test_fit_returns_the_configured_values_without_pairs():
    assert charge.fit([], P) == P


def test_fit_recovers_the_truth_from_sessions_and_play():
    truth = replace(P, per_kpa=1.6 * P.per_kpa, play_per_hour=0.0055)
    pairs = []
    for n in range(8):
        t0 = n * 20 * DAY
        charged = charge.build_track([(t0, t0 + 6 * DAY, [(t0, CHARGE + 10 * n)])])
        pairs.append(make_pair(truth, 0.93 + 0.005 * n, t0, t0 + 6 * DAY, charged))
        idle = charge.Track()
        pairs.append(make_pair(truth, 1.0, t0, t0 + 3 * DAY, idle, [(t0 + DAY, 1.0 + n / 2)]))
    fitted = charge.fit(pairs, P)
    assert fitted.pairs == 16
    assert fitted.per_kpa == pytest.approx(truth.per_kpa, rel=0.03)
    # 22 hours of play in all: most of the way from the configured 0.3% per hour to 0.55%.
    assert fitted.play_per_hour == pytest.approx(truth.play_per_hour, rel=0.12)
    assert fitted.play_per_hour < truth.play_per_hour
    assert fitted.per_kpa_sd < 0.25 * P.per_kpa_sd  # much better known than at the start
    assert (fitted.k_per_day, fitted.maintenance_kpa) == (P.k_per_day, P.maintenance_kpa)


def test_one_short_session_only_nudges_the_configured_value():
    truth = replace(P, per_kpa=2.0 * P.per_kpa)
    track = charge.build_track([(0.0, 6 * HOUR, [(0.0, CHARGE)])])
    fitted = charge.fit([make_pair(truth, 0.97, 0.0, 6 * HOUR, track)], P)
    assert P.per_kpa < fitted.per_kpa < 1.05 * P.per_kpa
    assert fitted.per_kpa_sd == pytest.approx(P.per_kpa_sd, rel=0.05)


def test_fit_keeps_play_from_adding_bounce_and_the_slope_in_range():
    idle = charge.Track()
    # Tests that came out higher after play: the play term goes to 0, not negative.
    up = [
        charge.pair(0.97, n * DAY, 1.0, n * DAY + HOUR, idle, [(n * DAY + 60, 2.0)], P)
        for n in range(6)
    ]
    assert charge.fit(up, P).play_per_hour == 0.0

    track = charge.build_track([(0.0, 5 * DAY, [(0.0, CHARGE)])])
    wild = replace(P, per_kpa=40 * P.per_kpa)
    high = charge.fit([make_pair(wild, 0.95, 0.0, 5 * DAY, track)] * 8, P)
    assert high.per_kpa == pytest.approx(4 * P.per_kpa)
    falling = [charge.pair(0.99, 0.0, 0.90, 5 * DAY, track, [], P)] * 8  # lost bounce at 30 psi
    assert charge.fit(falling, P).per_kpa == pytest.approx(0.25 * P.per_kpa)


def test_new_ball_reference_order():
    ref = bounce.new_ball_reference
    assert ref([110.0, 112.0], [120.0], 130.0, 140.0) == bounce.NewBallReference(
        111.0, "own_new", 2
    )
    assert ref([], [120.0, 121.0, 122.0], 130.0, 140.0) == bounce.NewBallReference(
        121.0, "same_model", 3
    )
    assert ref([], [], 130.0, 140.0) == bounce.NewBallReference(130.0, "batch_value", 0)
    assert ref([], [], None, 140.0) == bounce.NewBallReference(140.0, "entered", 0)
    assert ref([], [], None, None) is None


# --- config ------------------------------------------------------------------------------


def test_charge_config_defaults_and_validation():
    c = parse_config({}).charge
    assert (c.maintenance_psi, c.charge_psi, c.uptake_per_day) == (14.0, 30.0, 0.03)
    assert (c.rebound_pct_per_psi, c.play_loss_pct_per_hour) == (1.3, 0.3)
    assert (
        parse_config({"charge": {"maintenance_psi": 12, "charge_psi": 45}}).charge.charge_psi == 45
    )
    assert (
        parse_config({"charge": {"play_loss_pct_per_hour": 0}}).charge.play_loss_pct_per_hour == 0
    )
    for bad, message in [
        ({"maintenance_psi": 0}, "maintenance_psi must be greater than 0"),
        ({"uptake_per_day": -1}, "uptake_per_day must be greater than 0"),
        ({"rebound_pct_per_psi": 0}, "rebound_pct_per_psi must be greater than 0"),
        ({"play_loss_pct_per_hour": -0.1}, "can't be negative"),
        ({"charge_psi": 16}, "at least 4 psi above maintenance_psi"),
        ({"charge_pressure": 30}, "unknown key"),
    ]:
        with pytest.raises(ConfigError, match=message):
            parse_config({"charge": bad})


# --- storage -----------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "charge.db")
    yield c
    c.close()


def new_batch(conn, **kw):
    fields = {"brand": "Dunlop", "model": "ATP", "opened_ts": NOW - 30 * DAY, "n_balls": 9}
    fields.update(sample_size=9, **kw)
    return db.insert_batch(conn, **fields)


def test_migration_from_v5_adds_sessions_and_keeps_the_rest(tmp_path):
    path = tmp_path / "v5.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in enumerate(db.MIGRATIONS[:5], start=1):
        c.executescript(sql)
        c.execute("INSERT INTO schema_version VALUES (?)", (version,))
    c.executescript(
        """
        INSERT INTO ball_batches (id, brand, model, opened_ts, n_balls, sample_size)
            VALUES (1, 'Dunlop', 'ATP', 1, 9, 9);
        INSERT INTO events (ts, mac, kind, note, batch_id) VALUES (5, NULL, 'balls_added', 'in', 1);
        """
    )
    c.commit()
    c.close()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 6
    assert db.list_sessions(conn) == [] and db.list_plays(conn) == []
    assert db.batch_locations(conn) == {1: ("balls_added", None)}  # the old note still counts
    session_id = db.insert_session(conn, 1, 10.0, MAC1)
    assert db.get_session(conn, session_id) == db.ChargeSession(
        session_id, 1, MAC1, 10.0, None, None
    )
    conn.close()


def test_sessions_and_plays_round_trip(conn):
    batch = new_batch(conn)
    first = db.insert_session(conn, batch, 100.0, MAC1, end_ts=200.0, note="first")
    second = db.insert_session(conn, batch, 300.0)
    assert [s.id for s in db.list_sessions(conn, batch)] == [first, second]
    assert db.update_session(conn, second, mac=MAC1, end_ts=400.0, note="done")
    assert db.get_session(conn, second) == db.ChargeSession(
        second, batch, MAC1, 300.0, 400.0, "done"
    )
    assert db.update_session(conn, second, end_ts=None)  # reopened
    assert db.get_session(conn, second).end_ts is None
    assert db.update_session(conn, second) and not db.update_session(conn, 999, note="x")
    assert db.delete_session(conn, first) and not db.delete_session(conn, first)

    play = db.insert_play(conn, batch, 250.0, 1.5, "doubles")
    db.insert_play(conn, batch, 240.0)
    assert db.list_plays(conn, batch)[1] == db.Play(play, batch, 250.0, 1.5, "doubles")
    assert db.list_plays(conn)[0].hours is None
    assert db.delete_play(conn, play) and not db.delete_play(conn, play)

    db.delete_batch(conn, batch)  # takes its sessions and play with it
    assert db.list_sessions(conn) == [] and db.list_plays(conn) == []


OPEN_ABS = ATM + charge.OPEN_KPA


def store(conn, ts, psi, mac=MAC1):
    db.insert_reading(conn, Reading(ts, mac, ATM + psi_to_kpa(psi), 20.0, decoder="x"), None)


def test_an_empty_reading_ends_the_sessions_running_in_that_canister(conn):
    batch, other = new_batch(conn), new_batch(conn, model="Tour")
    done = db.insert_session(conn, batch, 50.0, MAC1, end_ts=90.0)
    running = db.insert_session(conn, batch, 100.0, MAC1)
    shared = db.insert_session(conn, other, 150.0, MAC1)
    elsewhere = db.insert_session(conn, other, 10.0, "30:94:A8:22:22:22", end_ts=20.0)
    no_sensor = db.insert_session(conn, new_batch(conn, model="Fort"), 100.0)
    for ts in (120.0, 200.0, 300.0):
        store(conn, ts, 30.0)
    store(conn, 400.0, 0.0)

    assert db.end_sessions_at(conn, MAC1, 400.0, OPEN_ABS) == [running, shared]
    assert [db.get_session(conn, s).end_ts for s in (running, shared)] == [400.0, 400.0]
    assert db.get_session(conn, done).end_ts == 90.0  # already over: left alone
    assert db.get_session(conn, elsewhere).end_ts == 20.0
    assert db.get_session(conn, no_sensor).end_ts is None
    assert db.end_sessions_at(conn, MAC1, 410.0, OPEN_ABS) == []  # the sensor repeats itself


def test_an_empty_reading_before_the_canister_is_pumped_up_ends_nothing(conn):
    batch = new_batch(conn)
    store(conn, 50.0, 30.0)  # the stay before this one
    store(conn, 90.0, 0.0)
    session = db.insert_session(conn, batch, 100.0, MAC1)
    store(conn, 110.0, 0.0)  # balls in, lid on, not pumped up yet
    assert db.end_sessions_at(conn, MAC1, 110.0, OPEN_ABS) == []
    assert db.session_opened_ts(conn, MAC1, 100.0, OPEN_ABS) is None

    store(conn, 120.0, 30.0)
    store(conn, 500.0, 12.0)  # lowered to maintenance pressure: not an end
    assert db.session_opened_ts(conn, MAC1, 100.0, OPEN_ABS) is None
    store(conn, 600.0, 0.9)  # under 1 psi is open
    store(conn, 610.0, 0.0)
    assert db.session_opened_ts(conn, MAC1, 100.0, OPEN_ABS) == 600.0
    assert db.end_sessions_at(conn, MAC1, 600.0, OPEN_ABS) == [session]
    # A reading from before a session started is never its end.
    assert db.session_opened_ts(conn, MAC1, 605.0, OPEN_ABS) is None


def test_batch_locations_follow_sessions_and_notes(conn):
    a, b, c = new_batch(conn), new_batch(conn), new_batch(conn)
    db.insert_session(conn, a, 100.0, MAC1, end_ts=200.0)
    db.insert_session(conn, a, 200.0, MAC1)  # straight back in: still in
    db.insert_session(conn, b, 100.0, MAC1, end_ts=200.0)
    db.insert_event(conn, 50.0, "balls_added", None, MAC1, c)  # a note from before sessions
    assert db.batch_locations(conn) == {
        a: ("balls_added", MAC1),
        b: ("balls_removed", MAC1),
        c: ("balls_added", MAC1),
    }
    db.insert_event(conn, 250.0, "balls_removed", None, None, a)  # a later note wins
    assert db.batch_locations(conn)[a] == ("balls_removed", None)


def test_a_session_start_counts_as_a_fill_and_ends_a_leak_test(conn):
    batch = new_batch(conn)
    db.insert_event(conn, 100.0, "leak_test", None, MAC1)
    db.insert_event(conn, 150.0, "fill", None, MAC1)
    assert db.latest_fill_ts(conn, MAC1, 1000.0) == 150.0
    db.insert_event(conn, 300.0, "leak_test", None, MAC1)
    db.insert_session(conn, batch, 500.0, MAC1)
    db.insert_session(conn, new_batch(conn), 600.0, "30:94:A8:22:22:22")  # another canister
    assert db.latest_fill_ts(conn, MAC1, 1000.0) == 500.0
    assert db.latest_fill_ts(conn, MAC1, 400.0) == 300.0
    assert db.leak_test_spans(conn, MAC1, 1000.0) == [(100.0, 150.0), (300.0, 500.0)]


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    app = create_app(CONFIG, db_path=tmp_path / "charge.db", clock=lambda: NOW)
    app.testing = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def error_of(resp, status=400) -> str:
    assert resp.status_code == status, resp.get_data(as_text=True)
    return resp.get_json()["error"]


def post(client, path, body, status=201):
    resp = client.post(path, json=body)
    assert resp.status_code == status, resp.get_data(as_text=True)
    return resp.get_json()


def add_readings(app, start, end, psi, mac=MAC1, every=600.0, temp=20.0):
    """A canister held at ``psi`` (or following ``psi(ts)``) from start to end."""
    conn = db.connect(app.extensions["canister"]["db_path"])
    ts = start
    while ts <= end:
        gauge = psi_to_kpa(psi(ts) if callable(psi) else psi)
        db.insert_reading(conn, Reading(ts, mac, ATM + gauge, temp, decoder="fbb0_ac00"), None)
        ts += every
    conn.close()


def add_test(client, batch, setup, ts, mean_cm, context="before"):
    drops = [{"ball_no": n, "rebound_cm": mean_cm + off} for n in (1, 2, 3) for off in (-1, 0, 1)]
    body = {"batch_id": batch, "setup_id": setup, "context": context, "ts": ts, "drops": drops}
    return post(client, "/api/bounce/tests", body)["id"]


@pytest.fixture
def seeded(client):
    setup = post(client, "/api/bounce/setups", {"name": "Garage", "drop_cm": 200.0})["id"]
    batch = post(
        client,
        "/api/bounce/batches",
        {"brand": "Dunlop", "model": "ATP", "n_balls": 9, "opened_ts": NOW - 20 * DAY},
    )["id"]
    return client, setup, batch


def overview(client, batch):
    body = client.get("/api/charge").get_json()
    return body, next(b for b in body["batches"] if b["batch_id"] == batch)


def test_overview_of_a_batch_with_nothing_recorded(seeded):
    client, _, batch = seeded
    body, b = overview(client, batch)
    assert body["now"] == NOW
    assert body["settings"]["maintenance_kpa"] == pytest.approx(MAINT)
    assert body["settings"]["charge_kpa"] == pytest.approx(CHARGE)
    assert body["settings"]["like_new_tolerance_pct"] == 5.0
    assert b["reference"] is None and b["ratio"] is None and b["setup_id"] is None
    assert b["advice"]["status"] == "unknown" and not b["in_canister"]
    assert b["sessions"] == [] and b["plays"] == [] and b["tests"] == []
    assert b["estimate"] == {"ts": [], "ratio": []}
    assert b["model"]["pairs"] == 0
    assert b["model"]["rebound_pct_per_psi"] == pytest.approx(1.3)
    assert b["model"]["play_loss_pct_per_hour"] == pytest.approx(0.3)


def test_charging_batch_gets_a_time_to_hold_then_is_told_to_lower(seeded, app):
    client, setup, batch = seeded
    add_test(client, batch, setup, NOW - 10 * DAY, 110.0, "new")
    add_test(client, batch, setup, NOW - 2 * DAY, 105.6, "after_play")  # 96% of new
    add_readings(app, NOW - 3 * DAY, NOW - 60, 30.0)  # the canister was up before they went in
    session = post(
        client,
        "/api/charge/sessions",
        {"batch_id": batch, "mac": MAC1, "start_ts": NOW - 2 * DAY + HOUR, "note": "after doubles"},
    )
    assert session["end_ts"] is None and session["mac"] == MAC1

    _, b = overview(client, batch)
    assert b["reference"] == {"rebound_cm": 110.0, "source": "own_new", "tests": 1}
    assert b["in_canister"] and b["mac"] == MAC1 and b["flags"] == []
    assert b["pressure_kpa"] == pytest.approx(CHARGE)
    assert [t["ratio"] for t in b["tests"]] == [pytest.approx(1.0), pytest.approx(0.96)]

    # The 8 days on the shelf between the two tests already taught the model a little.
    assert b["model"]["pairs"] == 1
    assert b["model"]["rebound_pct_per_psi"] == pytest.approx(1.3, rel=0.05)
    fitted = replace(P, per_kpa=b["model"]["rebound_pct_per_psi"] / 100 / psi_to_kpa(1.0))
    # An hour on the shelf, then 30 psi since.
    r_in = charge.follow(0.96, 0.0, [HOUR], charge.Track(), [], fitted)[0]
    held = charge.build_track([(0.0, None, [(0.0, CHARGE)])])
    r_now = charge.follow(r_in, 0.0, [2 * DAY - HOUR], held, [], fitted)[0]
    assert b["ratio"] == pytest.approx(r_now, abs=1e-5)
    assert 0.96 < b["ratio"] < 1.0
    lo, hi = b["ratio_range"]
    assert lo < b["ratio"] < hi

    a = b["advice"]
    assert a["status"] == "charging"
    assert a["hold_s"] == pytest.approx(charge.time_to_new(r_now, CHARGE, fitted), rel=1e-3)
    assert 3 * DAY < a["hold_s"] < 6 * DAY
    quick, slow = a["hold_s_range"]
    assert quick < a["hold_s"] < slow  # the rebound-per-psi could be off either way
    assert a["drift_per_day"] > 0

    [s] = b["sessions"]
    assert s["id"] == session["id"] and s["note"] == "after doubles"
    assert s["pressure"]["start_kpa"] == pytest.approx(CHARGE)
    assert s["pressure"]["mean_kpa"] == pytest.approx(CHARGE)
    assert s["before"]["ratio"] == pytest.approx(0.96) and not s["before"]["played"]
    assert s["after"] is None
    assert s["start_ratio"] == pytest.approx(r_in, abs=1e-5)
    assert s["end_ratio"] == pytest.approx(b["ratio"])

    xs, ys = b["estimate"]["ts"], b["estimate"]["ratio"]
    assert xs[0] == NOW - 10 * DAY and xs[-1] == NOW and xs == sorted(xs)
    assert ys[0] == pytest.approx(1.0) and ys[-1] == pytest.approx(b["ratio"])
    assert NOW - 2 * DAY + HOUR in xs  # the line turns where the session starts
    assert b["totals"]["sessions"] == 1
    assert b["totals"]["canister_days"] == pytest.approx(2 - 1 / 24)

    # A test later finds them back at new while the canister is still up: lower it.
    add_test(client, batch, setup, NOW - HOUR, 110.4, "other")
    _, b = overview(client, batch)
    assert b["ratio"] > 1.0 and b["advice"]["status"] == "lower_now"
    assert b["advice"]["hold_s"] == 0.0 and b["advice"]["hold_s_range"][0] == 0.0


def test_lowered_to_maintenance_the_batch_holds_or_needs_raising(seeded, app):
    client, setup, batch = seeded
    add_test(client, batch, setup, NOW - 12 * DAY, 110.0, "new")
    add_readings(app, NOW - 10 * DAY, NOW - 60, lambda ts: 30.0 if ts < NOW - 4 * DAY else 14.4)
    post(
        client, "/api/charge/sessions", {"batch_id": batch, "mac": MAC1, "start_ts": NOW - 10 * DAY}
    )
    _, b = overview(client, batch)
    assert b["advice"]["status"] == "holding" and b["ratio"] > 1.0
    assert b["advice"]["hold_s"] is None and b["advice"]["charge_s"] == 0.0
    [s] = b["sessions"]
    assert s["pressure"]["peak_kpa"] == pytest.approx(CHARGE)
    assert s["pressure"]["end_kpa"] == pytest.approx(psi_to_kpa(14.4))
    assert psi_to_kpa(14.4) < s["pressure"]["mean_kpa"] < CHARGE

    # A test shows them 4% short after all: maintenance pressure won't bring them back.
    add_test(client, batch, setup, NOW - HOUR, 105.6, "other")
    _, b = overview(client, batch)
    a = b["advice"]
    assert a["status"] == "raise" and a["hold_s"] is None
    assert a["charge_s"] > 2 * DAY
    assert a["charge_s_range"][0] < a["charge_s"] < a["charge_s_range"][1]


def test_out_of_a_canister_the_batch_fades_and_play_costs(seeded):
    client, setup, batch = seeded
    add_test(client, batch, setup, NOW - 5 * DAY, 110.0, "new")
    _, b = overview(client, batch)
    faded = charge.follow(1.0, 0.0, [5 * DAY], charge.Track(), [], P)[0]
    assert b["ratio"] == pytest.approx(faded) and b["ratio"] < 0.98
    assert b["advice"]["status"] == "out" and b["advice"]["drift_per_day"] < 0
    assert b["advice"]["charge_s"] == pytest.approx(charge.time_to_new(faded, CHARGE, P))

    play = post(client, "/api/charge/plays", {"batch_id": batch, "ts": NOW - DAY, "hours": 2})
    assert play["hours"] == 2 and play["note"] is None
    _, b = overview(client, batch)
    assert b["ratio"] == pytest.approx(faded - 0.006, abs=3e-4) and b["flags"] == []
    assert b["totals"]["plays"] == 1 and b["totals"]["play_hours"] == 2

    post(client, "/api/charge/plays", {"batch_id": batch, "note": "a quick hit"})
    _, b = overview(client, batch)
    assert b["flags"] == ["play_hours_missing"]  # the estimate can't count that one
    assert b["totals"]["plays_without_hours"] == 1
    assert [p["hours"] for p in b["plays"]] == [2, None]

    assert client.delete(f"/api/charge/plays/{play['id']}").status_code == 204
    error_of(client.delete(f"/api/charge/plays/{play['id']}"), 404)


def test_a_finished_session_is_bracketed_by_its_tests_and_teaches_the_model(seeded, app):
    client, setup, batch = seeded
    add_test(client, batch, setup, NOW - 15 * DAY, 110.0, "new")
    before = add_test(client, batch, setup, NOW - 9 * DAY, 104.5, "before")
    add_readings(app, NOW - 9 * DAY, NOW - 3 * DAY, 30.0)
    s = post(
        client,
        "/api/charge/sessions",
        {"batch_id": batch, "mac": MAC1, "start_ts": NOW - 9 * DAY + 600, "end_ts": NOW - 3 * DAY},
    )
    # They came out much livelier than the configured rate predicts.
    after = add_test(client, batch, setup, NOW - 3 * DAY + 600, 113.3, "after")
    _, b = overview(client, batch)
    assert not b["in_canister"] and b["advice"]["status"] == "out"
    [got] = b["sessions"]
    assert got["end_ts"] == NOW - 3 * DAY
    assert (got["before"]["test_id"], got["after"]["test_id"]) == (before, after)
    assert got["after"]["ratio"] == pytest.approx(1.03) and not got["after"]["played"]
    m = b["model"]
    assert m["pairs"] == 2
    assert 1.3 < m["rebound_pct_per_psi"] < 5.2  # learned upward from 1.3
    assert m["rebound_pct_per_psi_sd"] < 0.65

    # Play between coming out and the test is flagged on the "after" test.
    post(client, "/api/charge/plays", {"batch_id": batch, "ts": NOW - 3 * DAY + 300, "hours": 1})
    _, b = overview(client, batch)
    assert b["sessions"][0]["after"]["played"]

    # Ending, reopening and editing.
    resp = client.patch(f"/api/charge/sessions/{s['id']}", json={"end_ts": None})
    assert resp.get_json()["end_ts"] is None
    resp = client.patch(f"/api/charge/sessions/{s['id']}", json={"end_ts": NOW - DAY, "note": "x"})
    assert resp.get_json() == {**s, "end_ts": NOW - DAY, "note": "x"}
    assert client.delete(f"/api/charge/sessions/{s['id']}").status_code == 204
    error_of(client.delete(f"/api/charge/sessions/{s['id']}"), 404)
    error_of(client.patch(f"/api/charge/sessions/{s['id']}", json={"note": "y"}), 404)


def test_reference_falls_back_to_the_same_model_then_typed_values(seeded):
    client, setup, batch = seeded
    other = post(client, "/api/bounce/batches", {"brand": "dunlop", "model": "atp", "n_balls": 9})
    add_test(client, other["id"], setup, NOW - 4 * DAY, 106.0, "after_play")
    _, b = overview(client, other["id"])
    assert b["reference"] is None and b["tests"][0]["ratio"] is None
    assert b["advice"]["status"] == "unknown"

    post(
        client,
        "/api/bounce/references",
        {"brand": "Dunlop", "model": "ATP", "setup_id": setup, "rebound_cm": 112.0},
    )
    assert overview(client, other["id"])[1]["reference"]["source"] == "entered"
    client.patch(f"/api/bounce/batches/{other['id']}", json={"reference_override_cm": 111.0})
    assert overview(client, other["id"])[1]["reference"]["source"] == "batch_value"
    add_test(client, batch, setup, NOW - 6 * DAY, 110.0, "new")  # the first batch, tested new
    ref = overview(client, other["id"])[1]["reference"]
    assert ref == {"rebound_cm": 110.0, "source": "same_model", "tests": 1}
    assert overview(client, other["id"])[1]["tests"][0]["ratio"] == pytest.approx(106 / 110)


def test_a_session_without_fresh_readings_is_flagged(seeded, app):
    client, setup, batch = seeded
    add_test(client, batch, setup, NOW - 5 * DAY, 110.0, "new")
    s = post(client, "/api/charge/sessions", {"batch_id": batch, "start_ts": NOW - 2 * DAY})
    _, b = overview(client, batch)
    assert b["flags"] == ["no_sensor"] and b["ratio"] is None
    assert b["advice"]["status"] == "unknown" and b["sessions"][0]["pressure"] is None

    client.patch(f"/api/charge/sessions/{s['id']}", json={"mac": MAC1})
    assert overview(client, batch)[1]["flags"] == ["no_readings"]

    add_readings(app, NOW - 2 * DAY, NOW - 3 * HOUR, 30.0)  # silent for 3 h: stale after 90 min
    _, b = overview(client, batch)
    assert b["flags"] == ["stale"] and b["pressure_kpa"] is None
    assert b["advice"]["status"] == "no_pressure" and b["ratio"] is not None

    add_readings(app, NOW - 600, NOW - 600, 29.5)
    _, b = overview(client, batch)
    assert b["flags"] == [] and b["advice"]["status"] == "charging"
    assert b["pressure_kpa"] == pytest.approx(psi_to_kpa(29.5))


def test_session_validation(seeded):
    client, _, batch = seeded
    url = "/api/charge/sessions"
    first = post(
        client, url, {"batch_id": batch, "start_ts": NOW - 5 * DAY, "end_ts": NOW - 3 * DAY}
    )
    running = post(client, url, {"batch_id": batch, "start_ts": NOW - DAY})
    for body, status, message in [
        ({}, 400, "missing field(s): batch_id"),
        ({"batch_id": 999}, 400, "batch 999 not found"),
        ({"batch_id": batch, "canister": "x"}, 400, "unknown field(s): canister"),
        ({"batch_id": batch, "mac": "nope"}, 400, "mac must look like"),
        ({"batch_id": batch, "start_ts": "now"}, 400, "start_ts must be a number"),
        ({"batch_id": batch, "start_ts": NOW + HOUR}, 400, "start_ts is in the future"),
        ({"batch_id": batch, "start_ts": NOW - 9 * DAY, "end_ts": NOW + HOUR}, 400, "end_ts is in"),
        ({"batch_id": batch, "start_ts": NOW - 8 * DAY, "end_ts": NOW - 9 * DAY}, 400, "end after"),
        ({"batch_id": batch, "start_ts": NOW - 4 * DAY, "end_ts": NOW - 2 * DAY}, 409, "overlaps"),
        ({"batch_id": batch, "start_ts": NOW - 6 * DAY}, 409, "overlaps the session that"),
        ({"batch_id": batch}, 409, "overlaps the session still running"),
        ({"batch_id": batch, "note": "x" * 1001}, 400, "note must be text"),
    ]:
        assert message in error_of(client.post(url, json=body), status), body
    # Back to back is fine, and another batch can share the canister and the dates.
    post(client, url, {"batch_id": batch, "start_ts": NOW - 3 * DAY, "end_ts": NOW - DAY})
    other = post(client, "/api/bounce/batches", {"brand": "Penn", "model": "Tour", "n_balls": 3})
    post(client, url, {"batch_id": other["id"], "start_ts": NOW - 4 * DAY})

    patch = f"{url}/{first['id']}"
    assert "overlaps" in error_of(client.patch(patch, json={"end_ts": NOW - 2 * DAY}), 409)
    assert "overlaps" in error_of(client.patch(patch, json={"end_ts": None}), 409)
    assert "end after" in error_of(client.patch(patch, json={"start_ts": NOW - 2 * DAY}))
    assert "unknown field" in error_of(client.patch(patch, json={"batch_id": other["id"]}))
    assert client.patch(patch, json={"start_ts": NOW - 6 * DAY}).status_code == 200
    # Ending the running one now (no time given means now).
    resp = client.patch(f"{url}/{running['id']}", json={"end_ts": NOW})
    assert resp.get_json()["end_ts"] == NOW


def test_a_session_entered_after_the_canister_was_opened_comes_back_ended(seeded, app):
    client, setup, batch = seeded
    url = "/api/charge/sessions"
    add_test(client, batch, setup, NOW - 9 * DAY, 110.0, "new")
    add_readings(app, NOW - 6 * DAY, NOW - 4 * DAY, 30.0)
    opened = NOW - 4 * DAY + 60
    add_readings(app, opened, opened + 30, 0.0, every=10.0)  # opened, then the sensor goes quiet
    add_readings(app, NOW - 2 * DAY, NOW - 600, 30.0)  # pumped up again, still in

    first = post(client, url, {"batch_id": batch, "mac": MAC1, "start_ts": NOW - 6 * DAY})
    assert first["end_ts"] == opened
    # So the next stay doesn't overlap it, and is still running.
    second = post(client, url, {"batch_id": batch, "mac": MAC1, "start_ts": NOW - 2 * DAY})
    assert second["end_ts"] is None
    _, b = overview(client, batch)
    assert b["in_canister"] and [s["end_ts"] for s in b["sessions"]] == [opened, None]

    # The app missed an opening: the end can be set to any time since the balls went in.
    patch = f"{url}/{second['id']}"
    came_out = NOW - 5 * HOUR
    assert client.patch(patch, json={"end_ts": came_out}).get_json()["end_ts"] == came_out
    assert not overview(client, batch)[1]["in_canister"]
    assert "end after" in error_of(client.patch(patch, json={"end_ts": NOW - 3 * DAY}))

    # Reopened by hand, the first stays open: old readings aren't looked at again.
    assert "overlaps" in error_of(client.patch(f"{url}/{first['id']}", json={"end_ts": None}), 409)
    client.delete(patch)
    reopened = client.patch(f"{url}/{first['id']}", json={"end_ts": None, "note": "still in"})
    assert reopened.get_json()["end_ts"] is None
    # Moving its start checks the readings again; a note alone doesn't.
    assert client.patch(f"{url}/{first['id']}", json={"note": "x"}).get_json()["end_ts"] is None
    moved = client.patch(f"{url}/{first['id']}", json={"start_ts": NOW - 5 * DAY})
    assert moved.get_json()["end_ts"] == opened
    # A session with no sensor, or one starting after the opening, is left running.
    client.delete(f"{url}/{first['id']}")
    assert post(client, url, {"batch_id": batch, "start_ts": NOW - 6 * DAY})["end_ts"] is None


def test_play_validation(seeded):
    client, _, batch = seeded
    for body, message in [
        ({}, "missing field(s): batch_id"),
        ({"batch_id": 999}, "batch 999 not found"),
        ({"batch_id": batch, "hours": 0}, "hours must be above 0"),
        ({"batch_id": batch, "hours": 30}, "hours must be between 0 and 24"),
        ({"batch_id": batch, "hours": "two"}, "hours must be a number"),
        ({"batch_id": batch, "ts": NOW + DAY}, "ts is in the future"),
        ({"batch_id": batch, "duration": 2}, "unknown field(s): duration"),
    ]:
        assert message in error_of(client.post("/api/charge/plays", json=body)), body


def test_bounce_overview_shows_where_a_session_put_the_batch(seeded):
    client, _, batch = seeded
    s = post(
        client, "/api/charge/sessions", {"batch_id": batch, "mac": MAC1, "start_ts": NOW - HOUR}
    )

    def location():
        [b] = client.get("/api/bounce").get_json()["batches"]
        return b["location"]

    assert location() == {"in_canister": True, "mac": MAC1}
    client.patch(f"/api/charge/sessions/{s['id']}", json={"end_ts": NOW})
    assert location() == {"in_canister": False, "mac": MAC1}


def test_deleting_a_batch_removes_it_from_the_overview(seeded):
    client, _, batch = seeded
    post(client, "/api/charge/sessions", {"batch_id": batch, "mac": MAC1})
    post(client, "/api/charge/plays", {"batch_id": batch, "hours": 1})
    assert client.delete(f"/api/bounce/batches/{batch}").status_code == 204
    assert client.get("/api/charge").get_json()["batches"] == []
