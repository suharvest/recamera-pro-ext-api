#!/usr/bin/env python3
"""01-hello-frame -- 用 FrameSource 从摄像头拿几帧，存成灰度图。

最小取帧示例。核心只有 for 循环里那几行；其余是存盘和命令行参数。

API 逐一核实自 sdk/librecamera_ext/python/recamera_ext/__init__.py：
  - FrameSource(config=None, timeout_ms=1000, lib_path=None)   (L476)
  - 属性 src.width/height/fourcc/pool_depth/max_outstanding    (L488-489)
  - for frame in src: -> Frame                                 (L498-511)
  - frame.array   零拷贝 Y 平面视图 (height, width) uint8       (L441-449)
  - frame.seq / frame.pts_us / frame.dropped                   (L398-407)

运行：  python3 hello_frame.py [-n 张数] [-o 输出目录]
需要：  numpy；设备上有 librecamera_ext.so.1 + 含扩展 API 的固件。
"""
import argparse
import os

from recamera_ext import FrameSource


def save_pgm(path, y_view):
    """把灰度 Y 平面存成 PGM（P5，二进制），无需 opencv/PIL。
    y_view 是零拷贝视图，tobytes() 会拷出连续副本，安全。"""
    import numpy as np

    arr = np.ascontiguousarray(y_view)   # 视图 -> 连续内存（一次拷贝）
    h, w = arr.shape
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (w, h))
        f.write(arr.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="要存的帧数")
    ap.add_argument("-o", default=".", help="输出目录")
    args = ap.parse_args()
    os.makedirs(args.o, exist_ok=True)

    # 打开帧源：默认订阅 NPU 同款分辨率/格式（NV12）。
    with FrameSource() as src:
        print("subscribed: %dx%d fourcc=0x%08x pool_depth=%d max_outstanding=%d"
              % (src.width, src.height, src.fourcc, src.pool_depth, src.max_outstanding))

        saved = 0
        for frame in src:
            # frame.array 是灰度 Y 平面的零拷贝视图 (height, width)。
            # 只在本次迭代内有效——下一次迭代开始后底层内存会被覆写。
            # 这里 save_pgm 内部 ascontiguousarray 已拷出，安全存盘。
            out = os.path.join(args.o, "frame_%05d.pgm" % frame.seq)
            save_pgm(out, frame.array)
            print("saved %s  (seq=%d pts_us=%d dropped=%s)"
                  % (out, frame.seq, frame.pts_us, frame.dropped))

            saved += 1
            if saved >= args.n:
                break

    print("done, %d frame(s) saved." % saved)


if __name__ == "__main__":
    main()
