"""Reusable, app-agnostic logic helpers (geometry + temporal state machines).

These are deliberately model-free: they operate on decoded pose dicts
(kit.runtime.postprocess.pose output) and plain floats so that fall-detection,
fitness-trainer, and future pose apps share one implementation.
"""
