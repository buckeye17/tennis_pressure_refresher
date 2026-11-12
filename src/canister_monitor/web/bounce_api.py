"""JSON API for ball batches and bounce tests (PLAN.md section 6.2). Lengths are cm,
temperatures degC, times UTC epoch seconds; the browser converts for display."""

import base64
import binascii
import csv
import io
import json
import math
import re
import sqlite3
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from canister_monitor import db
from canister_monitor.bounce import CM_PER_IN, CONTEXTS, ITF_DROP_CM, outlier_json, summarize
from canister_monitor.web import get_db
from canister_monitor.web.api import ApiError, _ctx

bounce_api = Blueprint("bounce_api", __name__)

TEXT_MAX = 60
NOTE_MAX = 1000
MAX_CM = 500.0
MAX_BALLS = 500
MAX_DROPS = 2000
SAMPLE_ALL_UP_TO = 12  # test every ball in batches this small (PLAN.md 6.2)
DEFAULT_SAMPLE = 10


@bounce_api.errorhandler(ApiError)
def _api_error(e: ApiError):
    return jsonify({"error": e.message}), e.status


# --- body validation -----------------------------------------------------------------


def _body(allowed: set[str], required: set[str] = frozenset()) -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("request body must be a JSON object")
    unknown = set(body) - allowed
    if unknown:
        raise ApiError(f"unknown field(s): {', '.join(sorted(unknown))}")
    missing = {k for k in required if body.get(k) is None}
    if missing:
        raise ApiError(f"missing field(s): {', '.join(sorted(missing))}")
    return body


