"""Manifest `models[].classes` -> App.class_names (RENDER_DECLARATION_SPEC §5 P0-3).

Before this, `kit/app.py` hard-set `class_names = COCO80` in __init__ and the
manifest field had no consumer at all -- a custom-trained model's labels could
only be hand-written inside the app's Python.

Anti-vacuous-pass discipline: every "resolved" assertion also asserts the result
is NOT COCO80's head, so a silent fallback can never masquerade as a pass.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kit.app import App, BUILTIN_CLASS_TABLES, resolve_class_names  # noqa: E402
from kit.runtime.postprocess.detect import COCO80  # noqa: E402

COCO_HEAD = COCO80[:3]          # ["person", "bicycle", "car"]


def _bind(app, decls, app_dir=None):
    """Drive only the class_names binding step of start()."""
    app._bind_class_names(list(decls), app_dir)
    return list(app.class_names)


# --------------------------------------------------------------------------- #
# 1. built-in table name
# --------------------------------------------------------------------------- #
def test_builtin_table_name():
    names = _bind(App(), [{"id": "m", "classes": "coco80"}])
    assert names == list(COCO80)
    assert "coco80" in BUILTIN_CLASS_TABLES


def test_builtin_table_name_is_case_insensitive():
    assert resolve_class_names("COCO80") == list(COCO80)


# --------------------------------------------------------------------------- #
# 2. literal array
# --------------------------------------------------------------------------- #
def test_literal_array():
    names = _bind(App(), [{"id": "m", "classes": ["cat", "dog", "duck"]}])
    assert names == ["cat", "dog", "duck"]
    # anti-vacuous: proves we did NOT silently fall back to COCO80
    assert names[:3] != COCO_HEAD
    assert len(names) != len(COCO80)


def test_literal_array_single_label():
    names = _bind(App(), [{"id": "m", "classes": ["face"]}])
    assert names == ["face"]
    assert names[:3] != COCO_HEAD


# --------------------------------------------------------------------------- #
# 3. in-package file
# --------------------------------------------------------------------------- #
def test_labels_file_txt(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "labels.txt").write_text(
        "# my labels\n\nscrew\nbolt\n  washer  \n\n", encoding="utf-8")
    names = _bind(App(), [{"id": "m", "classes": "models/labels.txt"}], str(tmp_path))
    assert names == ["screw", "bolt", "washer"]
    assert names[:3] != COCO_HEAD          # anti-vacuous


def test_labels_file_json(tmp_path):
    (tmp_path / "labels.json").write_text(json.dumps(["alpha", "beta"]), encoding="utf-8")
    names = _bind(App(), [{"id": "m", "classes": "labels.json"}], str(tmp_path))
    assert names == ["alpha", "beta"]
    assert names[:3] != COCO_HEAD


# --------------------------------------------------------------------------- #
# failure modes -> fall back to COCO80, never raise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", [
    "coco9999",                 # unknown built-in table
    "models/missing.txt",       # file not in package
    "../../etc/passwd",         # traversal outside the app dir
    "/etc/passwd",              # absolute path
    [],                         # empty array
    {"not": "supported"},       # wrong type
    123,                        # wrong type
])
def test_unresolvable_falls_back_to_coco80(spec, tmp_path, capsys):
    app = App()
    names = _bind(app, [{"id": "m", "classes": spec}], str(tmp_path))
    assert names == list(COCO80)                 # fallback, app still starts
    assert "manifest classes" in capsys.readouterr().out   # and it said why


def test_empty_labels_file_falls_back(tmp_path, capsys):
    (tmp_path / "labels.txt").write_text("# only comments\n\n", encoding="utf-8")
    names = _bind(App(), [{"id": "m", "classes": "labels.txt"}], str(tmp_path))
    assert names == list(COCO80)
    assert "no labels" in capsys.readouterr().out


def test_bad_json_falls_back(tmp_path, capsys):
    (tmp_path / "labels.json").write_text("{not json", encoding="utf-8")
    names = _bind(App(), [{"id": "m", "classes": "labels.json"}], str(tmp_path))
    assert names == list(COCO80)
    assert "not valid JSON" in capsys.readouterr().out


def test_traversal_is_refused_even_when_target_exists(tmp_path, capsys):
    outside = tmp_path / "secret.txt"
    outside.write_text("leaked\n", encoding="utf-8")
    appdir = tmp_path / "app"
    appdir.mkdir()
    names = _bind(App(), [{"id": "m", "classes": "../secret.txt"}], str(appdir))
    assert names == list(COCO80)
    assert "leaked" not in names
    assert "escapes the app dir" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# precedence: app declaration > manifest > COCO80
# --------------------------------------------------------------------------- #
class _AppWithOwnLabels(App):
    id = "own-labels"
    class_names = ["widget", "gadget"]


def test_app_class_attribute_wins_over_manifest():
    names = _bind(_AppWithOwnLabels(), [{"id": "m", "classes": ["cat", "dog"]}])
    assert names == ["widget", "gadget"]
    assert names[:3] != COCO_HEAD


def test_app_instance_assignment_wins_over_manifest():
    app = App()
    app.class_names = ["only-this"]
    names = _bind(app, [{"id": "m", "classes": "coco80"}])
    assert names == ["only-this"]


def test_no_manifest_classes_keeps_coco80_default():
    assert _bind(App(), [{"id": "m"}]) == list(COCO80)
    assert _bind(App(), []) == list(COCO80)


def test_base_app_default_is_still_coco80():
    # the class attribute replaced an __init__ assignment -- make sure plain
    # attribute access still yields the labels legacy apps relied on.
    assert list(App().class_names) == list(COCO80)


# --------------------------------------------------------------------------- #
# shipped manifests keep resolving
# --------------------------------------------------------------------------- #
_APPS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "apps")


def test_shipped_manifests_resolve():
    seen = {}
    for app_id in sorted(os.listdir(_APPS)):
        mp = os.path.join(_APPS, app_id, "manifest.json")
        if not os.path.isfile(mp):
            continue
        with open(mp) as f:
            man = json.load(f)
        decls = man.get("models") or []
        if not decls or decls[0].get("classes") is None:
            continue
        names = resolve_class_names(decls[0]["classes"], os.path.join(_APPS, app_id),
                                    who=app_id)
        assert names, f"{app_id}: models[0].classes failed to resolve"
        seen[app_id] = names
    # yolo-detector/retail-vision declare "coco80"; the face apps declare ["face"]
    assert seen, "no shipped manifest declares models[0].classes"
    assert seen.get("yolo-detector") == list(COCO80)
    assert seen.get("face-analysis") == ["face"]
    assert seen.get("facemesh-reader") == ["face"]
