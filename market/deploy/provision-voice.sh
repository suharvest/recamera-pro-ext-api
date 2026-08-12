#!/bin/sh
# provision-voice.sh -- one-time (idempotent) runtime provisioning for the 9th
# App Center app, `voice-transcribe`, so it actually runs on device alongside the
# 8 vision apps. It is the audio counterpart to provision-runtime.sh (which fixes
# recamera_ext importability for every app); this script adds the AUDIO runtime
# the vision apps never needed:
#
#   (1) Python audio deps INTO the rknn venv /userdata/rknnenv (the interpreter
#       voice-transcribe's manifest points at). The venv ships only numpy +
#       rknn-toolkit-lite2; voice additionally needs:
#           voxedge            pure-python ASR backend abstraction (kit/asr.py)
#           sherpa_onnx        streaming KWS wake word + silero VAD endpointing
#           kaldi_native_fbank fbank frontend for the NPU (rk) SenseVoice decode
#           sentencepiece      BPE detokenize for the NPU SenseVoice decode
#       (numpy + rknnlite are already present and reused.)
#
#   (2) The ASR model set into the SHARED dir /userdata/local/models/asr/ (models
#       live outside the app package -- hundreds of MB, shared across voice apps;
#       see apps/voice-transcribe/app.py model resolution order). For the NPU
#       (rk) ASR backend + streaming-KWS wake + silero VAD this is:
#           sensevoice_rv1126b_w4a16.rknn          w4a16 SenseVoice encoder (127M)
#           am.mvn / embedding.npy                 CMVN + prompt embeddings
#           chn_jpn_yue_eng_ko_spectok.bpe.model   sentencepiece model
#           silero_vad.onnx                        silero v5 VAD (endpointing)
#           kws/{encoder,decoder,joiner}.int8.onnx gigaspeech 3.3M streaming KWS
#           kws/tokens.txt  kws/keywords.txt       KWS tokens + "HELLO CAMERA"
#
#   (3) config.json for voice-transcribe selecting the NPU ASR backend
#       (asr_backend=rk). The manifest default is the CPU sherpa SenseVoice
#       (model.int8.onnx) which we do NOT ship here; the NPU w4a16 path reuses
#       the already-on-device rknnlite and the staged .rknn, so we pin it. NOTE:
#       `asr_backend` is an internal key (not in config_schema), so it is written
#       to config.json directly -- appmgr's schema validator would reject it, and
#       a later UI config-set would drop it (re-run this script if that happens).
#
# PAYLOAD LAYOUT (this script + its data are copied to the device together):
#     <payload>/provision-voice.sh   (this file)
#     <payload>/wheels/*.whl         (aarch64 cp311 glibc wheels + voxedge)
#     <payload>/models/asr/...       (the model set above)
# Payload dir defaults to the script's own directory; override with $1 or
# $VOICE_PAYLOAD.
#
# Idempotent: re-runnable any number of times. Installs are skipped when the
# import already succeeds; model copies are skipped when the file is already in
# place with the same size. Prints a PASS/FAIL summary and exits non-zero if a
# hard prerequisite is missing.
#
# BusyBox/POSIX-sh compatible (device shell). Run on the device:
#     sh <payload>/provision-voice.sh
set -u

SELF_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
PAYLOAD=${1:-${VOICE_PAYLOAD:-$SELF_DIR}}
WHEELS="$PAYLOAD/wheels"
MODELS_SRC="$PAYLOAD/models/asr"

RKNNENV=/userdata/rknnenv
VENV_PY="$RKNNENV/bin/python"
MODELS_DST=/userdata/local/models/asr
APP_DIR=/userdata/local/apps/voice-transcribe
CONFIG_JSON="$APP_DIR/config.json"

rc=0
ok()   { echo "[voice-prov] OK   $*"; }
warn() { echo "[voice-prov] WARN $*"; }
fail() { echo "[voice-prov] FAIL $*"; rc=1; }

# --- (0) prerequisites ------------------------------------------------------ #
if [ -x "$VENV_PY" ] || [ -L "$VENV_PY" ]; then
    ok "rknn venv interpreter: $VENV_PY ($("$VENV_PY" -V 2>&1))"
else
    fail "rknn venv interpreter missing: $VENV_PY"
    echo "[voice-prov] cannot proceed without the venv"; exit 1
