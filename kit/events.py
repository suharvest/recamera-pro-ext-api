"""
kit.events -- mechanical result->event converters (KIT_APP_SHAPE_SPEC §5.3).

Imported as ``from kit import events as E`` and used inside an app's ``run()``:

    self.emit([E.detection(d) for d in dets], frame.pts, results=dets)

**Only mechanical transforms live here**: renaming/copying fields, ``round``,
normalised<->pixel conversion, filling in ``kind``. Business semantics -- what
counts as an event, which threshold fires it, cross-frame state -- stay in the
app. A helper here must never decide a threshold, hold state, or gate whether
an event is produced.

This module is deliberately grown one converter at a time, as each app is
migrated to the new shape. Today: ``detection`` (yolo-detector), ``track`` and
``metrics`` (retail-vision), ``text`` (ppocr-reader).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict

__all__ = ["detection", "text", "track", "metrics"]


def detection(d: Dict[str, Any]) -> Dict[str, Any]:
    """One detect() result dict -> one flat, overlay-friendly ``detection`` event.

    Field-for-field identical to the hand-written mapping yolo-detector carried
    in ``on_results()`` before the migration:

        {"kind": "detection", "label": <cls_name>, "cls": <cls>,
         "score": <score>, "box": <box>}

    ``box`` is passed through untouched -- post-processing has already
    un-letterboxed it into ORIGINAL-frame xyxy pixels.
    """
    return {
        "kind": "detection",
        "label": d["cls_name"],
        "cls": d["cls"],
        "score": d["score"],
        "box": d["box"],
    }


def text(box: Dict[str, Any], *, text: str, rec_conf: float) -> Dict[str, Any]:
    """One recognized text box -> one flat, overlay-friendly ``text`` event.

    Field-for-field identical to the hand-written mapping ppocr-reader carried
    in ``on_results()`` before the migration:

        {"kind": "text", "box": ..., "quad": ..., "text": ...,
         "score": <detection score>, "rec_conf": <round(conf, 4)>}

    What this function does -- all mechanical (spec §5.3):

      * copies ``box`` / ``quad`` / ``score`` off the stage-1 result dict
        (already un-letterboxed into ORIGINAL-frame pixels by db_ocr.decode),
      * rounds the recognition confidence to 4 dp,
      * fills in ``kind``.

    What it does NOT do -- the caller passes these in, because they are the
    app's business decisions:

      * the perspective crop and which recognizer ran,
      * the reading order the boxes arrive in,
      * ``text`` itself -- in particular, whether a low-confidence reading is
        blanked out. This helper never compares ``rec_conf`` against anything;
        the app decides that and hands over the string it wants published.
    """
    return {
        "kind": "text",
        "box": box["box"],
        "quad": box["quad"],
        "text": text,
        "score": box["score"],
        "rec_conf": round(float(rec_conf), 4),
    }


def track(tr, frame, *, state, in_zone: bool) -> Dict[str, Any]:
    """One ``kit.logic.tracker.Track`` -> one flat, overlay-friendly ``track`` event.

    Purely mechanical (spec §5.3). What this function does:

      * de-normalises the track's centre/size (``cx``/``cy``/``w``/``h`` are in
        [0,1]) into ORIGINAL-frame xyxy pixels using ``frame.w``/``frame.h``,
      * rounds -- box to 0.1 px, score to 3 dp, speed to 0.1 px/s,
      * copies ``track_id`` off the Track and fills in ``kind``.

    What it does NOT do -- the caller passes these in, because they are business
    decisions the app owns:

      * ``state``    -- the dwell state machine's verdict for this track,
      * ``in_zone``  -- whether the counting-zone polygon contains this track.

    It holds no state, applies no threshold, and never decides whether an event
    is emitted -- the app calls it once per track it has already decided to
    report.
    """
    x1 = (tr.cx - tr.w / 2) * frame.w
    y1 = (tr.cy - tr.h / 2) * frame.h
    x2 = (tr.cx + tr.w / 2) * frame.w
    y2 = (tr.cy + tr.h / 2) * frame.h
    return {
        "kind": "track",
        "track_id": tr.track_id,
        "state": state,
        "in_zone": bool(in_zone),
        "score": round(tr.score, 3),
        "speed_px_s": round(tr.speed_px_s, 1),
        "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
    }


def metrics(snapshot, **extra) -> Dict[str, Any]:
    """A metrics dataclass (e.g. ``zones.WindowSnapshot``) -> a ``metrics`` event.

    ``dataclasses.asdict`` + ``kind``, nothing else: every field of the snapshot
    is copied out under its own name. `extra` merges in additional flat fields
    the app wants alongside them.

    The app decides WHAT the snapshot contains and WHEN to publish it (which
    frames feed the rolling window, what counts as occupancy); this helper only
    flattens the object it is handed.
    """
    if dataclasses.is_dataclass(snapshot) and not isinstance(snapshot, type):
        fields = dataclasses.asdict(snapshot)
    else:
        fields = dict(snapshot)
    return {"kind": "metrics", **fields, **extra}
