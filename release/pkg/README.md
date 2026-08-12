# reCamera Pro Extension API — v1.2.0 (sideload package)

Persistent sideload that adds the extension API (frame proxy / result injection / probe) to a
reCamera Pro (RV1126B). It overwrites `/oem/usr/bin/rkipc` (ext4, read-write) so it **survives reboot**.
It is not a firmware image and does not touch partitions.

## What's inside
See `MANIFEST.txt` for md5s and install targets. In short: a patched `rkipc`, an optional `entry.cgi`
(M4 control plane), and the SDK (`librecamera_ext.so` + Python `recamera_ext` + C header).

## Flash a new device

Prereqs: device reachable over `adb` as root (`adb connect <ip>:5555`).

```sh
# from the machine holding this package (Mac/Linux):
adb shell "mkdir -p /userdata/ext-pkg"
adb push ./ /userdata/ext-pkg/            # push the whole package dir
adb shell "sh /userdata/ext-pkg/install.sh"   # backs up factory, md5-verifies, overwrites /oem
adb reboot                                    # or: install.sh --reboot
# wait ~1-2 min, then self-check:
adb shell "ls -l /run/recamera/"          # expect frame.sock result-in.sock probe.sock apps.d/
adb shell "md5sum /oem/usr/bin/rkipc"     # expect de5b3aa41ba5dd02968632823aac29cf
```

`install.sh` is idempotent and md5-checked: it refuses to run with a mismatched artifact, backs up the
factory `rkipc`/`entry.cgi` to `/userdata/*.factory.bak` exactly once, and aborts before overwriting if
no valid rollback target exists.

## Verify the SDK (anyone can connect)

```sh
export LD_LIBRARY_PATH=/oem/usr/lib:$LD_LIBRARY_PATH
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
python3 - <<'PY'
import recamera_ext as re
s = re.ResultSink("selftest")                 # opens /run/recamera/result-in.sock (handshake)
print("rc =", s.send_detections(123456, [(10,20,110,220,0.9,"person",0)]))  # 0 = accepted
PY
```

## Rollback

```sh
adb shell "sh /userdata/ext-pkg/rollback.sh --reboot"   # restores factory rkipc + entry.cgi, reboots
```

## Boundaries
- **OTA reverts this.** A firmware OTA / `update.img` flash rewrites `/oem` and restores factory `rkipc`.
  Re-run `install.sh` after any OTA.
- **Firmware compat:** V1.0.4 and V1.0.10 (cross-build compatible).
- **Resolved:** keypoints *decode over WebSocket* now works in this `rkipc` (de5b3aa4), alongside
  classification box OSD rendering. frame/result/probe/keypoints paths are all active.