fi
for b in ffmpeg arecord; do
    if command -v "$b" >/dev/null 2>&1; then
        ok "audio tool present: $b ($(command -v $b))"
    else
        fail "audio tool missing: $b (ai_asr/rtsp capture needs it)"
    fi
done

# --- (1) python audio deps into the venv ------------------------------------ #
# Install ONLY what is missing; each import-test gates its (re)install so the
# script is cheap to re-run. We install from the local wheelhouse (no network).
need_pkg() {  # $1 = import name
    "$VENV_PY" -c "import $1" >/dev/null 2>&1
}
install_wheelhouse() {
    if [ ! -d "$WHEELS" ]; then
        fail "wheelhouse not found: $WHEELS"; return 1
    fi
    # --no-deps + offline wheelhouse. These need only numpy (already in the venv).
    # sherpa_onnx_core ships the native libs (libonnxruntime.so + libsherpa-onnx-*)
    # that the sherpa_onnx pybind links -- it is a HARD requirement of sherpa_onnx
    # 1.13.x (Requires-Dist: sherpa-onnx-core==1.13.4) and must be installed too,
    # else `import sherpa_onnx` fails with "libonnxruntime.so: cannot open ...".
    "$VENV_PY" -m pip install --no-index --no-deps --find-links "$WHEELS" \
        voxedge sherpa_onnx sherpa_onnx_core kaldi_native_fbank sentencepiece 2>&1 \
        | sed 's/^/[voice-prov]   pip: /'
}
_missing=""
for mod in voxedge sherpa_onnx kaldi_native_fbank sentencepiece; do
    need_pkg "$mod" || _missing="$_missing $mod"
done
if [ -n "$_missing" ]; then
    warn "missing python modules:$_missing -> installing from $WHEELS"
    install_wheelhouse
else
    ok "python audio deps already present (voxedge/sherpa_onnx/kaldi_native_fbank/sentencepiece)"
fi

# --- (2) model set into the shared dir -------------------------------------- #
mkdir -p "$MODELS_DST/kws" 2>/dev/null
copy_model() {  # $1 = relative path under models/asr
    src="$MODELS_SRC/$1"; dst="$MODELS_DST/$1"
    if [ ! -f "$src" ]; then fail "model payload missing: $src"; return 1; fi
    if [ -f "$dst" ] && [ "$(wc -c <"$src")" = "$(wc -c <"$dst")" ]; then
        ok "model already in place: $1"; return 0
    fi
    mkdir -p "$(dirname "$dst")" 2>/dev/null
    if cp "$src" "$dst" 2>/dev/null; then ok "staged model: $1"; else fail "copy failed: $1"; fi
}
# REQUIRED models (rk NPU ASR + silero VAD). These gate a hard PASS.
for m in sensevoice_rv1126b_w4a16.rknn am.mvn embedding.npy \
         chn_jpn_yue_eng_ko_spectok.bpe.model silero_vad.onnx; do
    copy_model "$m"
done
# OPTIONAL KWS model set (streaming wake word). If the gigaspeech encoder/decoder/
# joiner onnx are staged we install them and use wake_backend=kws; otherwise the
# app falls back to wake_backend=asr (VAD+ASR keyword match -- no extra model).
copy_model_opt() {  # $1 = relative path; missing payload is a WARN, not a FAIL
    src="$MODELS_SRC/$1"; dst="$MODELS_DST/$1"
    [ -f "$src" ] || { warn "optional model not staged: $1"; return 1; }
    if [ -f "$dst" ] && [ "$(wc -c <"$src")" = "$(wc -c <"$dst")" ]; then
        ok "model already in place: $1"; return 0
    fi
    mkdir -p "$(dirname "$dst")" 2>/dev/null
    cp "$src" "$dst" 2>/dev/null && ok "staged model: $1" || { warn "copy failed: $1"; return 1; }
}
KWS_OK=1
for m in kws/encoder.int8.onnx kws/decoder.int8.onnx kws/joiner.int8.onnx \
         kws/tokens.txt kws/keywords.txt; do
    copy_model_opt "$m" || KWS_OK=0
done
if [ "$KWS_OK" = 1 ]; then
    WAKE_BACKEND=kws; ok "KWS model present -> wake_backend=kws (streaming wake word)"
else
    WAKE_BACKEND=asr; warn "KWS model incomplete -> wake_backend=asr (VAD+ASR keyword match; no extra model)"
fi