def _number(value: object, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(f"{name} must be a number")
    if not lo <= value <= hi:
        raise ApiError(f"{name} must be between {lo:g} and {hi:g}, got {value:g}")
    return float(value)


def _integer(value: object, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(f"{name} must be a whole number")
    if not lo <= value <= hi:
        raise ApiError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _text(value: object, name: str, max_len: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise ApiError(f"{name} must be text of at most {max_len} characters")
    value = value.strip()
    if not value and not optional:
        raise ApiError(f"{name} must not be empty")
    return value or None


def _ts(value: object, name: str = "ts") -> float:
    """A timestamp, defaulting to now."""
    if value is None:
        return _ctx()["clock"]()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(f"{name} must be a number (UTC epoch seconds)")
    return float(value)


def _setup(setup_id: object) -> db.BounceSetup:
    setup_id = _integer(setup_id, "setup_id", 1, 2**62)
    setup = next((s for s in db.list_setups(get_db()) if s.id == setup_id), None)
    if setup is None:
        raise ApiError(f"setup {setup_id} not found")
    return setup


def _batch(batch_id: object, status: int = 400) -> db.Batch:
    batch_id = _integer(batch_id, "batch_id", 1, 2**62)
    batch = db.get_batch(get_db(), batch_id)
    if batch is None:
        raise ApiError(f"batch {batch_id} not found", status)
    return batch


def _rebound(value: object, name: str, drop_cm: float) -> float:
    value = _number(value, name, 0.0, MAX_CM)
    if value <= 0 or value > drop_cm:
        raise ApiError(
            f"{name} must be above 0 and no higher than the drop height "
            f"({drop_cm:g} cm), got {value:g} cm: wrong units?"
        )
    return value


# --- everything at once -----------------------------------------------------------


def _video_json(v: db.VideoAnalysis | None) -> dict | None:
    """What the dashboard shows of a video test: the file, how it was calibrated, and
    every drop found (pictures from ``/api/bounce/video-drops/<id>.jpg``, apex close-ups
    from ``/api/bounce/video-drops/<id>/closeup.jpg``)."""
    if v is None:
        return None
    cal = v.video.get("calibration")
    return {
        "filename": v.filename,
        "calibration": cal.get("method") if isinstance(cal, dict) else None,
        "drops": [
            {
                "id": d.id,
                "seq": d.seq,
                "ball_no": d.ball_no,
                "rebound_cm": d.rebound_cm,
                "kept": d.kept,
                "edited_cm": d.edited_cm,
                "image": d.has_image,
                "closeup": d.has_closeup,
                "warnings": list(d.warnings),
            }
            for d in v.drops
        ],
    }


def _test_json(t: db.BounceTest, video: db.VideoAnalysis | None = None) -> dict:
    s = summarize((d.ball_no, d.rebound_cm) for d in t.drops)
    flags, fit = outlier_json([d.rebound_cm for d in t.drops])
    return {
        "id": t.id,
        "ts": t.ts,
        "setup_id": t.setup_id,
        "context": t.context,
        "temp_c": t.temp_c,
        "note": t.note,
        "source": t.source,
        "video": _video_json(video),
        "drops": [
            {"ball_no": d.ball_no, "rebound_cm": d.rebound_cm, "outlier_z": z}
            for d, z in zip(t.drops, flags, strict=True)
        ],
        "summary": {
            "balls": s.balls,
            "mean_cm": s.mean_cm,
            "sd_cm": s.sd_cm,
            "medians": [{"ball_no": b, "rebound_cm": m} for b, m in s.medians.items()],
            "drops_fit": fit,  # a normal distribution of the drops, outliers left out
        },
    }


@bounce_api.post("/api/bounce/outliers")
def check_outliers():
    """Flag drops that don't fit the others, for the test form as it's typed in.
    Nothing is stored. Body: {"rebounds_cm": [...]}, in any order."""
    values = _body({"rebounds_cm"}, {"rebounds_cm"})["rebounds_cm"]
    if not isinstance(values, list) or len(values) > MAX_DROPS:
        raise ApiError(f"rebounds_cm must be a list of up to {MAX_DROPS} numbers")
    cm = [_number(v, "rebounds_cm", 0.0, MAX_CM) for v in values]
    flags, fit = outlier_json(cm)
    return {"outlier_z": flags, "drops_fit": fit}


@bounce_api.get("/api/bounce")
def bounce_all():
    """Setups, batches with their tests, reference values and conversion pairs.

    Small enough to send in one go: a few batches with tens of tests each.
    """
    conn = get_db()
    cfg = _ctx()["config"].bounce
    tests: dict[int, list[dict]] = {}
    videos = db.list_video_analyses(conn)
    for t in db.list_tests(conn):
        tests.setdefault(t.batch_id, []).append(_test_json(t, videos.get(t.id)))
    locations = db.batch_locations(conn)
    batches = []
    for b in db.list_batches(conn):
        kind, mac = locations.get(b.id, (None, None))
        batches.append(
            {
                **b.__dict__,
                "location": None
                if kind is None
                else {"in_canister": kind == "balls_added", "mac": mac},
                "tests": tests.get(b.id, []),
            }
        )
    return jsonify(
        {
            "settings": {
                "display_units": cfg.display_units,
                "like_new_tolerance_pct": cfg.like_new_tolerance_pct,
                "itf_rebound_cm": [v * CM_PER_IN for v in cfg.itf_rebound_in],
                "itf_drop_cm": ITF_DROP_CM,
                "contexts": list(CONTEXTS),
            },
            "setups": [s.__dict__ for s in db.list_setups(conn)],
            "batches": batches,
            "references": [r.__dict__ for r in db.list_references(conn)],
            "conversions": [c.__dict__ for c in db.list_conversions(conn)],
        }
    )


# --- setups --------------------------------------------------------------------------


def _mid_mark(value: object, drop_cm: float) -> float | None:
    """The video calibration mark (PLAN.md 6.3): between the floor and the drop height."""
    if value is None:
        return None
    value = _number(value, "mid_mark_cm", 0.0, MAX_CM)
    if not 0 < value < drop_cm:
        raise ApiError(
            f"mid_mark_cm must be between the floor and the drop height ({drop_cm:g} cm), "
            f"got {value:g} cm: wrong units?"
        )
    return value


@bounce_api.post("/api/bounce/setups")
def create_setup():
    body = _body({"name", "drop_cm", "is_itf", "note", "mid_mark_cm"}, {"name", "drop_cm"})
    name = _text(body["name"], "name", TEXT_MAX)
    drop_cm = _number(body["drop_cm"], "drop_cm", 10.0, MAX_CM)
    is_itf = body.get("is_itf", False)
    if not isinstance(is_itf, bool):
        raise ApiError("is_itf must be true or false")
    if is_itf and abs(drop_cm - ITF_DROP_CM) > 0.5:
        raise ApiError(f"an ITF setup drops from {ITF_DROP_CM:g} cm (100 in)")
    note = _text(body.get("note"), "note", NOTE_MAX, optional=True)
    mid = _mid_mark(body.get("mid_mark_cm"), drop_cm)
    try:
        setup_id = db.insert_setup(get_db(), name, drop_cm, is_itf, note, mid)
    except sqlite3.IntegrityError:
        raise ApiError(f"a setup named {name!r} already exists", 409) from None
    return jsonify(db.BounceSetup(setup_id, name, drop_cm, is_itf, note, False, mid).__dict__), 201


@bounce_api.patch("/api/bounce/setups/<int:setup_id>")
def update_setup(setup_id: int):
    """Only the mid mark changes: the drop height and surface define the tests taken."""
    body = _body({"mid_mark_cm"})
    setup = next((s for s in db.list_setups(get_db()) if s.id == setup_id), None)
    if setup is None:
        raise ApiError(f"setup {setup_id} not found", 404)
    if "mid_mark_cm" in body:
        db.set_setup_mid_mark(get_db(), setup_id, _mid_mark(body["mid_mark_cm"], setup.drop_cm))
    return jsonify(next(s for s in db.list_setups(get_db()) if s.id == setup_id).__dict__)


@bounce_api.delete("/api/bounce/setups/<int:setup_id>")
def delete_setup(setup_id: int):
    try:
        deleted = db.delete_setup(get_db(), setup_id)
    except sqlite3.IntegrityError:
        raise ApiError("this setup has tests or values recorded with it", 409) from None
    if not deleted:
        raise ApiError(f"setup {setup_id} not found", 404)
    return "", 204


# --- batches -------------------------------------------------------------------------


def _batch_fields(body: dict, current: db.Batch | None) -> dict:
    """Validate batch fields present in ``body``; checks combinations against ``current``."""
    out = {}
    if "brand" in body:
        out["brand"] = _text(body["brand"], "brand", TEXT_MAX)
    if "model" in body:
        out["model"] = _text(body["model"], "model", TEXT_MAX)
    if "opened_ts" in body:
        out["opened_ts"] = _ts(body["opened_ts"], "opened_ts")
    if "n_balls" in body:
        out["n_balls"] = _integer(body["n_balls"], "n_balls", 1, MAX_BALLS)
    if "sample_size" in body:
        out["sample_size"] = _integer(body["sample_size"], "sample_size", 1, MAX_BALLS)
    if "reference_override_cm" in body:
        v = body["reference_override_cm"]
        out["reference_override_cm"] = (
            None if v is None else _number(v, "reference_override_cm", 1.0, MAX_CM)
        )
    if "retired_ts" in body:
        v = body["retired_ts"]
        out["retired_ts"] = None if v is None else _ts(v, "retired_ts")
    if "note" in body:
        out["note"] = _text(body["note"], "note", NOTE_MAX, optional=True)

    if current is None:  # creating: defaults
        out.setdefault("opened_ts", _ctx()["clock"]())
        n = out["n_balls"]
        out.setdefault("sample_size", n if n <= SAMPLE_ALL_UP_TO else DEFAULT_SAMPLE)
    merged = {**(current.__dict__ if current else {}), **out}
    if merged["sample_size"] > merged["n_balls"]:
        raise ApiError("sample_size can't be more than n_balls")
    if current is not None:
        highest = db.highest_ball_tested(get_db(), current.id)
        if highest is not None and merged["n_balls"] < highest:
            raise ApiError(f"ball {highest} has been tested, so n_balls must be at least {highest}")
    return out


@bounce_api.post("/api/bounce/batches")
def create_batch():
    fields = _batch_fields(_body(set(db.BATCH_FIELDS), {"brand", "model", "n_balls"}), None)
    batch_id = db.insert_batch(get_db(), **fields)
    return jsonify(db.get_batch(get_db(), batch_id).__dict__), 201


@bounce_api.patch("/api/bounce/batches/<int:batch_id>")
def update_batch(batch_id: int):
    current = _batch(batch_id, 404)
    fields = _batch_fields(_body(set(db.BATCH_FIELDS)), current)
    db.update_batch(get_db(), batch_id, **fields)
    return jsonify(db.get_batch(get_db(), batch_id).__dict__)


@bounce_api.delete("/api/bounce/batches/<int:batch_id>")
def delete_batch(batch_id: int):
    if not db.delete_batch(get_db(), batch_id):
        raise ApiError(f"batch {batch_id} not found", 404)
    return "", 204


# --- tests ---------------------------------------------------------------------------


TEST_FIELDS = {"ts", "batch_id", "setup_id", "context", "temp_c", "note", "drops"}


def _test_args(body: dict) -> dict:
    batch = _batch(body["batch_id"])
    setup = _setup(body["setup_id"])
    context = body["context"]
    if context not in CONTEXTS:
        raise ApiError(f"context must be one of {', '.join(CONTEXTS)}")
    temp = body.get("temp_c")
    raw = body["drops"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DROPS:
        raise ApiError(f"drops must be a list of 1-{MAX_DROPS} drops")
    drops = []
    for i, d in enumerate(raw, start=1):
        if not isinstance(d, dict) or set(d) != {"ball_no", "rebound_cm"}:
            raise ApiError(f"drop {i} must be {{ball_no, rebound_cm}}")
        drops.append(
            db.BounceDrop(
                _integer(d["ball_no"], f"drop {i} ball_no", 1, batch.n_balls),
                _rebound(d["rebound_cm"], f"drop {i} rebound_cm", setup.drop_cm),
            )
        )
    return {
        "ts": _ts(body.get("ts")),
        "batch_id": batch.id,
        "setup_id": setup.id,
        "context": context,
        "temp_c": None if temp is None else _number(temp, "temp_c", -30.0, 60.0),
        "note": _text(body.get("note"), "note", NOTE_MAX, optional=True),
        "drops": drops,
    }


def _saved_test(test_id: int) -> dict:
    t = next(t for t in db.list_tests(get_db()) if t.id == test_id)
    return _test_json(t, db.list_video_analyses(get_db()).get(test_id))


@bounce_api.post("/api/bounce/tests")
def create_test():
    args = _test_args(_body(TEST_FIELDS, {"batch_id", "setup_id", "context", "drops"}))
    test_id = db.insert_test(get_db(), **args)
    return jsonify(_saved_test(test_id)), 201


@bounce_api.put("/api/bounce/tests/<int:test_id>")
def update_test(test_id: int):
    args = _test_args(_body(TEST_FIELDS, {"batch_id", "setup_id", "context", "drops"}))
    if not db.replace_test(get_db(), test_id, **args):
        raise ApiError(f"test {test_id} not found", 404)
    return jsonify(_saved_test(test_id))


@bounce_api.delete("/api/bounce/tests/<int:test_id>")
def delete_test(test_id: int):
    if not db.delete_test(get_db(), test_id):
        raise ApiError(f"test {test_id} not found", 404)
    return "", 204


# --- video tests (PLAN.md 6.3) -----------------------------------------------------------

MAX_VIDEO_BYTES = 40 << 20  # the whole request: a long session's pictures, base64
MAX_VIDEO_DROPS = 500
MAX_IMAGE_BYTES = 512 << 10
MAX_JSON_BYTES = 256 << 10  # each of the analysis' video and params, and each drop's detail
SAVE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
VIDEO_DROP_FIELDS = {
    "seq",
    "ball_no",
    "rebound_cm",
    "kept",
    "edited_cm",
    "outlier_z",
    "detail",
    "image_jpeg",
    "closeup_jpeg",
}


def _json_object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ApiError(f"{name} must be a JSON object")
    if len(json.dumps(value)) > MAX_JSON_BYTES:
        raise ApiError(f"{name} is too large")
    return value


def _jpeg(value: object, name: str) -> bytes | None:
    if value is None:
        return None
    try:
        data = base64.b64decode(value, validate=True) if isinstance(value, str) else None
    except (binascii.Error, ValueError):
        data = None
    if data is None or data[:2] != b"\xff\xd8":
        raise ApiError(f"{name} must be a base64 JPEG")
    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(f"{name} is larger than {MAX_IMAGE_BYTES >> 10} KB")
    return data


def _video_drop(d: object, i: int, n_balls: int, drop_cm: float) -> db.VideoDrop:
    name = f"drop {i}"
    if not isinstance(d, dict):
        raise ApiError(f"{name} must be a JSON object")
    unknown = set(d) - VIDEO_DROP_FIELDS
    if unknown:
        raise ApiError(f"{name}: unknown field(s): {', '.join(sorted(unknown))}")
    kept = d.get("kept")
    if not isinstance(kept, bool):
        raise ApiError(f"{name}: kept must be true or false")
    measured = d.get("rebound_cm")
    edited = d.get("edited_cm")
    return db.VideoDrop(
        seq=_integer(d.get("seq"), f"{name} seq", 0, 10**7),
        # A rejected drop may sit on a ball beyond the batch (a stray catch); a kept one can't.
        ball_no=_integer(d.get("ball_no"), f"{name} ball_no", 1, n_balls if kept else MAX_BALLS),
        rebound_cm=None if measured is None else _number(measured, f"{name} rebound_cm", 0, MAX_CM),
        kept=kept,
        edited_cm=None if edited is None else _rebound(edited, f"{name} edited_cm", drop_cm),
        detail=_json_object(d.get("detail", {}), f"{name} detail"),
        image=_jpeg(d.get("image_jpeg"), f"{name} image_jpeg"),
        closeup=_jpeg(d.get("closeup_jpeg"), f"{name} closeup_jpeg"),
    )


@bounce_api.post("/api/bounce/video-tests")
def save_video_test():
    """A reviewed test from the laptop app, with its analysis and pictures, in one request:
    the document the app's review exports, plus ``key``.

    The test's drops are the kept drops at their reviewed heights (typed over, or
    as measured). Saving the same ``key`` again replaces the test it made (200)
    instead of adding another (201).
    """
    if (request.content_length or 0) > MAX_VIDEO_BYTES:
        raise ApiError(f"the request is larger than {MAX_VIDEO_BYTES >> 20} MB", 413)
    body = _body(
        {"key", "tool", "test", "set_up", "summary", "analysis", "drops"},
        {"key", "test", "analysis", "drops"},
    )
    key = body["key"]
    if not isinstance(key, str) or not SAVE_KEY_RE.match(key):
        raise ApiError("key must be 8-64 letters, digits, - or _")

    test = body["test"]
    if not isinstance(test, dict):
        raise ApiError("test must be a JSON object")
    unknown = set(test) - (TEST_FIELDS | {"source"})
    if unknown:
        raise ApiError(f"test: unknown field(s): {', '.join(sorted(unknown))}")
    if test.get("source", "video") != "video":
        raise ApiError("test source must be video")
    missing = {k for k in ("batch_id", "setup_id", "context") if test.get(k) is None}
    if missing:
        raise ApiError(f"test: missing field(s): {', '.join(sorted(missing))}")
    batch, setup = _batch(test["batch_id"]), _setup(test["setup_id"])

    raw = body["drops"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_VIDEO_DROPS:
        raise ApiError(f"drops must be a list of 1-{MAX_VIDEO_DROPS} drops")
    video_drops = [
        _video_drop(d, i, batch.n_balls, setup.drop_cm) for i, d in enumerate(raw, start=1)
    ]
    if len({d.seq for d in video_drops}) < len(video_drops):
        raise ApiError("drop seq numbers must be unique")
    kept = []
    for d in video_drops:
        if not d.kept:
            continue
        height = d.edited_cm if d.edited_cm is not None else d.rebound_cm
        if height is None:
            raise ApiError(f"drop seq {d.seq} is kept but has no height")
        _rebound(height, f"drop seq {d.seq} height", setup.drop_cm)
        kept.append({"ball_no": d.ball_no, "rebound_cm": height})
    if not kept:
        raise ApiError("no drops are kept: there's no test to save")
    # The test's own list, if sent, must be what review left (catches a stale export).
    if "drops" in test and test["drops"] != kept:
        raise ApiError("test drops don't match the kept drops at their reviewed heights")

    args = _test_args({**{k: v for k, v in test.items() if k != "source"}, "drops": kept})
    analysis = body["analysis"]
    if not isinstance(analysis, dict) or set(analysis) - {"filename", "video", "params"}:
        raise ApiError("analysis must be {filename, video, params}")
    test_id, created = db.save_video_test(
        get_db(),
        key,
        **args,
        filename=_text(analysis.get("filename"), "analysis filename", 255),
        video=_json_object(analysis.get("video", {}), "analysis video"),
        params=_json_object(analysis.get("params", {}), "analysis params"),
        video_drops=video_drops,
    )
    return jsonify(_saved_test(test_id)), 201 if created else 200


@bounce_api.get("/api/bounce/video-drops/<int:drop_id>.jpg")
@bounce_api.get("/api/bounce/video-drops/<int:drop_id>/closeup.jpg", defaults={"closeup": True})
def video_drop_image(drop_id: int, closeup: bool = False):
    jpeg = db.video_drop_image(get_db(), drop_id, closeup)
    if jpeg is None:
        what = "apex close-up" if closeup else "picture"
        raise ApiError(f"no {what} for video drop {drop_id}", 404)
    # A drop's picture never changes: saving a review again makes new drop ids.
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# --- reference values and ITF conversion ----------------------------------------------


@bounce_api.post("/api/bounce/references")
def set_reference():
    """Create or replace the new-ball rebound for a brand, model and setup."""
    fields = {"brand", "model", "setup_id", "rebound_cm"}
    body = _body(fields, fields)
    brand = _text(body["brand"], "brand", TEXT_MAX)
    model = _text(body["model"], "model", TEXT_MAX)
    setup = _setup(body["setup_id"])
    rebound = _rebound(body["rebound_cm"], "rebound_cm", setup.drop_cm)
    ref_id = db.upsert_reference(get_db(), brand, model, setup.id, rebound)
    ref = next(r for r in db.list_references(get_db()) if r.id == ref_id)
    return jsonify(ref.__dict__), 201


@bounce_api.delete("/api/bounce/references/<int:ref_id>")
def delete_reference(ref_id: int):
    if not db.delete_reference(get_db(), ref_id):
        raise ApiError(f"reference {ref_id} not found", 404)
    return "", 204


@bounce_api.post("/api/bounce/conversions")
def create_conversion():
    """One ball, dropped the user's way and the ITF way on the same day."""
    body = _body(
        {"ts", "setup_id", "rebound_cm", "itf_rebound_cm", "note"},
        {"setup_id", "rebound_cm", "itf_rebound_cm"},
    )
    setup = _setup(body["setup_id"])
    if setup.is_itf:
        raise ApiError("this setup already is the ITF method; no conversion needed")
    args = {
        "ts": _ts(body.get("ts")),
        "setup_id": setup.id,
        "rebound_cm": _rebound(body["rebound_cm"], "rebound_cm", setup.drop_cm),
        "itf_rebound_cm": _rebound(body["itf_rebound_cm"], "itf_rebound_cm", ITF_DROP_CM),
        "note": _text(body.get("note"), "note", NOTE_MAX, optional=True),
    }
    conv_id = db.insert_conversion(get_db(), **args)
    return jsonify(db.Conversion(conv_id, **args).__dict__), 201


@bounce_api.delete("/api/bounce/conversions/<int:conv_id>")
def delete_conversion(conv_id: int):
    if not db.delete_conversion(get_db(), conv_id):
        raise ApiError(f"conversion {conv_id} not found", 404)
    return "", 204


# --- export ----------------------------------------------------------------------------


CSV_COLUMNS = [
    "local_time",
    "utc_epoch",
    "test_id",
    "batch_id",
    "brand",
    "model",
    "setup",
    "drop_cm",
    "context",
    "temp_c",
    "ball_no",
    "rebound_cm",
    "rebound_in",
    "note",
    "outlier_z",  # blank unless the drop doesn't fit its test's others (bounce.fit_normal)
    "source",  # manual | video
]


@bounce_api.get("/api/bounce.csv")
def export_bounce_csv():
    """Every drop of every test, one row each."""
    conn = get_db()
    batches = {b.id: b for b in db.list_batches(conn)}
    setups = {s.id: s for s in db.list_setups(conn)}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for t in db.list_tests(conn):
        b, s = batches[t.batch_id], setups[t.setup_id]
        flags, _ = outlier_json([d.rebound_cm for d in t.drops])
        for d, z in zip(t.drops, flags, strict=True):
            writer.writerow(
                [
                    datetime.fromtimestamp(t.ts).astimezone().isoformat(timespec="seconds"),
                    f"{t.ts:.0f}",
                    t.id,
                    b.id,
                    b.brand,
                    b.model,
                    s.name,
                    f"{s.drop_cm:.1f}",
                    t.context,
                    "" if t.temp_c is None else f"{t.temp_c:.1f}",
                    d.ball_no,
                    f"{d.rebound_cm:.1f}",
                    f"{d.rebound_cm / CM_PER_IN:.2f}",
                    t.note or "",
                    "" if z is None else f"{z:.2f}",
                    t.source,
                ]
            )
    stamp = datetime.fromtimestamp(_ctx()["clock"]()).strftime("%Y%m%d-%H%M")
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="bounce-{stamp}.csv"'},
    )
