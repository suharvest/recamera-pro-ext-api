# reCamera Pro RGA preprocess A/B

Date: 2026-08-13 (Asia/Shanghai)

Target: `recamera-pro-test` (`192.168.42.1`), RV1126B, firmware `V1.0.4`,
kernel `6.1.157`, 1280x720 NV12 frame broker

`librga.so` SHA256: `2fc6f8bb5c31f12f35fc0c85e365860c33bc6772a3bfa492a54f89b7226fa09c`

Final deployed code hashes: `frame_source.py`
`1b1dde5844c325192e3186f8b03ff64866b41e15ddcba78b8055faaf32ab50f7`,
`_rga.py` `9b1000d599efc8302b28bb605800c8a1f0b35353385452a1ccf344b27fc63d2e`,
`official.py` `34231ba125f4532d4ce2a75a3be966c4b1c7bdb4d6bdde828ad2a212965696f9`,
and `app.py` `76a88456552cf8132155fd0483d4115f176a3d8efdd4d6e46352a25b2e2e809e`.

The test used the official `appMgr` single-active API. `retail-vision` was
captured before the test, then stopped before each `fall-detection` run. No
second camera/NPU app was started.

## Result

| Path | E2E WS fps (12.02 s) | appMgr metrics fps | preprocess | RKNN infer | postprocess |
|---|---:|---:|---:|---:|---:|
| RGA direct (NV12 resize -> RGB 640x360 -> 114 gray pad) | 18.13 | 17.5–18.9 | 0.0 ms | 42.0–45.3 ms | 1.9–2.4 ms |
| Full-RGB fallback (RGA NV12->RGB at 1280x720, Python letterbox) | 12.14 | 11.4–12.4 | 38.2–43.1 ms | 35.3–37.9 ms | 1.3–1.6 ms |

The direct path improves end-to-end throughput by approximately 49.3% on this
run and removes about 40 ms of Python preprocessing per frame. The fallback is
kept as the safe default whenever the two-stage RGA ABI or operation fails.

## Direct-path evidence

The device log contains:

```text
[OfficialFrameSource] preprocess backend: RGA (hardware)
[OfficialFrameSource] preprocess path: RGA direct NV12 resize 1280x720 -> RGB 640x640 + gray pad
```

There was no `direct preprocess failed`, `RGA convert failed`, traceback, VPSS
FIFO error, or kernel Oops in the run.

## Coordinate/pixel contract

`Frame.w`/`Frame.h` remain `1280x720`; only `Frame.data` is model-sized
`640x640`. `Frame.model_info` is a `LetterboxInfo` transform with
`scale=0.5`, `pad_w=0`, `pad_h=140`, `orig_w=1280`, `orig_h=720`. The existing
postprocessor therefore maps boxes/keypoints back to original pixels and the
existing result sink still normalizes against 1280x720. Gray padding is exactly
`(114,114,114)`.

## Recovery

After the A/B runs, the official API was used to activate `retail-vision` again.
Final state: `active_app=retail-vision`, PID `1631`, `rkipc` PID `961`, NPU 1%,
RAM 735.9/1986 MB (37.1%), temperature 51.4 C. Retail log continued emitting
frames and detections.

Raw local evidence is retained under `/tmp/edgefallkit-pro-ab/`:
`direct-ws-metrics.json`, `fallback-ws-metrics.json`, `direct-explicit.log`,
`direct-grep.log`, `fallback-grep.log`, `post-restore-state.txt`, and
`post-restore-health.txt`.

## Reproduction

```sh
uv run --with numpy --with pytest pytest -q kit/adapters apps/fall-detection/test_fall_detection.py
```

The run passed 71 tests. The source-side implementation is in
`kit/adapters/_rga.py`, `kit/adapters/official.py`, `kit/adapters/frame_source.py`
and `kit/app.py`; only `fall-detection` opts into the direct model-frame path in
this first release.
