#!/usr/bin/env python3
"""
retail-vision -- reCamera Pro retail people-counting app (port of the first-gen
SSCMA solution). Thin: every shared stage (frame grab / letterbox / RKNN infer /
detect post-process / WS publish) lives in kit.app.App; the reusable tracking and
counting live in kit.logic.{tracker,zones}. This file only:
  * declares model + detect post-processor (mirrors manifest.json),
  * setup(): reads detection / zone / line / dwell / window params, and
  * on_results(): person filter -> Tracker.update -> Dwell + Zone + Line
    counting -> emit per-track events, line-crossing events and a metrics event.

Config for the counting zone / entry line is not carried on appmgr's minimal
launcher CLI, so (like fall-detection reads its own thresholds) this app reads:
  1. manifest.json config_schema defaults,
  2. an optional JSON override file named by env RETAIL_CONFIG (or config.json
     next to app.py), e.g. {"entry_line": {"a":[0.5,0],"b":[0.5,1],"in":"right"},
     "count_zone": [[0.2,0.2],[0.8,0.2],[0.8,0.8],[0.2,0.8]]}.
Points are normalised [0,1]. With no line configured, entry/exit fall back to
appearance/disappearance counting (new track = entry, aged-out track = exit).

Run on device (inference requires root):
    KIT=/userdata/local/kit
    PYTHONPATH=$KIT python3 app.py \
        --model models/yolo8n_rawhead_int8.rknn --sink ws --port 8124
"""
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_kit_parent_env = os.environ.get("KIT_PARENT")
_kit_dir_env = os.environ.get("KIT_DIR")
for _cand in (
    _kit_parent_env,
    os.path.dirname(_kit_dir_env) if _kit_dir_env else None,
    "/userdata/local",                               # device: kit at /userdata/local/kit
    os.path.join(_here, ".."),                       # device: /userdata/local/apps
    os.path.join(_here, "..", ".."),                 # repo: recamera_pro/
    "/userdata/local/apps",
):
    if _cand and os.path.isdir(os.path.join(_cand, "kit")):
        _cand = os.path.abspath(_cand)
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from kit.app import App, run_app                                    # noqa: E402
from kit.logic.tracker import Tracker, TrackerConfig                # noqa: E402
from kit.logic.zones import (                                       # noqa: E402
    ZoneCounter, LineCounter, Dwell, DwellConfig, RollingWindow,
    StateCount, BROWSING, ENGAGED, ASSISTANCE,
)

PERSON_CLS = 0  # COCO 'person'


class RetailVisionApp(App):
    id = "retail-vision"
    name = "Retail People Counting"
    postproc = "detect"

    def setup(self, config):
        super().setup(config)
        # `config` is the effective config from kit.config (manifest defaults
        # overlaid by <app_dir>/config.json). Numeric params AND the spatial
        # count_zone / entry_line controls now come from this ONE source -- the
        # old RETAIL_CONFIG env / separate spatial file is gone.
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # Spatial controls live under the same config keys the /appcenter panel
        # edits (config_schema types "zone" / "line").
        spatial = {
            "count_zone": params.get("count_zone"),
            "entry_line": params.get("entry_line"),
        }

        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))

        self.tracker = Tracker(TrackerConfig())

        self.dwell = Dwell(DwellConfig(
            speed_threshold=float(params.get("dwell_speed", 10.0)),
            engaged_sec=float(params.get("dwell_engaged", 1.5)),
            assistance_sec=float(params.get("dwell_assist", 20.0)),
        ))

        zone_poly = spatial.get("count_zone")
        self.zone = ZoneCounter(zone_poly)

        self.line = LineCounter()
        line_cfg = spatial.get("entry_line")
        if line_cfg and "a" in line_cfg and "b" in line_cfg:
            # "in" names which side of a->b counts as an entry: a left->right
            # crossing has cross-product sign +1, i.e. the point moves toward the
            # RIGHT side. ab_in True means that +1 (left->right) is an entry.
            ab_in = str(line_cfg.get("in", "right")).lower() != "left"
            self.line.set_line(line_cfg["a"], line_cfg["b"], ab_in)

        self.window = RollingWindow(float(params.get("window_duration", 60.0)))

        # Appearance-based fallback counters (used only when no line configured).
        self._entry = 0
        self._exit = 0

        print(f"[retail] setup conf={self.conf} iou={self.iou} "
              f"zone={'on' if self.zone.enabled else 'off'} "
              f"line={'on' if self.line.enabled else 'off'} "
              f"dwell(engaged={self.dwell.cfg.engaged_sec}s "
              f"assist={self.dwell.cfg.assistance_sec}s "
              f"speed={self.dwell.cfg.speed_threshold}px/s) "
              f"window={self.window.window_sec}s", flush=True)

    def on_results(self, results, frame):
        # 1. Keep only person detections for tracking.
        persons = [d for d in results if d.get("cls") == PERSON_CLS]
        tracks = self.tracker.update(persons, frame.pts, frame.w, frame.h)

        events = []

        # 2. Per-track dwell state + emit a track event (overlay-friendly).
        live_ids = [tr.track_id for tr in tracks]
        self.dwell.prune(live_ids)
        counts = StateCount()
        in_zone = self.zone.inside(tracks)
        in_zone_ids = {tr.track_id for tr in in_zone}
        for tr in tracks:
            state = self.dwell.update(tr, frame.pts)
            if tr.track_id in in_zone_ids:
                counts.total += 1
                if state == ENGAGED:
                    counts.engaged += 1
                elif state == ASSISTANCE:
                    counts.assistance += 1
                else:
                    counts.browsing += 1
            x1 = (tr.cx - tr.w / 2) * frame.w
            y1 = (tr.cy - tr.h / 2) * frame.h
            x2 = (tr.cx + tr.w / 2) * frame.w
            y2 = (tr.cy + tr.h / 2) * frame.h
            events.append({
                "kind": "track",
                "track_id": tr.track_id,
                "state": state,
                "in_zone": tr.track_id in in_zone_ids,
                "score": round(tr.score, 3),
                "speed_px_s": round(tr.speed_px_s, 1),
                "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            })

        # 3. Entry/exit counting: line crossings if configured, else appearance.
        if self.line.enabled:
            for ev in self.line.update(tracks):
                events.append({"kind": "line_cross", **ev})
            entry, exit_ = self.line.entry_count, self.line.exit_count
        else:
            self._entry += len(self.tracker.new_ids)
            self._exit += len(self.tracker.removed_ids)
            for tid in self.tracker.new_ids:
                events.append({"kind": "line_cross", "track_id": tid, "dir": "in"})
            for tid in self.tracker.removed_ids:
                events.append({"kind": "line_cross", "track_id": tid, "dir": "out"})
            entry, exit_ = self._entry, self._exit

        # 4. Rolling-window metrics snapshot.
        self.window.update(counts, entry, exit_, frame.pts)
        snap = self.window.snapshot()
        events.append({
            "kind": "metrics",
            "occupancy": snap.occupancy,
            "browsing": snap.browsing,
            "engaged": snap.engaged,
            "assistance": snap.assistance,
            "peak": snap.peak,
            "entry_count": snap.entry_count,
            "exit_count": snap.exit_count,
        })

        for r in results:
            r.setdefault("kind", "person")
        return events


if __name__ == "__main__":
    run_app(RetailVisionApp())