# --- (2b) satisfy hardcoded staging-path defaults --------------------------- #
# kit.logic.vad.DEFAULT_VAD_MODEL and kit.logic.wakeword.DEFAULT_KWS_DIR point at
# the feasibility-spike staging dir /userdata/tmp/asr, NOT the shared model dir.
# In wake_backend=asr the AsrKeywordWakeWord builds an internal VadSegmenter()
# with NO model arg -> it loads /userdata/tmp/asr/silero_vad.onnx. Mirror the VAD
# (and keywords) there via symlinks so those defaults resolve without duplicating
# the 127M rknn. (Harmless when wake_backend=kws.)
STAGING_DIR=/userdata/tmp/asr
mkdir -p "$STAGING_DIR/kws" 2>/dev/null
if ln -sf "$MODELS_DST/silero_vad.onnx" "$STAGING_DIR/silero_vad.onnx" 2>/dev/null; then
    ok "linked default VAD path: $STAGING_DIR/silero_vad.onnx -> $MODELS_DST/silero_vad.onnx"
else
    warn "could not link $STAGING_DIR/silero_vad.onnx (default VAD path)"
fi
[ -f "$MODELS_DST/kws/keywords.txt" ] && ln -sf "$MODELS_DST/kws/keywords.txt" "$STAGING_DIR/kws/keywords.txt" 2>/dev/null

# --- (3) pin the NPU ASR backend in config.json ----------------------------- #
# Merge {"asr_backend":"rk"} into config.json without clobbering other keys.
export WAKE_BACKEND="${WAKE_BACKEND:-asr}"
if [ -d "$APP_DIR" ]; then
    if "$VENV_PY" - "$CONFIG_JSON" <<'PY'
import json, os, sys
p = sys.argv[1]
try:
    with open(p) as f: cfg = json.load(f)
    if not isinstance(cfg, dict): cfg = {}
except Exception:
    cfg = {}
cfg["asr_backend"] = "rk"
cfg["wake_backend"] = os.environ.get("WAKE_BACKEND", "asr")
tmp = p + ".tmp"
with open(tmp, "w") as f: json.dump(cfg, f, indent=2, ensure_ascii=False)
os.replace(tmp, p)
print("wrote " + p + " -> asr_backend=rk")
PY
    then ok "config.json pinned asr_backend=rk wake_backend=$WAKE_BACKEND"; else fail "could not write $CONFIG_JSON"; fi
else
    fail "app dir missing: $APP_DIR (is voice-transcribe installed?)"
fi

# --- self-test: imports + model presence ------------------------------------ #
echo "[voice-prov] ---- self-test ----"
if "$VENV_PY" - <<PY
import sys
mods = ["voxedge", "sherpa_onnx", "kaldi_native_fbank", "sentencepiece", "numpy"]
try:
    import importlib
    for m in mods:
        importlib.import_module(m)
    from rknnlite.api import RKNNLite  # already-on-device NPU runtime
    print("[voice-prov]   imports OK: " + ", ".join(mods) + ", rknnlite")
except Exception as e:
    print("[voice-prov]   IMPORT FAILED: %r" % (e,)); sys.exit(1)
import os
need = ["sensevoice_rv1126b_w4a16.rknn","am.mvn","embedding.npy",
        "chn_jpn_yue_eng_ko_spectok.bpe.model","silero_vad.onnx"]
missing = [n for n in need if not os.path.exists(os.path.join("$MODELS_DST", n))]
if missing:
    print("[voice-prov]   REQUIRED MODELS MISSING: " + ", ".join(missing)); sys.exit(1)
kws = ["kws/encoder.int8.onnx","kws/decoder.int8.onnx","kws/joiner.int8.onnx","kws/tokens.txt"]
kws_missing = [n for n in kws if not os.path.exists(os.path.join("$MODELS_DST", n))]
print("[voice-prov]   required model set complete under $MODELS_DST"
      + ("" if not kws_missing else "  (KWS optional set absent -> wake_backend=asr)"))
PY
then ok "self-test passed (imports + model set)"; else fail "self-test failed"; fi

echo "[voice-prov] ---------------------------------------------"
if [ "$rc" -eq 0 ]; then
    echo "[voice-prov] RESULT: PASS -- voice-transcribe runtime is provisioned."
    echo "[voice-prov] Start it via appmgr (single-active, from /userdata/local):"
    echo "[voice-prov]     cd /userdata/local && python3 -m appmgr start voice-transcribe"
else
    echo "[voice-prov] RESULT: FAIL -- see FAIL lines above."
fi
exit "$rc"
