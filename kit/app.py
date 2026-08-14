"""
App base class + generic main loop for reCamera Pro Kit (see docs/guide/kit-design.md §3).

An application is a thin subclass that overrides at most two hooks:

    setup(config)               -- read config_schema params (conf, iou, ...)
    on_results(results, frame)  -- ★the only business-logic entry point★
                                   turn raw detections into app-level events.
                                   Default = passthrough (no events).

Everything else -- open the frame source, skip camera warm-up placeholder
frames, letterbox, RKNN infer, detect post-process, publish via ResultSink,
and collect FPS / latency debug metrics -- lives here in the base loop and is
never re-implemented per app.

Import convention: `kit` is a package. The directory that CONTAINS `kit/` is on
sys.path (the appmgr and each app's bootstrap add it), so kit modules import each
other as `kit.adapters.*` / `kit.runtime.*`. This avoids the app.py/kit.app name
collision that a bare `app` module would cause.
"""
from __future__ import annotations

import argparse
import inspect
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import numpy as np

from kit.adapters.frame_source import open_frame_source, DEFAULT_SUB_STREAM, Frame
from kit.adapters.result_sink import ResultSink, open_result_sink
from kit.runtime.preprocess import letterbox
from kit.runtime.postprocess.detect import postprocess, COCO80
# NOTE: RknnModel (kit.runtime.engine) is imported LAZILY inside run(), not here.
# It does `from rknnlite.api import RKNNLite` at module top, so importing it
# eagerly would force rknnlite onto every interpreter that touches kit.app --
# including CPU-only / audio apps (e.g. voice-transcribe under the sherpa venv
# /userdata/rknnenv, which has no rknnlite). Model-backed vision apps still get
# it the moment run() constructs a model; behaviour there is unchanged.


# --------------------------------------------------------------------------- #
# New app shape (internal/KIT_APP_SHAPE_SPEC.md §1/§2): the app owns an explicit
# `run()` loop and calls back into these kit-owned primitives. Everything below
# is ADDITIVE -- the legacy `run(model_path, ...)` loop further down is
# untouched, so apps that have not been migrated keep working unchanged.
# --------------------------------------------------------------------------- #

@dataclass
class PreparedInput:
    """What `App.pre(frame)` returns: model-space pixels + the letterbox map.

    `.data` is a uint8 HWC (or 1HWC) RGB array ready for `.infer()`; `.info` is
    a `LetterboxInfo`-compatible object post-processing uses to map coordinates
    back to ORIGINAL camera geometry.
    """
    data: Any
    info: Any

    def __iter__(self):            # allows `x, info = self.pre(frame)`
        return iter((self.data, self.info))


class _ModelHandle:
    """One preloaded model. `infer()` is timed into the owning app's frame budget."""

    def __init__(self, model_id: str, path: str, owner: "App", impl: Any):
        self.id = model_id
        self.path = path
        self._owner = owner
        self._impl = impl

    def infer(self, x):
        """Run one forward pass. Accepts a raw array or a `PreparedInput`."""
        if isinstance(x, PreparedInput):
            x = x.data
        t0 = time.monotonic()
        try:
            return self._impl.infer(x)
        finally:
            self._owner._t_infer += time.monotonic() - t0

    def release(self) -> None:
        rel = getattr(self._impl, "release", None)
        if rel is not None:
            try:
                rel()
            except Exception:
                pass

    def __repr__(self) -> str:      # pragma: no cover - debug aid
        return f"<model {self.id} {self.path}>"


# task -> extra short aliases, so `self.models.det` works for a manifest model
# declared as {"id": "yolo8n_rawhead_int8", "task": "detect"}.
_TASK_ALIASES = {
    "detect": ("det",),
    "detection": ("det",),
    "recognize": ("rec",),
    "recognition": ("rec",),
    "rec": ("rec",),
    "classify": ("cls",),
    "classification": ("cls",),
    "landmark": ("lmk",),
    "segment": ("seg",),
    "segmentation": ("seg",),
}


class ModelRegistry:
    """`self.models` -- attribute / index access to the manifest's `models[]`.

    Every model is reachable by its manifest `id`. Convenience aliases are added
    when unambiguous: the model's `task` (e.g. `.detect`), a short form of it
    (`.det`, `.rec`, ...), and -- for a single-model app -- `.model`/`.first`.
    An alias claimed by two models is dropped rather than resolved arbitrarily.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_by_id", {})
        object.__setattr__(self, "_alias", {})
        object.__setattr__(self, "_order", [])
        object.__setattr__(self, "_ambiguous", set())

    # -- construction (kit-internal) ------------------------------------- #
    def _register(self, model_id: str, handle: _ModelHandle, aliases=()) -> None:
        self._by_id[model_id] = handle
        self._order.append(handle)
        for a in aliases:
            if not a or a == model_id or a in self._by_id:
                continue
            if a in self._alias and self._alias[a] is not handle:
                self._ambiguous.add(a)
                self._alias.pop(a, None)
                continue
            if a in self._ambiguous:
                continue
            self._alias[a] = handle

    def _all(self) -> List[_ModelHandle]:
        return list(self._order)

    # -- access ----------------------------------------------------------- #
    def __getattr__(self, name):
        by_id = object.__getattribute__(self, "_by_id")
        if name in by_id:
            return by_id[name]
        alias = object.__getattribute__(self, "_alias")
        if name in alias:
            return alias[name]
        known = sorted(set(list(by_id) + list(alias)))
        if name in object.__getattribute__(self, "_ambiguous"):
            raise AttributeError(
                f"model alias {name!r} is ambiguous (several models claim it); "
                f"use the manifest id: {known}")
        raise AttributeError(
            f"no model {name!r} in manifest models[]; available: {known}")

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._order[key]
        return getattr(self, key)

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self):
        return iter(self._order)

    def __repr__(self) -> str:      # pragma: no cover - debug aid
        return f"<ModelRegistry {[h.id for h in self._order]}>"


def _schema_items(manifest: Optional[dict]) -> Dict[str, dict]:
    """Return {key: spec} from a FLAT or GROUPED `config_schema`.

    Both structures exist in the wild today (yolo-detector is flat, the rest are
    grouped); auto-binding must handle either. Mirrors `kit.config.flatten_schema`
    but keeps the whole spec dict (we need `type` and `apply`, not just default).
    """
    cs = (manifest or {}).get("config_schema") or {}
    out: Dict[str, dict] = {}
    if isinstance(cs, dict) and "groups" in cs:
        for g in cs.get("groups") or []:
            for it in g.get("items") or []:
                key = it.get("key")
                if key:
                    out[key] = it
    elif isinstance(cs, dict):
        for k, v in cs.items():
            if isinstance(v, dict):
                out[k] = v
    return out


def _coerce(spec_type: Optional[str], value):
    """Best-effort conversion of a config value to its declared schema type.

    Never raises: an unconvertible value is returned unchanged so a typo in
    config.json degrades to "wrong value" rather than "app will not start".
    """
    try:
        if spec_type == "number":
            return float(value)
        if spec_type == "integer":
            return int(value)
        if spec_type == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if spec_type == "string":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return value
    return value


_POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY,
                     inspect.Parameter.POSITIONAL_OR_KEYWORD,
                     inspect.Parameter.VAR_POSITIONAL)


def _run_takes_positional(fn) -> bool:
    """True when `fn` (an unbound `run`) accepts any positional arg besides self.

    That is the shape of the LEGACY loop entry point
    (`run(self, model_path, *, source=..., ...)`); the new shape takes none.
    Unintrospectable callables are treated as legacy (the safer default: the old
    path is what every not-yet-migrated app uses).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):     # pragma: no cover - exotic callables
        return True
    params = [p for n, p in sig.parameters.items() if n != "self"]
    return any(p.kind in _POSITIONAL_KINDS for p in params)


