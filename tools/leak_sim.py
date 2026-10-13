"""Check leak calibration on simulated leak-test protocols (PLAN.md section 6.1).

Draws seal leak rates from a known population, simulates each test through the
real encoder/decoder with weather and temperature drift, fits the calibration,
and reports how well it recovers the truth and predicts a new session.

    python tools/leak_sim.py --protocol 50:168,20:72,20:72,20:72,50:72,50:72
    python tools/leak_sim.py --tau 0.1 --trials 50     # repeatable seals
    python tools/leak_sim.py --barometer               # weather measured and removed

Each protocol entry is fill psi : hours. Leak rates are fractions of gauge
pressure per day at 30 psi (0.005 = 0.15 psi/day at 30 psi).
"""

import argparse
import math
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from canister_monitor import leak, simulate  # noqa: E402
from canister_monitor.compensation import compensated_gauge_kpa, kpa_to_psi  # noqa: E402

ATM = 101.325


def parse_protocol(text: str) -> list[tuple[float, float]]:
    return [(float(p), float(h)) for p, h in (item.split(":") for item in text.split(","))]


def run_session(test, hours, rng, room, start_ts) -> leak.SessionFit | None:
    rows = simulate.leak_test_readings(test, start_ts, hours * 3600, rng, room, ATM)
    ts = [r[0] for r in rows]
    gauge = [compensated_gauge_kpa(r[1], r[2], ATM, simulate.REFERENCE_TEMP_C) for r in rows]
    return leak.fit_session(
        ts, gauge, start_ts, start_ts + hours * 3600 + 1, weather_sd_kpa=room.weather_sd_kpa
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", default="50:168,20:72,20:72,20:72,50:72,50:72")
    ap.add_argument("--rate", type=float, default=0.005, help="typical rate per day at 30 psi")
    ap.add_argument("--tau", type=float, default=0.4, help="seal spread, sd of ln(rate)")
    ap.add_argument("--beta", type=float, default=-0.3, help="pressure exponent of the rate")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--barometer", action="store_true", help="weather drift measured and removed")
    args = ap.parse_args(argv)

    protocol = parse_protocol(args.protocol)
    room = simulate.Room(weather_sd_kpa=0.0) if args.barometer else simulate.Room()
    mu = math.log(args.rate)
    rng = random.Random(args.seed)
    t_start = time.time()

    mus, betas, taus, fitted_beta = [], [], [], 0
    covered = {20.0: 0, 50.0: 0}
    pred_err_psi = {20.0: [], 50.0: []}
    for trial in range(args.trials):
        fits = []
        for i, (psi, hours) in enumerate(protocol):
            rate = math.exp(mu + args.tau * rng.gauss(0, 1))
            test = simulate.LeakTest(psi, rate, beta=args.beta)
            fit = run_session(test, hours, rng, room, 1.7e9 + (trial * 20 + i) * 30 * 86400)
            if fit is not None:
                fits.append(fit)
        cal = leak.fit_calibration(fits)
        if cal is None:
            continue
        mus.append(cal.mu)
        betas.append(cal.beta)
        taus.append(cal.tau)
        fitted_beta += cal.beta_fitted
        # A new session at each mode: is its true rate inside the predicted +-1 sd range?
        for psi in covered:
            true_rate = math.exp(mu + args.tau * rng.gauss(0, 1)) * (psi / 30) ** args.beta
            gauge = psi * 6.894757 * 0.97  # roughly the session mean
            lo, hi = cal.rate_range(gauge, 1.0)
            covered[psi] += lo <= true_rate <= hi
            pred_err_psi[psi].append(kpa_to_psi((cal.rate_per_day(gauge) - true_rate) * gauge))

    n = len(mus)
    conditions = "barometer" if args.barometer else "weather + temp drift"
    print(f"protocol {args.protocol}  ({conditions})")
    print(
        f"truth    rate {args.rate:.4f}/day at 30 psi   tau {args.tau:.2f}   beta {args.beta:+.2f}"
    )
    print(
        f"fitted   rate {math.exp(statistics.median(mus)):.4f} (median)"
        f"   mu sd {statistics.pstdev(mus):.2f}"
    )
    print(f"         tau median {statistics.median(taus):.2f}  [{min(taus):.2f}-{max(taus):.2f}]")
    print(
        f"         beta median {statistics.median(betas):+.2f}  sd {statistics.pstdev(betas):.2f}"
        f"  (fitted in {fitted_beta}/{n})"
    )
    for psi in covered:
        errs = pred_err_psi[psi]
        rms = math.sqrt(sum(e * e for e in errs) / len(errs))
        print(
            f"new {psi:.0f} psi session: inside +-1 sd range {covered[psi]}/{n}"
            f" (expect ~{0.68 * n:.0f}); leak prediction error {rms:.3f} psi/day rms"
        )
    print(f"{n} trials in {time.time() - t_start:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
