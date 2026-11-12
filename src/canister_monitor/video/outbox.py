"""Reviewed tests the Pi couldn't take yet, kept beside the recordings to send later.

Each is the document Save to Pi sends, in ``recordings/<video name>.unsent.json``.
Once the Pi has it, the file is renamed ``.sent.json`` rather than deleted, so the
review stays on the laptop with its video.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

UNSENT = ".unsent.json"
SENT = ".sent.json"


def path_for(recordings: Path, video: Path | None, fallback: str) -> Path:
    """Where a session's unsent test goes: named after its video, so saving the same
    session again overwrites it."""
    stem = video.stem if video is not None else fallback
    return recordings / f"{stem}{UNSENT}"


def keep(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(path)


def pending(recordings: Path) -> list[tuple[Path, dict]]:
    """Unsent tests, oldest first. Unreadable files are skipped (and logged)."""
    out = []
    for path in sorted(recordings.glob(f"*{UNSENT}")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("skipping %s: %s", path.name, e)
            continue
        if isinstance(doc, dict):
            out.append((path, doc))
    return out


def mark_sent(path: Path) -> None:
    path.replace(path.with_name(path.name[: -len(UNSENT)] + SENT))


def mark_sent_by_key(recordings: Path, key: str) -> None:
    """The Pi has this review now: an older copy waiting to be sent is done too."""
    for path, doc in pending(recordings):
        if doc.get("key") == key:
            mark_sent(path)


def summary(path: Path, doc: dict) -> dict:
    """What the page lists for an unsent test."""
    test = doc.get("test") or {}
    set_up = doc.get("set_up") or {}
    return {
        "file": path.name,
        "key": doc.get("key"),
        "ts": test.get("ts"),
        "batch": set_up.get("batch_label"),
        "setup": set_up.get("setup_name"),
        "context": test.get("context"),
        "drops": len(test.get("drops") or []),
    }
