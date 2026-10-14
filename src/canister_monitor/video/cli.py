"""canister-video: measure bounce-test drops from a video (PLAN.md section 6.3).

    canister-video CLIP --drop-height 72in --mid-mark 36in --dry-run

Start the clip with the three calibration holds (ball still for ~2 s on the
floor, at the mid mark, at the drop mark), then drop each numbered ball in
order, the same number of times, catching it after its first bounce.

Uploading to the Pi and live webcams come in later steps; for now this
prints the drops it measured.
"""

import argparse
import csv
import json
import re
import statistics
import sys
from pathlib import Path

try:
    import cv2
except ImportError:  # e.g. on the Pi, where the video extra is never installed
    sys.exit('canister-video needs the laptop-only video extra: pip install -e ".[video]"')

from canister_monitor.video.detect import BallDetector, ColorRange, sample_color  # noqa: E402
from canister_monitor.video.pipeline import Result, analyze_clip, open_clip  # noqa: E402

CM_PER = {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": 2.54, '"': 2.54, "ft": 30.48, "'": 30.48}
LENGTH_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(cm|mm|m|in|\"|ft|')\s*$")


def parse_length(text: str) -> float:
    """'72in', '182.9cm', '6ft', '1.83m' -> cm. The unit is required."""
    m = LENGTH_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: give a number with a unit, e.g. 72in or 183cm")
    return float(m.group(1)) * CM_PER[m.group(2)]


def pick_ball(path: Path) -> tuple[ColorRange, tuple[float, float]] | None:
    """Show the clip; click the ball. Space skips ahead a second, Esc cancels."""
    _, frames = open_clip(path)
    clicked: list[tuple[int, int]] = []
    window = "Click the ball (space: skip 1 s, Esc: cancel)"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(
        window, lambda ev, x, y, *_: ev == cv2.EVENT_LBUTTONDOWN and clicked.append((x, y))
    )
    shown_at = None
    try:
        for t, frame in frames:
            if shown_at is not None and t - shown_at < 1.0:
                continue
            shown_at = t
            scale = min(1.0, 900 / frame.shape[0])
            cv2.imshow(window, cv2.resize(frame, None, fx=scale, fy=scale))
            while not clicked:
                key = cv2.waitKey(50) & 0xFF
                if key == 27:
                    return None
                if key == ord(" "):
                    break
            if clicked:
                x, y = clicked[0][0] / scale, clicked[0][1] / scale
                return ColorRange.around(sample_color(frame, x, y)), (x, y)
        return None
    finally:
        cv2.destroyWindow(window)


def fmt_in(cm: float | None) -> str:
    return "" if cm is None else f"{cm / 2.54:.2f}"


def print_result(result: Result, out=None) -> None:
    out = out or sys.stdout
    v = result.video
    seen = result.ball_found / result.frames if result.frames else 0
    print(
        f"{v.name}: {result.frames} frames, {v.width}x{v.height}, ball seen in {seen:.0%}", file=out
    )
    cal = result.calibration
    if cal is None:
        print("No calibration holds found: heights assume a level camera.", file=out)
    else:
        rows = ", ".join(f"{r:.1f}" for r in cal.rows)
        print(f"Calibrated ({cal.method}) from holds at rows {rows}", file=out)
    if not result.entries:
        print("No drops found.", file=out)
        return
    print(
        f"\n{'ball':>4} {'drop':>4} {'rebound in':>10} {'cm':>7} {'release in':>10}  warnings",
        file=out,
    )
    count: dict[int, int] = {}
    for ball, d in result.entries:
        count[ball] = count.get(ball, 0) + 1
        print(
            f"{ball:>4} {count[ball]:>4} {d.rebound_cm / 2.54:>10.2f} {d.rebound_cm:>7.1f} "
            f"{fmt_in(d.release_cm):>10}  {'; '.join(d.warnings)}",
            file=out,
        )
    print("\nPer ball (median):", file=out)
    by_ball: dict[int, list[float]] = {}
    for ball, d in result.entries:
        by_ball.setdefault(ball, []).append(d.rebound_cm)
    for ball, values in by_ball.items():
        m = statistics.median(values)
        print(f"  ball {ball}: {m / 2.54:.2f} in ({m:.1f} cm) from {len(values)} drops", file=out)


def write_debug(result: Result, trace: list, folder: Path) -> None:
    """Everything needed to see what the tool saw: a per-frame trace, the result, pictures."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "trace.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "found", "cx", "cy", "width", "height", "bottom"])
        for t, o in trace:
            if o is None:
                w.writerow([f"{t:.4f}", 0, "", "", "", "", ""])
            else:
                w.writerow(
                    [
                        f"{t:.4f}",
                        1,
                        f"{o.cx:.2f}",
                        f"{o.cy:.2f}",
                        f"{o.width:.1f}",
                        f"{o.height:.1f}",
                        f"{o.bottom:.3f}",
                    ]
                )
    (folder / "result.json").write_text(json.dumps(result.to_json(), indent=2))
    for seq, image in result.images.items():
        cv2.imwrite(str(folder / f"drop-{seq + 1:02d}.jpg"), image)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="canister-video", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("clip", type=Path, help="video file of a bounce test")
    ap.add_argument("--drop-height", type=parse_length, required=True, help="e.g. 72in")
    ap.add_argument("--mid-mark", type=parse_length, help="calibration mark between, e.g. 36in")
    ap.add_argument("--drops-per-ball", type=int, default=3)
    ap.add_argument("--pick", action="store_true", help="click the ball to teach its colour")
    ap.add_argument(
        "--debug", type=Path, metavar="DIR", help="write a trace, the result and drop pictures here"
    )
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument(
        "--dry-run", action="store_true", help="don't upload (uploading isn't built yet)"
    )
    args = ap.parse_args(argv)

    if not args.clip.is_file():
        ap.error(f"{args.clip} not found")
    if args.mid_mark is not None and not 0 < args.mid_mark < args.drop_height:
        ap.error("--mid-mark must be between the floor and the drop height")
    detector = BallDetector()
    if args.pick:
        picked = pick_ball(args.clip)
        if picked is None:
            print("No ball picked; using the default colour range.", file=sys.stderr)
        else:
            detector = BallDetector(color=picked[0], start_point=picked[1])
    trace: list = []
    try:
        result = analyze_clip(
            args.clip, args.drop_height, args.mid_mark, args.drops_per_ball, detector, trace
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.debug:
        write_debug(result, trace, args.debug)
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print_result(result)
        if not args.dry_run:
            print("\n(Uploading to the Pi isn't built yet: results shown only.)")
    return 0 if result.entries else 1


if __name__ == "__main__":
    sys.exit(main())