def _is_new_shape(app: "App") -> bool:
    """Which loop shape this app uses -- decided by the EXPLICIT `owns_loop` flag.

    `owns_loop = True` on the subclass means "I drive the loop myself"
    (KIT_APP_SHAPE_SPEC §1/§3): kit calls `start()` + `run()` + `finish()`.
    Everything else goes down the legacy `run(model_path, ...)` path.

    Signature sniffing was removed on purpose. It made `def run(self, *,
    debug=False)` -- a perfectly reasonable new-shape signature -- silently fall
    through to the legacy path, which opens a camera frame source the app never
    asked for. Instead the two ways of getting it wrong are now hard startup
    errors:

      * a `run()` that LOOKS new-shape (takes no positional arg) but has no
        `owns_loop = True`;
      * `owns_loop = True` on a `run()` that still takes positional args
        (i.e. the legacy signature, or `run()` not overridden at all).
    """
    cls = type(app)
    fn = getattr(cls, "run", None)
    overridden = fn is not None and fn is not App.run
    owns = bool(getattr(cls, "owns_loop", False))

    if owns:
        if not overridden:
            raise RuntimeError(
                f"{cls.__name__}: owns_loop = True but run() is not overridden; "
                f"a loop-owning app must define `def run(self): "
                f"for frame in self.frames(): ...` (spec §1)")
        if _run_takes_positional(fn):
            raise RuntimeError(
                f"{cls.__name__}: owns_loop = True but run{inspect.signature(fn)} "
                f"takes positional arguments; a loop-owning run() takes no "
                f"arguments (kit supplies frames via self.frames()) (spec §1)")
        return True

    if overridden and not _run_takes_positional(fn):
        raise RuntimeError(
            f"{cls.__name__}: run{inspect.signature(fn)} takes no positional "
            f"arguments, which is the NEW app shape, but the class does not set "
            f"`owns_loop = True`. Add `owns_loop = True` to {cls.__name__} (kit "
            f"will then call start()/run()/finish() and hand you frames via "
            f"self.frames()); if you really meant the legacy callback shape, "
            f"keep the legacy run(self, model_path, ...) signature instead "
            f"(spec §1/§3).")
    return False


