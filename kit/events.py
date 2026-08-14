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
migrated to the new shape. Today: ``detection`` (yolo-detector).
"""
from __future__ import annotations

from typing import Any, Dict

__all__ = ["detection"]


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
