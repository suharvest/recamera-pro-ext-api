# librecamera_ext — reCamera Pro 扩展 API SDK(权威发布源)

> **这里是 SDK 的唯一权威来源。** 文件系统里其他任何 `recamera_ext` / `librecamera_ext` / `sdk_work` 副本(如 `recamera_rk/m2_scratch/sdk_work`、`recamera_rk/_stage_3b`)都是**开发过程的过时临时快照,已废弃,勿用**——它们可能只含 `send_detections`,缺 pose/分类/关键点。以本目录为准。

## 内容
- `include/recamera_ext.h` — C ABI v1(冻结)
- `lib/librecamera_ext.so.1.0.0` (+ soname 软链) — aarch64,链 libprotobuf-c
- `python/recamera_ext/__init__.py` — Python 封装(ctypes)
- `CMakeLists.txt` — 参考构建
- `VERSION` — 版本/固件/能力对照

## 结果注入(全套任务类型)
`rc_ext_result_send_{detections,classification,segmentation,tracking,keypoints}`
Python: `ResultSink(source_id).send_{detections,classification,tracking,keypoints,segmentation}(...)`
→ 上官方 OSD(检测框/关键点/分类标签/跟踪)+ 录像 + WS/MQTT/HTTP/UART。
（注:keypoints/classification 的 OSD 绘制代码已在 osd_infer.c,真机端到端验证待补。）

## 帧代理
`FrameSource` / `rc_ext_frame_*` 连 `/run/recamera/frame.sock`。
**帧源给 VI 原始帧(全分辨率,不预 letterbox)**——letterbox/resize/量化等预处理由你的管线负责(后处理坐标映射、级联 ROI、OSD 叠加都需要原图)。
`frame.array` 零拷贝视图,只在当前迭代有效,跨帧保留必须 `.copy()`。

## 部署到设备
- Python: 把 `python/recamera_ext/` 拷到设备 site-packages 或 PYTHONPATH;`lib/librecamera_ext.so.1*` 拷到 `/oem/usr/lib` 或设 LD_LIBRARY_PATH。
- C: `-I include -L lib -lrecamera_ext`,交叉工具链 `aarch64-rockchip1240-linux-gnu-`。
- 需运行含扩展 API 的固件(见 VERSION)。