class App:
    """Base application. Subclass and override `setup` / `on_results`."""

    # Subclasses set these (usually mirrored from manifest.json).
    id: str = "app"
    name: str = "App"
    postproc: str = "detect"          # which post-processor the base loop runs
    input_size: int = 640             # stage-1 model input side (letterbox target);
                                      # ppocr-reader overrides to 480 for the DB detector
    # ★Loop shape★ (KIT_APP_SHAPE_SPEC §1/§3). False = legacy shape: the base
    # `run(model_path, ...)` loop drives the app through run_postproc/on_results/
    # process_frame. True = the app owns the loop: it defines `def run(self):`
    # and iterates `self.frames()`; kit calls start()/run()/finish() around it.
    # This is an EXPLICIT declaration -- kit never guesses from the signature.
    owns_loop: bool = False

    needs_model: bool = True          # CPU-only apps (e.g. qrcode-reader) set this
                                      # False: the loop skips RknnModel + letterbox +
                                      # infer and calls process_frame(frame) instead.

    # Where the model-input letterbox is produced.  The Python resize costs
    # ~40 ms/frame at 1280x720 -> 640x640; moving it onto RGA measured +49% e2e
    # throughput.  Set it on your App subclass:
    #
    #     class MyApp(App):
    #         model_frame = "hw"        # or "hw-direct"
    #
    #   "cpu"       (default) letterbox in this loop.  Always correct.
    #   "hw"        the source letterboxes on RGA into ``frame.model_data`` and
    #               keeps ORIGINAL-resolution pixels in ``frame.data``.  Safe for
    #               any model-backed app, including ones that crop source pixels
    #               (ROI / perspective) after inference.
    #   "hw-direct" the RGA letterbox IS ``frame.data`` -- also skips the
    #               full-resolution NV12->RGB conversion (the cheapest path), so
    #               NO original-resolution pixels are available.  Only for apps
    #               that consume detections/keypoints and never read
    #               ``frame.data``.
    #
    # ``frame.w``/``frame.h`` and post-processed coordinates stay in original
    # camera geometry in every mode.  Ignored when ``needs_model`` is False, when
    # the backend exposes no dma-buf fd (RTSP/snapshot), or when RGA/librga is
    # unavailable -- each case falls back to the CPU letterbox with identical
    # geometry, never an error.
    model_frame: str = "cpu"

    def __init__(self) -> None:
        # config_schema-backed knobs (defaults; overridden in setup())
        self.conf: float = 0.25
        self.iou: float = 0.45
        self.class_names = COCO80
        self.config: Dict[str, Any] = {}
        # Hot-reload (SIGHUP) flag. appmgr set_config sends SIGHUP after writing
        # config.json when ALL changed items are apply:"live" (see DESIGN
        # §3.2/§4). The signal handler only FLIPS this flag; the main loop does
        # the real re-read on the next frame -- signal handlers must stay tiny
        # and must not touch the model / pipeline.
        self._reload_flag: bool = False

        # -- new-shape runtime state (unused by legacy apps) --------------- #
        # `self.models` is populated by start(); an empty registry until then so
        # attribute access fails with a clear message instead of AttributeError
        # on `self.models` itself.
        self.models = ModelRegistry()
        self._rt: Optional[Dict[str, Any]] = None   # runtime options from start()
        self._manifest: Optional[dict] = None
        self._params_bound: bool = False
        self._pre_size: int = 0
        self._cur_frame: Optional[Frame] = None
        self._t_frame0: float = 0.0
        self._t_pre: float = 0.0
        self._t_infer: float = 0.0
        self._t_emit: float = 0.0
        self._warmed: bool = False
        self._processed: int = 0
        self._last_payload: Optional[Dict[str, Any]] = None

    # -- application hooks (override these) -------------------------------- #
    def setup(self, config: Dict[str, Any]) -> None:
        """Read config_schema parameters. Override + call super().setup(config)."""
        self.config = config or {}
        self.conf = float(self.config.get("conf", self.conf))
        self.iou = float(self.config.get("iou", self.iou))

    # -- live-reload value-replace helpers (shared by every app override) --- #
    @staticmethod
    def _reload_params(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Drop keys whose value is None so a missing/cleared config item never
        overwrites a live attribute (the None-filter every app applied by hand)."""
        return {k: v for k, v in (config or {}).items() if v is not None}

    @staticmethod
    def _reload_float(params: Dict[str, Any], key: str, cur: float) -> float:
        """Value-replace a float knob from `params`; keep `cur` on absence/garbage."""
        try:
            return float(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    @staticmethod
    def _reload_int(params: Dict[str, Any], key: str, cur: int) -> int:
        """Value-replace an int knob from `params`; keep `cur` on absence/garbage."""
        try:
            return int(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    def on_config_reload(self, config: Dict[str, Any]) -> None:
        """★Live config hot-reload hook★ (SIGHUP -> re-read config.json).

        Called from the main loop when appmgr signals a LIVE-only config change.
        `config` is the freshly re-read effective config (manifest defaults
        overlaid by the updated config.json).

        Base default: reapply ONLY the base-managed live knobs (conf/iou) and
        refresh self.config. This is deliberately safe -- it just replaces
        values, and NEVER rebuilds the model, frame source, or any pipeline
        state. Subclasses that snapshot extra params into their own attributes
        (e.g. self.max_faces, thresholds, ROI geometry) override this to reapply
        those the same value-replacing way -- use the `_reload_float/_reload_int`
        helpers above. Anything structural (model swap, input_size, backend,
        buffer resize) must NOT be hot-reloaded -- those params are
        apply:"restart" in the manifest and never reach here.
        """
        self.config = config or {}
        params = self._reload_params(config)
        self.conf = self._reload_float(params, "conf", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)

    # -- hot-reload plumbing (base; apps do not touch) -------------------- #
    def _install_reload_handler(self) -> None:
        """Install the SIGHUP handler. Best-effort: signal.signal only works on
        the main thread, so a non-main-thread run() silently skips hot-reload
        (the app still works, config changes just need a restart)."""
        try:
            signal.signal(signal.SIGHUP, self._on_sighup)
        except (ValueError, OSError):
            pass

    def _on_sighup(self, signum, frame) -> None:
        # Tiny by design: only flip the flag; the loop does the work.
        self._reload_flag = True

    def _maybe_reload(self) -> None:
        """If a SIGHUP arrived, re-read the effective config and hand it to
        on_config_reload. Never raises into the loop."""
        if not self._reload_flag:
            return
        self._reload_flag = False
        from kit import config as _cfg
        try:
            cfg = _cfg.effective_config(_cfg.app_dir_of(self))
        except Exception as e:                 # config unreadable -> keep running
            print(f"[app:{self.id}] config reload skipped (read failed: {e})",
                  flush=True)
            return
        # New-shape apps: re-bind the apply:"live" config_schema keys onto self
        # BEFORE the (usually absent) on_config_reload hook, so an override can
        # still see/adjust the freshly bound values. Legacy apps never reach
        # this branch -- _params_bound is only set by start().
        if self._params_bound:
            try:
                changed = self._bind_params(cfg, live_only=True)
                if changed:
                    self.on_params_changed(changed)
            except Exception as e:             # binding bug must not kill loop
                print(f"[app:{self.id}] param rebind failed: {e}", flush=True)
        try:
            self.on_config_reload(cfg)
            print(f"[app:{self.id}] config hot-reloaded ({len(cfg)} keys)",
                  flush=True)
        except Exception as e:                 # app hook bug must not kill loop
            print(f"[app:{self.id}] config reload failed: {e}", flush=True)

    # ------------------------------------------------------------------ #
    # New app shape: start / frames / pre / models / emit / tick
    # (internal/KIT_APP_SHAPE_SPEC.md §2). A migrated app overrides `run(self)`
    # and drives these; `run_app` wires them up.
    # ------------------------------------------------------------------ #
    def _load_model(self, path: str):
        """Construct the NPU model for `path`. Overridable seam (tests stub it)."""
        from kit.runtime.engine import RknnModel  # lazy: only vision apps need rknnlite
        return RknnModel(path)

    def on_params_changed(self, changed: set) -> None:
        """Called after SIGHUP re-bound the apply:"live" params onto `self`.

        `changed` is the set of keys whose value actually differs. Override only
        when a derived object must be rebuilt (state machine, cached geometry).
        Plain scalar knobs need nothing -- they are already re-bound.
        """
        pass

    def _bind_params(self, config: Dict[str, Any], *, live_only: bool = False) -> set:
        """Bind `config_schema` keys onto `self` as plain attributes.

        Returns the set of keys whose value changed. `live_only` restricts the
        pass to `apply:"live"` items (the SIGHUP re-bind); the initial pass binds
        everything. A None value is skipped so a cleared config item never wipes
        a live attribute (same rule as `_reload_params`). A key that would shadow
        a method/property on the class is skipped with a warning.
        """
        schema = _schema_items(self._manifest)
        changed = set()
        for key, spec in schema.items():
            if live_only and (spec.get("apply") or "live") != "live":
                continue
            if key not in config:
                continue
            value = config[key]
            if value is None:
                continue
            attr = getattr(type(self), key, None)
            if callable(attr) or isinstance(attr, property):
                print(f"[app:{self.id}] config key {key!r} not auto-bound "
                      f"(would shadow a method/property)", flush=True)
                continue
            value = _coerce(spec.get("type"), value)
            if getattr(self, key, object()) != value:
                changed.add(key)
            setattr(self, key, value)
        self._params_bound = True
        return changed

    def start(
        self,
        model_path: Optional[str] = None,
        *,
        source: str = "ffmpeg",
        url: str = DEFAULT_SUB_STREAM,
        sink: Optional[ResultSink] = None,
        n: int = 0,
        every: int = 1,
        skip_gray_std: float = 8.0,
        max_gray_skip: int = 120,
        verbose: bool = True,
        app_dir: Optional[str] = None,
        manifest: Optional[dict] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> "App":
        """Prepare the kit-owned runtime for a new-shape `run()`.

        Loads the manifest's `models[]` (paths made absolute against the install
        dir), binds `config_schema` params onto `self`, opens the frame source and
        installs the SIGHUP handler. `run_app` calls this; `finish()` tears it down.
        `model_path` (the `--model` CLI flag) overrides the FIRST manifest model.
        """
        from kit import config as _cfg
        if app_dir is None:
            app_dir = _cfg.app_dir_of(self)
        if manifest is None:
            manifest = _cfg.load_manifest(app_dir)
        self._manifest = manifest or {}

        # Shape guard: start() is the loop-owning entry point, so the class must
        # actually declare `owns_loop = True` with a no-argument run(). Raises
        # with the fix spelled out when the two disagree (see _is_new_shape).
        if not _is_new_shape(self):
            raise RuntimeError(
                f"{type(self).__name__}: start() is the loop-owning entry point "
                f"but the class does not declare `owns_loop = True`")

        # Migration guard (spec §6): the new shape and the old callback shape
        # must never both be present -- a silent precedence rule would be worse
        # than a startup failure.
        cls = type(self)
        legacy = [h for h in ("on_results", "process_frame", "run_postproc")
                  if getattr(cls, h, None) is not getattr(App, h, None)]
        if legacy:
            raise RuntimeError(
                f"{cls.__name__}: new-shape run() cannot be combined with legacy "
                f"hook(s) {legacy}; move that logic into run() (spec §6)")

        cfg = config if config is not None else (self.config or {})
        self._bind_params(cfg)
        # setup() runs AFTER the auto-bind so an app that still needs one (to
        # build derived objects: trackers, zone geometry, state machines) can
        # read the already-bound `self.<param>` attributes instead of digging
        # through the raw config dict. `run_app` therefore does NOT call setup()
        # for a loop-owning app -- start() owns that call.
        self.setup(cfg)

        # -- models: preload + absolutise paths --------------------------- #
        self.models = ModelRegistry()
        decls = list(self._manifest.get("models") or [])
        if self.needs_model:
            for i, m in enumerate(decls):
                mid = m.get("id") or f"model{i}"
                rel = m.get("file") or ""
                path = rel if os.path.isabs(rel) else os.path.join(app_dir, rel)
                if i == 0 and model_path:
                    # --model overrides the primary model. supervisor passes it
                    # RELATIVE (`models/x.rknn`), so absolutise it the same way.
                    path = (model_path if os.path.isabs(model_path)
                            else os.path.join(app_dir, model_path))
                aliases = []
                task = (m.get("task") or "").strip().lower()
                if task:
                    aliases.append(task)
                    aliases.extend(_TASK_ALIASES.get(task, ()))
                if len(decls) == 1:
                    aliases.extend(("model", "first"))
                self.models._register(mid, _ModelHandle(mid, path, self,
                                                        self._load_model(path)),
                                      aliases)
            if not decls and model_path:
                # No manifest models[] but a --model was supplied: expose it as
                # `.model` so a hand-run app still works.
                p = (model_path if os.path.isabs(model_path)
                     else os.path.join(app_dir, model_path))
                self.models._register("model",
                                      _ModelHandle("model", p, self,
                                                   self._load_model(p)),
                                      ("first",))

        # -- model input geometry: manifest first, class attribute as fallback #
        size = None
        if decls:
            inp = decls[0].get("input")
            if isinstance(inp, (list, tuple)) and len(inp) >= 3:
                try:
                    size = int(inp[1])
                except (TypeError, ValueError):
                    size = None
        self._pre_size = size or self.input_size

        mode = self.model_frame if self.needs_model else "cpu"
        if mode not in ("cpu", "hw", "hw-direct"):
            raise ValueError(
                "%s: model_frame must be 'cpu', 'hw' or 'hw-direct' (got %r)"
                % (self.id, self.model_frame))
        src = open_frame_source(
            url=url,
            prefer=source,
            input_size=self._pre_size if mode != "cpu" else 0,
            direct_preprocess=(mode == "hw-direct"),
            hw_letterbox=(mode == "hw"),
        )

        if sink is None:
            sink = open_result_sink("stdout")
            own_sink = True
        else:
            own_sink = False

        self._rt = {
            "src": src, "sink": sink, "own_sink": own_sink, "n": n,
            "every": every, "skip_gray_std": skip_gray_std,
            "max_gray_skip": max_gray_skip, "verbose": verbose,
            "app_dir": app_dir, "source": source, "url": url,
            "grays_skipped": 0, "loop_start": None,
        }
        self._warmed = False
        self._processed = 0
        self._install_reload_handler()
        if verbose:
            print(f"[app:{self.id}] models={[h.path for h in self.models]} "
                  f"source={source} url={url} input={self._pre_size} "
                  f"sink={type(sink).__name__}", flush=True)
        return self

    def finish(self) -> None:
        """Release everything `start()` acquired and print the run summary."""
        rt, self._rt = self._rt, None
        if rt is None:
            return
        try:
            rt["src"].close()
        except Exception:
            pass
        for h in self.models:
            h.release()
        if rt["own_sink"]:
            try:
                rt["sink"].close()
            except Exception:
                pass
        loop_start = rt.get("loop_start")
        if self._processed and loop_start:
            wall = time.monotonic() - loop_start
            print(f"\n[app:{self.id}] === {self._processed} frames "
                  f"(grey-skipped {rt['grays_skipped']}) ===", flush=True)
            print(f"[app:{self.id}] end-to-end {self._processed/wall:4.1f} fps",
                  flush=True)
        elif rt["verbose"]:
            print(f"[app:{self.id}] no frames processed", file=sys.stderr)

    def frames(self) -> Iterator[Frame]:
        """Yield frames to the app's `run()` loop, kit-managed.

        What it does for you (spec §2, and §8's "say what it hides"):
          * opens/owns the frame source (`start()`), releases each frame by
            simply advancing the iterator -- do NOT hold a frame past one turn;
          * skips the camera's grey warm-up placeholder frames;
          * honours `--every N` frame skipping;
          * applies pending SIGHUP config hot-reloads at the frame boundary;
          * runs the FIRST real frame as a model warm-up: your loop body executes
            normally (so the NPU gets warm) but that frame's `emit()` is dropped,
            exactly as the legacy loop did;
          * measures the frame budget and flushes the periodic `metrics` meta
            event (pre/infer/emit measured by kit, the remainder is `app`);
          * stops after `--n` processed frames.
        """
        rt = self._rt
        if rt is None:
            raise RuntimeError(
                f"{type(self).__name__}.frames() called before start(); use "
                f"run_app(app) (or app.start(...)) to drive a new-shape app")
        verbose = rt["verbose"]
        every = max(1, int(rt["every"] or 1))
        n = int(rt["n"] or 0)

        METRICS_PERIOD = 1.0
        m_t0 = time.monotonic()
        m_frames = 0
        m_pre = m_inf = m_emit = m_app = 0.0

        got_real = False
        fidx = 0

        for frame in rt["src"].frames():
            self._maybe_reload()

            if not got_real:
                std = float(np.asarray(frame.data).std())
                if (std < rt["skip_gray_std"]
                        and rt["grays_skipped"] < rt["max_gray_skip"]):
                    rt["grays_skipped"] += 1
                    continue
                got_real = True
                if verbose:
                    print(f"[app:{self.id}] skipped {rt['grays_skipped']} grey "
                          f"warm-up frames; first real frame std={std:.1f}",
                          flush=True)

            fidx += 1
            if every > 1 and (fidx % every) != 0:
                continue

            # -- frame boundary: reset the per-frame timing buckets --------- #
            self._cur_frame = frame
            self._t_pre = self._t_infer = self._t_emit = 0.0
            self._t_frame0 = time.monotonic()
            self._last_payload = None

            yield frame                                   # <-- app's loop body

            total = time.monotonic() - self._t_frame0
            self._cur_frame = None

            if not self._warmed:
                self._warmed = True
                rt["loop_start"] = time.monotonic()
                if verbose:
                    print(f"[app:{self.id}] warmup frame {frame.w}x{frame.h} "
                          f"fmt={frame.fmt} (output dropped)", flush=True)
                continue

            self._processed += 1
            app_ms = total - self._t_pre - self._t_infer - self._t_emit
            m_frames += 1
            m_pre += self._t_pre
            m_inf += self._t_infer
            m_emit += self._t_emit
            m_app += max(0.0, app_ms)

            m_now = time.monotonic()
            m_dt = m_now - m_t0
            if m_dt >= METRICS_PERIOD and m_frames:
                try:
                    rt["sink"].emit_meta({
                        "type": "metrics",
                        "kind": "metrics",
                        "app": self.id,
                        "fps": round(m_frames / m_dt, 1),
                        "latency_ms": {
                            # `pre`/`infer`/`post` keep the existing appmgr /
                            # debug-panel contract; in the new shape the app owns
                            # post-processing, so `post` IS the app bucket. `app`
                            # and `emit` are additive detail (spec §4).
                            "pre": round(m_pre / m_frames * 1000, 1),
                            "infer": round(m_inf / m_frames * 1000, 1),
                            "post": round(m_app / m_frames * 1000, 1),
                            "app": round(m_app / m_frames * 1000, 1),
                            "emit": round(m_emit / m_frames * 1000, 1),
                        },
                        "frames": self._processed,
                        "pts": frame.pts,
                    })
                except Exception:
                    pass    # telemetry must never break the inference loop
                m_t0 = m_now
                m_frames = 0
                m_pre = m_inf = m_emit = m_app = 0.0

            if verbose:
                p = self._last_payload or {}
                res = p.get("results") or []
                evs = p.get("events") or []

                def _label(d):
                    name = (d.get("cls_name") or d.get("text")
                            or d.get("label") or "?")
                    return (f"{name}:{d['score']:.2f}" if "score" in d
                            else f"{name}")
                names = ", ".join(_label(d) for d in res[:6])
                print(f"[app:{self.id}] frame#{self._processed:03d} "
                      f"dets={len(res):2d} events={len(evs)} "
                      f"clients={getattr(rt['sink'], 'client_count', lambda: 0)()} "
                      f"{names}", flush=True)

            if n and self._processed >= n:
                break

    def pre(self, frame: Frame) -> PreparedInput:
        """Produce the model input for `frame` (letterbox to the manifest size).

        Prefers what the frame source already did on RGA -- `frame.model_data`
        (hw) or a `frame.model_info`-annotated `frame.data` (hw-direct) -- and
        only falls back to the Python letterbox. Geometry is identical in every
        case; `.info` always maps back to ORIGINAL camera pixels.
        """
        t0 = time.monotonic()
        try:
            info = getattr(frame, "model_info", None)
            padded = getattr(frame, "model_data", None)
            if padded is None:
                if info is not None:
                    padded = frame.data
                else:
                    padded, info = letterbox(frame.data, self._pre_size
                                             or self.input_size)
            return PreparedInput(padded, info)
        finally:
            self._t_pre += time.monotonic() - t0

    def emit(self, events=None, ts: Optional[float] = None, *,
             results=None, extra: Optional[Dict[str, Any]] = None) -> None:
        """Publish one frame's output through the manifest-configured sinks.

        `events` are the app-level events; `results` (optional) are the raw
        per-frame detections/records that the /appcenter overlay and the
        manifest `output` field mappings read as `results[]`. `ts` defaults to
        the current frame's pts.

        During the warm-up frame this is a no-op (same as the legacy loop).
        """
        rt = self._rt
        if rt is None:
            raise RuntimeError("emit() called outside a started run()")
        t0 = time.monotonic()
        try:
            frame = self._cur_frame
            payload = {
                "results": list(results) if results is not None else [],
                "events": list(events) if events is not None else [],
                "inference_time_ms": round(self._t_infer * 1000.0, 3),
                "pipeline_ms": round((t0 - self._t_frame0) * 1000.0, 3),
                "stream_id": "camera-0",
            }
            if extra:
                payload.update(extra)
            self._last_payload = payload
            if not self._warmed:
                return                      # warm-up frame: output discarded
            sink = rt["sink"]
            if frame is not None:
                sink.set_frame_size(frame.w, frame.h)
            if ts is None:
                ts = frame.pts if frame is not None else time.monotonic()
            sink.emit(payload, ts)
        finally:
            self._t_emit += time.monotonic() - t0

    def tick(self) -> None:
        """Apply a pending SIGHUP config hot-reload.

        `frames()` already ticks at every frame boundary; only an app that takes
        over the loop entirely (audio chunks, multi-stream) needs to call this.
        """
        self._maybe_reload()

    def run_postproc(self, outs, info) -> List[dict]:
        """Turn raw RKNN outputs into result dicts. Default = YOLO detect.

        Pose apps override this to run kit.runtime.postprocess.pose instead.
        Kept as a hook so the generic run() loop stays post-processor agnostic.
        """
        return postprocess(outs, info, conf_thres=self.conf, iou_thres=self.iou,
                           class_names=self.class_names)

    def process_frame(self, frame: Frame) -> List[dict]:
        """CPU-only entry point for `needs_model = False` apps.

        Turn one `Frame` straight into result dicts WITHOUT any NPU model /
        letterbox / infer. Only called when `needs_model` is False; model-backed
        apps ignore this and use the letterbox->infer->run_postproc path instead.
        qrcode-reader overrides this to run cv2.QRCodeDetector on the frame.
        """
        raise NotImplementedError("process_frame() must be overridden when needs_model=False")

    def on_results(self, results: List[dict], frame: Frame) -> List[dict]:
        """★Business logic★. Map raw detections -> app-level events.

        Default: passthrough (no derived events). yolo-detector overrides this
        to format detection events; fall-detection would run its state machine
        here, etc. `results` is the list of detect dicts; return a list of
        JSON-serialisable event dicts.
        """
        return []

    # -- generic main loop (base; apps do not touch) ---------------------- #
    def run(
        self,
        model_path: str,
        *,
        source: str = "ffmpeg",
        url: str = DEFAULT_SUB_STREAM,
        sink: Optional[ResultSink] = None,
        n: int = 0,
        every: int = 1,
        skip_gray_std: float = 8.0,
        max_gray_skip: int = 120,
        verbose: bool = True,
    ) -> None:
        """Run the live pipeline: frames -> infer -> post -> on_results -> emit.

        n=0 runs until the stream ends / interrupted; n>0 stops after n frames.
        `skip_gray_std`: frames whose pixel std is below this are treated as the
        camera's warm-up placeholder (grey, std~0) and skipped (up to
        `max_gray_skip`).
        """
        own_sink = sink is None
        if sink is None:
            sink = open_result_sink("stdout")

        # Enable SIGHUP config hot-reload for the lifetime of this loop.
        self._install_reload_handler()

        if self.needs_model:
            model = self._load_model(model_path)
        else:
            model = None
        mode = self.model_frame if self.needs_model else "cpu"
        if mode not in ("cpu", "hw", "hw-direct"):
            raise ValueError(
                "%s: model_frame must be 'cpu', 'hw' or 'hw-direct' (got %r)"
                % (self.id, self.model_frame))
        src = open_frame_source(
            url=url,
            prefer=source,
            input_size=self.input_size if mode != "cpu" else 0,
            direct_preprocess=(mode == "hw-direct"),
            hw_letterbox=(mode == "hw"),
        )

        t_pre = t_inf = t_post = 0.0
        processed = 0
        grays_skipped = 0
        got_real = False
        warmed = False
        loop_start = None
        fidx = 0

        # -- live telemetry (debug panel): FPS + per-stage latency ---------- #
        # Accumulated over a short window and flushed as a `metrics` meta event
        # once per METRICS_PERIOD s. Purely additive -- rides the existing WS
        # channel via emit_meta (a no-op on MQTT), never touches results/events.
        METRICS_PERIOD = 1.0
        m_t0 = time.monotonic()
        m_frames = 0
        m_pre = m_inf = m_post = 0.0

        if verbose:
            print(f"[app:{self.id}] model={model_path} source={source} url={url} "
                  f"conf={self.conf} iou={self.iou} sink={type(sink).__name__}",
                  flush=True)
        try:
            for frame in src.frames():
                # -- apply any pending live config hot-reload (SIGHUP) ------ #
                self._maybe_reload()
                # -- skip camera warm-up placeholder (grey) frames --------- #
                if not got_real:
                    std = float(np.asarray(frame.data).std())
                    if std < skip_gray_std and grays_skipped < max_gray_skip:
                        grays_skipped += 1
                        continue
                    got_real = True
                    if verbose:
                        print(f"[app:{self.id}] skipped {grays_skipped} grey "
                              f"warm-up frames; first real frame std={std:.1f}",
                              flush=True)

                fidx += 1
                if every > 1 and (fidx % every) != 0:
                    continue

                t0 = time.monotonic()
                if self.needs_model:
                    # OfficialFrameSource may have performed the aspect-ratio
                    # resize on RGA already.  It preserves original ``Frame.w``
                    #/``h`` and supplies a LetterboxInfo-compatible object;
                    # post-processing therefore still maps detections back to
                    # camera pixels while Python skips the second resize.
                    # "hw" delivers the letterbox alongside the original pixels
                    # (model_data); "hw-direct" letterboxes into data itself.
                    info = getattr(frame, "model_info", None)
                    padded = getattr(frame, "model_data", None)
                    if padded is None:
                        if info is not None:
                            padded = frame.data
                        else:
                            padded, info = letterbox(frame.data, self.input_size)
                    t1 = time.monotonic()
                    outs = model.infer(padded)
                    t2 = time.monotonic()
                    results = self.run_postproc(outs, info)
                    t3 = time.monotonic()
                else:
                    # CPU-only path: no letterbox / NPU infer. process_frame does
                    # all the work (counted under the "infer" timing bucket).
                    t1 = time.monotonic()
                    results = self.process_frame(frame)
                    t2 = t3 = time.monotonic()

                if not warmed:
                    warmed = True
                    loop_start = time.monotonic()
                    if verbose:
                        if self.needs_model:
                            print(f"[app:{self.id}] warmup output_shapes="
                                  f"{[o.shape for o in outs]} "
                                  f"frame={frame.w}x{frame.h} fmt={frame.fmt}",
                                  flush=True)
                        else:
                            print(f"[app:{self.id}] warmup (cpu, no model) "
                                  f"frame={frame.w}x{frame.h} fmt={frame.fmt}",
                                  flush=True)
                    continue

                events = self.on_results(results, frame)

                # Hand the sink the current frame's pixel dimensions before
                # emit so coordinate-normalizing sinks (OfficialResultSink ->
                # extension-API [0,1] contract) can divide by the ORIGINAL
                # full-res frame size our result coords are mapped to. No-op on
                # the WS/stdout/MQTT workaround sinks (they keep pixel coords).
                sink.set_frame_size(frame.w, frame.h)
                sink.emit({
                    "results": results,
                    "events": events,
                    "inference_time_ms": round((t2 - t1) * 1000.0, 3),
                    "pipeline_ms": round((time.monotonic() - t0) * 1000.0, 3),
                    "stream_id": "camera-0",
                }, frame.pts)

                t_pre += t1 - t0
                t_inf += t2 - t1
                t_post += t3 - t2
                processed += 1

                # -- periodic metrics meta event (FPS + per-stage latency) --- #
                m_frames += 1
                m_pre += t1 - t0
                m_inf += t2 - t1
                m_post += t3 - t2
                m_now = time.monotonic()
                m_dt = m_now - m_t0
                if m_dt >= METRICS_PERIOD and m_frames:
                    try:
                        sink.emit_meta({
                            "type": "metrics",
                            "kind": "metrics",
                            "app": self.id,
                            "fps": round(m_frames / m_dt, 1),
                            "latency_ms": {
                                "pre": round(m_pre / m_frames * 1000, 1),
                                "infer": round(m_inf / m_frames * 1000, 1),
                                "post": round(m_post / m_frames * 1000, 1),
                            },
                            "frames": processed,
                            "pts": frame.pts,
                        })
                    except Exception:
                        pass   # telemetry must never break the inference loop
                    m_t0 = m_now
                    m_frames = 0
                    m_pre = m_inf = m_post = 0.0

                if verbose:
                    def _label(d):
                        name = (d.get("cls_name") or d.get("text")
                                or d.get("label") or "?")
                        return (f"{name}:{d['score']:.2f}" if "score" in d
                                else f"{name}")
                    names = ", ".join(_label(d) for d in results[:6])
                    print(f"[app:{self.id}] frame#{processed:03d} "
                          f"dets={len(results):2d} events={len(events)} "
                          f"clients={getattr(sink, 'client_count', lambda: 0)()} "
                          f"{names}", flush=True)

                if n and processed >= n:
                    break
        finally:
            src.close()
            if model is not None:
                model.release()
            if own_sink:
                sink.close()

        if processed and loop_start:
            wall = time.monotonic() - loop_start
            comp = (t_pre + t_inf + t_post) / processed * 1000
            print(f"\n[app:{self.id}] === {processed} frames "
                  f"(grey-skipped {grays_skipped}) ===", flush=True)
            print(f"[app:{self.id}] preprocess {t_pre/processed*1000:5.1f} ms | "
                  f"infer {t_inf/processed*1000:5.1f} ms | "
                  f"post {t_post/processed*1000:5.1f} ms", flush=True)
            print(f"[app:{self.id}] compute-only {comp:5.1f} ms -> "
                  f"{1000.0/comp:4.1f} fps | end-to-end {processed/wall:4.1f} fps",
                  flush=True)
        elif verbose:
            print(f"[app:{self.id}] no frames processed", file=sys.stderr)


def run_app(app: App, argv: Optional[List[str]] = None) -> None:
    """Generic CLI entry an app's app.py calls from __main__.

    Wires argparse -> config -> sink -> app.run(). Keeps app.py thin.
    """
    ap = argparse.ArgumentParser(description=f"reCamera Pro app: {app.id}")
    ap.add_argument("--model", default=None,
                    help="path to .rknn model (omitted for CPU-only apps)")
    # conf/iou default to None: the effective config (manifest defaults overlaid
    # by config.json) supplies them. A value here is a MANUAL override that wins
    # over config.json -- used for on-device debugging, not by appmgr.
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--iou", type=float, default=None)
    ap.add_argument("--source", default="ffmpeg", choices=["ffmpeg", "snapshot"])
    ap.add_argument("--url", default=DEFAULT_SUB_STREAM)
    ap.add_argument("--sink", default="ws", choices=["ws", "stdout"])
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--n", type=int, default=0, help="stop after N frames (0=forever)")
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    # MQTT / Home Assistant fan-out (optional; default OFF). Normally driven by
    # appmgr via RECAMERA_MQTT_* env; these flags are for on-device manual test.
    ap.add_argument("--mqtt-host", default=None,
                    help="enable MQTT/HA output to this broker (adds to WS)")
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--mqtt-base-topic", default=None)
    ap.add_argument("--mqtt-discovery-prefix", default=None)
    args = ap.parse_args(argv)

    # Unified config load (kit-design / parameter hot-tuning): manifest
    # config_schema defaults overlaid by <app_dir>/config.json. Explicit CLI
    # --conf/--iou win over that (manual debugging override).
    from kit import config as _cfg
    app_dir = _cfg.app_dir_of(app)
    # Read the manifest ONCE and thread it through both consumers (effective
    # config overlay + output-sink assembly) instead of loading it twice.
    manifest = _cfg.load_manifest(app_dir)
    eff = _cfg.effective_config(app_dir, manifest=manifest)
    if args.conf is not None:
        eff["conf"] = args.conf
    if args.iou is not None:
        eff["iou"] = args.iou
    # Decide the loop shape FIRST: a mis-declared shape must fail before we open
    # sinks or a camera. Loop-owning apps get their setup() from start(), after
    # the config_schema auto-bind (see App.start).
    new_shape = _is_new_shape(app)
    if not new_shape:
        app.setup(eff)

    if args.sink == "stdout":
        primary: ResultSink = open_result_sink("stdout")
    else:
        primary = open_result_sink("ws", host=args.host, port=args.port,
                                   app_id=app.id)

    # Unified configurable output (internal/OUTPUT_SINK_SPEC.md §3). Apps that
    # declare `capabilities:["output"]` get channels/formatters/filters assembled
    # from the manifest `output` block + persisted config; apps that do NOT opt
    # in bypass this entirely and keep the legacy MQTT fan-out below unchanged.
    from kit.adapters.result_sink import MultiSink
    from kit.adapters.output_sink import assemble_output_sink
    out_sink, opted_in = assemble_output_sink(
        app, app_dir, manifest, eff, verbose=not args.quiet)

    sinks: List[ResultSink] = [primary]
    if opted_in:
        if out_sink is not None:
            sinks.append(out_sink)
    else:
        # Legacy path: optional MQTT / Home Assistant fan-out, enabled when a
        # broker host is supplied via appmgr's RECAMERA_MQTT_* env or --mqtt-host.
        # Fully best-effort: any failure here degrades to WS-only, never aborts.
        mqtt = _maybe_open_mqtt_sink(app, app_dir, args, verbose=not args.quiet)
        if mqtt is not None:
            sinks.append(mqtt)
    sink: ResultSink = MultiSink(sinks) if len(sinks) > 1 else sinks[0]
    try:
        if new_shape:
            # New shape (KIT_APP_SHAPE_SPEC §1): the app owns the loop; kit
            # supplies frames/pre/models/emit/tick via start().
            app.start(args.model, source=args.source, url=args.url, sink=sink,
                      n=args.n, every=args.every, verbose=not args.quiet,
                      app_dir=app_dir, manifest=manifest, config=eff)
            try:
                app.run()
            finally:
                app.finish()
        else:
            app.run(args.model, source=args.source, url=args.url, sink=sink,
                    n=args.n, every=args.every, verbose=not args.quiet)
    finally:
        sink.close()


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _maybe_open_mqtt_sink(app: "App", app_dir: str, args, *, verbose: bool):
    """Build an MqttSink from CLI flags (win) or RECAMERA_MQTT_* env, or None.

    Precedence: --mqtt-host flag > RECAMERA_MQTT_ENABLED+RECAMERA_MQTT_HOST env >
    disabled. HA entities come from the app manifest's `ha_entities` array.
    """
    env = os.environ
    host = args.mqtt_host or (env.get("RECAMERA_MQTT_HOST")
                              if _env_flag("RECAMERA_MQTT_ENABLED") else None)
    if not host:
        return None
    port = args.mqtt_port or int(env.get("RECAMERA_MQTT_PORT", "1883") or 1883)
    user = args.mqtt_user if args.mqtt_user is not None else env.get("RECAMERA_MQTT_USERNAME", "")
    pw = args.mqtt_pass if args.mqtt_pass is not None else env.get("RECAMERA_MQTT_PASSWORD", "")
    base = args.mqtt_base_topic or env.get("RECAMERA_MQTT_BASE_TOPIC", "recamera")
    prefix = args.mqtt_discovery_prefix or env.get("RECAMERA_MQTT_DISCOVERY_PREFIX", "homeassistant")

    from kit import config as _cfg
    manifest = _cfg.load_manifest(app_dir)
    entities = manifest.get("ha_entities") or []
    device_name = manifest.get("name") or app.name or "reCamera Pro"
    try:
        from kit.adapters.mqtt_sink import MqttSink
        return MqttSink(host=host, port=port, app_id=app.id, base_topic=base,
                        discovery_prefix=prefix, username=user or "", password=pw or "",
                        entities=entities, device_name=device_name, verbose=verbose)
    except Exception as e:  # never let MQTT setup break WS output
        if verbose:
            print(f"[app:{app.id}] MQTT sink disabled (setup error: {e})", flush=True)
        return None
