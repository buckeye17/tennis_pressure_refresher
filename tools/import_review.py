"""Send a review downloaded from Ball Bounce Tracker (Download JSON) to the Pi.

For sessions measured while the Pi was away, or before Save to Pi existed: such a
file doesn't say which batch, setup and stage it was, so you give them here.

    uv run python tools/import_review.py --list
    uv run python tools/import_review.py REVIEW.json --batch 2 --setup 1 --context before \\
        [--temp 68F] [--note "garage"] [--dry-run]

The test keeps the session's own time, drops, review edits and pictures. Importing
the same file again updates the test it made instead of adding another.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from canister_monitor.video.pi import PiClient, PiError  # noqa: E402

DEFAULT_PI = "http://canisters.local:5000"
CM_PER_IN = 2.54
DROP_TOLERANCE_CM = 0.5  # the setup's drop height must be the one the session used


class ReviewError(Exception):
    pass


def parse_temp(text: str) -> float:
    """'68F', '20C' or '20.5 c' -> degC. The unit is required: 20 could be either."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CcFf])\s*", text)
    if not m:
        raise argparse.ArgumentTypeError("give the unit, e.g. 68F or 20C")
    v = float(m.group(1))
    return (v - 32) * 5 / 9 if m.group(2) in "Ff" else v


def save_key(doc: dict) -> str:
    """The review's own key, or one made from its video and start time, so importing
    the same file twice replaces the first import."""
    if isinstance(doc.get("key"), str):
        return doc["key"]
    seed = f"{doc['analysis'].get('filename')}|{doc['test'].get('ts')}"
    return "import-" + hashlib.sha256(seed.encode()).hexdigest()[:24]


def prepare(
    doc: dict,
    bounce: dict,
    batch_id: int,
    setup_id: int,
    context: str,
    temp_c: float | None = None,
    note: str | None = None,
) -> dict:
    """The document to send: ``doc`` with its batch, setup and stage filled in.

    ``bounce`` is the Pi's ``GET /api/bounce``. Raises ReviewError for anything the
    Pi would refuse, with a message for the user.
    """
    if not all(isinstance(doc.get(k), dict) for k in ("test", "analysis", "set_up")) or (
        not isinstance(doc.get("drops"), list)
    ):
        raise ReviewError("this isn't a review from Ball Bounce Tracker's Download JSON")
    batch = next((b for b in bounce["batches"] if b["id"] == batch_id), None)
    if batch is None:
        raise ReviewError(f"the Pi has no batch {batch_id} (see --list)")
    setup = next((s for s in bounce["setups"] if s["id"] == setup_id), None)
    if setup is None:
        raise ReviewError(f"the Pi has no setup {setup_id} (see --list)")
    contexts = bounce.get("settings", {}).get("contexts") or []
    if context not in contexts:
        raise ReviewError(f"--context must be one of {', '.join(contexts)}")
    measured = doc["set_up"].get("drop_height_cm")
    if measured is None or abs(measured - setup["drop_cm"]) > DROP_TOLERANCE_CM:
        raise ReviewError(
            f"the session was measured from {measured} cm, but setup {setup_id} "
            f"({setup['name']}) drops from {setup['drop_cm']:g} cm"
        )
    kept = [d for d in doc["drops"] if d.get("kept")]
    if not kept:
        raise ReviewError("no drops are kept in this review")
    highest = max(d["ball_no"] for d in kept)
    if highest > batch["n_balls"]:
        raise ReviewError(
            f"the review has ball {highest}, but batch {batch_id} has {batch['n_balls']} balls"
        )

    out = json.loads(json.dumps(doc))  # a copy
    out["key"] = save_key(doc)
    out["test"].update(
        batch_id=batch_id,
        setup_id=setup_id,
        context=context,
        temp_c=temp_c,
        note=(note or "").strip() or None,
    )
    out["set_up"].update(
        batch_id=batch_id,
        batch_label=f"{batch['brand']} {batch['model']}",
        sample_size=batch["sample_size"],
        setup_id=setup_id,
        setup_name=setup["name"],
        context=context,
    )
    return out


