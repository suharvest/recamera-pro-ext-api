#!/usr/bin/env python3
"""02-inject-result -- 用 ResultSink 把结果注入固件。

注入的结果走与内建推理完全相同的三路分发（docs/ext/README.md §4）：
  1. OSD 叠加   -> 出现在 RTSP/预览的叠加层（按 source_id 哈希分配颜色）
  2. 录像       -> 进 vigil 录像队列，回放可见
  3. 推送       -> WS(127.0.0.1:8123 / /ws/inference/results) / MQTT / HTTP / UART

本示例每隔一段时间注入一次固定结果，方便你在 RTSP/WS 里持续看到它。
--task 选择注入的任务类型：detection / classification / tracking / keypoints。

API 逐一核实自 sdk/librecamera_ext/python/recamera_ext/__init__.py：
  - ResultSink(source_id, lib_path=None)                        (L232)
  - send_detections(pts_us, boxes)  box=(x1,y1,x2,y2,score,label[,class_id])  (L241)
  - send_classification(pts_us, items)  item=(score, class_id, label)         (L264)
  - send_tracking(pts_us, items)  item=(x1,y1,x2,y2,score,class_id,label,track_id) (L307)
  - send_keypoints(pts_us, instances)  inst=dict{points[,box,score,class_id,label]} (L328)
    points 元素=(x, y, score, keypoint_id)
  - send_segmentation(pts_us, items)   见 __init__.py L280（含 mask，示例从略）

约束（docs/ext/README.md §4.3）：
  - source_id 不能用保留字 "builtin"（会被拒 EAUTH）。
  - 限速：每连接 60 msg/s（burst 15）；单条 payload <= 64KB。别超过帧率发。
  - pts_us=0 表示不与具体帧关联（照常叠加/推送）；要与某帧对齐叠加时传 frame.pts_us。

运行：  python3 inject_result.py [--task detection|classification|tracking|keypoints]
                                 [--source-id my-app] [--interval 1.0] [--count 0]
需要：  设备上有 librecamera_ext.so.1 + 含扩展 API 的固件。
"""
import argparse
import time

from recamera_ext import ResultSink


def inject_detection(sink):
    # boxes: (x1, y1, x2, y2, score, label[, class_id]) 像素坐标, score 0..1
    sink.send_detections(pts_us=0, boxes=[
        (100, 80, 240, 300, 0.92, "person", 0),
        (320, 120, 420, 260, 0.75, "cup", 41),
    ])


def inject_classification(sink):
    # items: (score, class_id, label)  —— top-k 分类
    sink.send_classification(pts_us=0, items=[
        (0.88, 5, "cat"),
        (0.09, 3, "dog"),
    ])


def inject_tracking(sink):
    # items: (x1, y1, x2, y2, score, class_id, label, track_id)
    sink.send_tracking(pts_us=0, items=[
        (100, 80, 240, 300, 0.92, 0, "person", 7),
    ])


def inject_keypoints(sink):
    # instances: dict{ points:[(x,y,score,keypoint_id),...], 可选 box/score/class_id/label }
    # 无 "box" 则整个 object_info 组在 wire 上留空（仅关键点）。
    sink.send_keypoints(pts_us=0, instances=[
        {
            "box": (100, 80, 240, 300),
            "score": 0.9,
            "class_id": 0,
            "label": "person",
            "points": [
                (170, 100, 0.95, 0),   # 例：鼻
                (150, 130, 0.90, 1),   # 例：左眼
                (190, 130, 0.90, 2),   # 例：右眼
            ],
        },
    ])


TASKS = {
    "detection": inject_detection,
    "classification": inject_classification,
    "tracking": inject_tracking,
    "keypoints": inject_keypoints,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=sorted(TASKS), default="detection")
    ap.add_argument("--source-id", default="my-app",
                    help='标识来源；不能用保留字 "builtin"')
    ap.add_argument("--interval", type=float, default=1.0, help="两次注入间隔秒")
    ap.add_argument("--count", type=int, default=0, help="注入次数，0=一直发")
    args = ap.parse_args()

    fn = TASKS[args.task]
    with ResultSink(source_id=args.source_id) as sink:
        print("connected, source_id=%r  task=%s  (Ctrl-C 退出)"
              % (sink.source_id, args.task))
        n = 0
        while args.count == 0 or n < args.count:
            fn(sink)
            n += 1
            print("injected #%d" % n)
            time.sleep(args.interval)
    print("done, %d injection(s)." % n)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