def describe(doc: dict) -> str:
    t, s = doc["test"], doc.get("summary") or {}
    kept = sum(1 for d in doc["drops"] if d.get("kept"))
    when = datetime.fromtimestamp(t["ts"]).strftime("%Y-%m-%d %H:%M") if t.get("ts") else "?"
    mean = s.get("mean_cm")
    avg = f", average {mean:.1f} cm ({mean / CM_PER_IN:.2f} in)" if mean else ""
    return (
        f"{when}, {doc['analysis'].get('filename')}: {kept} of {len(doc['drops'])} drops kept, "
        f"{s.get('balls', '?')} balls{avg}"
    )


def list_pi(bounce: dict) -> None:
    print("Batches:")
    for b in bounce["batches"]:
        retired = " (retired)" if b.get("retired_ts") else ""
        opened = datetime.fromtimestamp(b["opened_ts"]).strftime("%Y-%m-%d")
        print(f"  {b['id']:>3}  {b['brand']} {b['model']}, opened {opened}, "
              f"{b['n_balls']} balls, {len(b.get('tests', []))} tests{retired}")  # fmt: skip
    print("Setups:")
    for s in bounce["setups"]:
        mid = s.get("mid_mark_cm")
        mid_text = f", mid mark {mid:g} cm" if mid is not None else ""
        print(f"  {s['id']:>3}  {s['name']}: drops from {s['drop_cm']:g} cm{mid_text}")
    print("Stages:", ", ".join(bounce.get("settings", {}).get("contexts", [])))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("review", nargs="?", type=Path, help="a review JSON from Download JSON")
    p.add_argument("--pi", default=DEFAULT_PI, help=f"the Pi's dashboard (default {DEFAULT_PI})")
    p.add_argument("--list", action="store_true", help="list the Pi's batches and setups")
    p.add_argument("--batch", type=int, help="batch id (see --list)")
    p.add_argument("--setup", type=int, help="setup id (see --list)")
    p.add_argument("--context", help="stage: new, before, after, after_play or other")
    p.add_argument("--temp", type=parse_temp, help="room temperature, e.g. 68F or 20C")
    p.add_argument("--note", help="a note for the test")
    p.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    args = p.parse_args(argv)

    pi = PiClient(args.pi, timeout=10.0)
    try:
        bounce = pi.bounce()
    except PiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if bounce["setups"] and "mid_mark_cm" not in bounce["setups"][0]:
        print("error: the Pi runs older code without video tests: update it first", file=sys.stderr)
        return 1
    if args.list or args.review is None:
        list_pi(bounce)
        return 0
    missing = [f"--{k}" for k in ("batch", "setup", "context") if getattr(args, k) is None]
    if missing:
        p.error(f"{', '.join(missing)} needed (see --list)")

    try:
        doc = json.loads(args.review.read_text(encoding="utf-8"))
        out = prepare(doc, bounce, args.batch, args.setup, args.context, args.temp, args.note)
    except (OSError, ValueError) as e:
        print(f"error: can't read {args.review}: {e}", file=sys.stderr)
        return 1
    except ReviewError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(describe(out))
    s = out["set_up"]
    temp = "no room temperature" if args.temp is None else f"{args.temp:.1f} °C"
    print(
        f"  -> {s['batch_label']} (batch {args.batch}), {s['setup_name']}, {args.context}, {temp}"
    )
    if args.temp is None:
        print("  (rebound depends on temperature: add --temp if you know it)")
    if args.dry_run:
        print("Dry run: nothing sent.")
        return 0
    try:
        status, test = pi.save_video_test(out)
    except PiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{'Saved' if status == 201 else 'Updated'} on the Pi as test #{test['id']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
