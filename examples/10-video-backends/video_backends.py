#!/usr/bin/env python3
"""10-video-backends -- 用第三方视频框架取帧，接进同一段推理代码。

同一段 letterbox + RKNN 推理 + 后处理 + 结果输出，四种取帧后端可切换：

    --backend kit       kit 原生 open_frame_source()（官方帧代理在就零拷贝 + RGA）
    --backend ffmpeg    自己起 ffmpeg 子进程拉 RTSP，读 rawvideo rgb24
    --backend gst       GStreamer（gi/PyGObject）uridecodebin -> appsink
    --backend opencv    cv2.VideoCapture(url, CAP_FFMPEG)

三个第三方后端都走 **rkipc 已经在推的 RTSP 流**（go2rtc，`127.0.0.1:5554`），
是旁路消费，不新开 VI 通道 —— 设备上同一时刻只能有一个进程占摄像头，
自己再开一路采集会撞 VPSS。取舍与前提见同目录 README.md。

三个第三方后端都实现 kit 的 `FrameSource` ABC、产出 kit 的 `Frame`，所以
下游（`letterbox` / `RknnModel.infer` / `detect.postprocess` / `ResultSink`）
一行不用改 —— 这就是"换后端"的接口位置。

运行（设备上，/userdata/rknnenv 环境）：
    python3 video_backends.py --probe                  # 只探测哪些后端可用
    python3 video_backends.py --backend ffmpeg --n 60
    python3 video_backends.py --backend gst --model yolo11n.rknn --n 60
    python3 video_backends.py --backend kit --model yolo11n.rknn --sink ws

需要：numpy；kit 在 PYTHONPATH 上（`PYTHONPATH=<KIT_PARENT> python3 video_backends.py`，
或把本文件放到仓库检出目录里跑）。--model 省略时不加载 RKNN，只跑取帧 + letterbox，
用来单独量后端本身的开销。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import Iterator, List, Optional

import numpy as np

# kit 的帧抽象：第三方后端只要产出 Frame，下游就认。
from kit.adapters.frame_source import DEFAULT_SUB_STREAM, Frame, FrameSource
from kit.runtime.preprocess import letterbox


# --------------------------------------------------------------------------- #
# 后端 1：ffmpeg 子进程（rawvideo 管道）
# --------------------------------------------------------------------------- #
class FfmpegPipeSource(FrameSource):
    """`ffmpeg -i <rtsp> -pix_fmt rgb24 -f rawvideo -` 的 stdout 按帧读。

    这就是 kit 默认后端 `kit/adapters/frame_source.py:FfmpegRtspSource` 的做法，
    此处重写一遍是为了让示例自包含、逐行可读。**生产环境直接用 kit 那个**
    （它带 ffprobe 探分辨率、SIGTERM 收尾、低延迟 flags）。

    拷贝次数：ffmpeg 内部 H.265 解码 + NV12->RGB 转换（1 次），管道内核缓冲
    (1 次)，Python 侧 read() 出的 bytes（1 次）。`np.frombuffer` 不再拷。
    """

    def __init__(self, url: str, width: int, height: int,
                 transport: str = "tcp", ffmpeg_bin: str = "ffmpeg"):
        self.url, self.w, self.h = url, int(width), int(height)
        self.transport, self.ffmpeg_bin = transport, ffmpeg_bin
        self._frame_bytes = self.w * self.h * 3
        self._proc: Optional[subprocess.Popen] = None

    def frames(self) -> Iterator[Frame]:
        cmd = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-rtsp_transport", self.transport, "-i", self.url,
               "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      bufsize=self._frame_bytes)
        try:
            stdout = self._proc.stdout
            while True:
                buf = bytearray()
                while len(buf) < self._frame_bytes:      # 管道按块给，必须读满
                    chunk = stdout.read(self._frame_bytes - len(buf))
                    if not chunk:
                        return                            # EOF / 解码器退出
                    buf.extend(chunk)
                arr = np.frombuffer(bytes(buf), dtype=np.uint8)
                yield Frame(data=arr.reshape(self.h, self.w, 3),
                            w=self.w, h=self.h, fmt="RGB", pts=time.monotonic())
        finally:
            self.close()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()


# --------------------------------------------------------------------------- #
# 后端 2：GStreamer（gi / PyGObject）
# --------------------------------------------------------------------------- #
class GstRtspSource(FrameSource):
    """`uridecodebin ! videoconvert ! appsink(format=RGB)`，同步 pull_sample。

    用 `uridecodebin` 而不是手搭 `rtspsrc ! rtph265depay ! h265parse ! decoder`：
    它按 caps 自动挑 depay/parse/decoder，缺哪个插件会在 set_state 时报出来，
    比手搭少踩一半的坑。要控制延迟就用 appsink 的 `max-buffers=1 drop=true`。

    拷贝次数：解码 + videoconvert 到 RGB（GStreamer 内部，1 次），
    `buf.map()` 出的内存我们必须 `.copy()`（1 次）——unmap 之后那块内存会被
    回收进 pool，不 copy 下一帧就被覆写。想省掉这次 copy 只能在 map 生命周期
    内把推理做完，代码会更难写。

    ⚠️ 本后端在 reCamera Pro 上是否可用取决于固件里装了哪些 GStreamer 插件
    （尤其 `rtspsrc` 和 H.265 解码器），见 README §前提。`--probe` 会告诉你。
    """

    PIPELINE = ("uridecodebin uri={url} ! videoconvert ! "
                "video/x-raw,format=RGB ! "
                "appsink name=out max-buffers=1 drop=true sync=false")

    def __init__(self, url: str, latency_ms: int = 100):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        # 只用 appsink 的 `pull-sample` action signal（GObject 层就能 emit），
        # 不碰 GstApp 的 Python 绑定，少一个 typelib 依赖。
        Gst.init(None)
        self._Gst = Gst
        self.url = url
        self.latency_ms = latency_ms
        self._pipe = Gst.parse_launch(self.PIPELINE.format(url=url))
        self._sink = self._pipe.get_by_name("out")
        self._stop = False

    def _shape_of(self, sample) -> tuple:
        """从 sample 的 caps 取 W/H；stride 用 GstVideoMeta（若有）兜底。"""
        s = sample.get_caps().get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        return int(w), int(h)

    def frames(self) -> Iterator[Frame]:
        Gst = self._Gst
        self._pipe.set_state(Gst.State.PLAYING)
        # PLAYING 是异步的：先等 preroll 完成，否则第一帧还没协商 caps。
        st = self._pipe.get_state(10 * Gst.SECOND)[0]
        if st != Gst.StateChangeReturn.SUCCESS:
            raise RuntimeError(
                "GStreamer pipeline 起不来（state=%s）。多半是缺 rtspsrc 或 "
                "H.265 解码插件，用 `gst-inspect-1.0 rtspsrc` 确认" % st)
        try:
            while not self._stop:
                sample = self._sink.emit("pull-sample")
                if sample is None:
                    break                                   # EOS
                buf = sample.get_buffer()
                w, h = self._shape_of(sample)
                ok, info = buf.map(Gst.MapFlags.READ)
                if not ok:
                    continue
                try:
                    stride = info.size // h                 # RGB 行可能有 padding
                    arr = np.frombuffer(info.data, dtype=np.uint8, count=info.size)
                    arr = arr.reshape(h, stride)[:, :w * 3].reshape(h, w, 3)
                    rgb = np.ascontiguousarray(arr)         # ★必须拷贝★
                finally:
                    buf.unmap(info)
                pts = (buf.pts / Gst.SECOND) if buf.pts != Gst.CLOCK_TIME_NONE \
                    else time.monotonic()
                yield Frame(data=rgb, w=w, h=h, fmt="RGB", pts=pts)
        finally:
            self.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._pipe.set_state(self._Gst.State.NULL)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 后端 3：OpenCV VideoCapture
# --------------------------------------------------------------------------- #
class OpenCvRtspSource(FrameSource):
    """`cv2.VideoCapture(url, cv2.CAP_FFMPEG)`，最短的一条。

    OpenCV 的 FFMPEG backend 内部就是 libav*，所以能力上界和 `--backend ffmpeg`
    一样，代价多一项：VideoCapture 出的是 **BGR**，喂 kit 的前处理要转 RGB
    （`[:, :, ::-1]` 是视图，`ascontiguousarray` 才是那次真实拷贝）。

    延迟：VideoCapture 内部有解码队列，`CAP_PROP_BUFFERSIZE=1` 只在部分后端
    生效；网络流上 read() 拿到的可能不是最新帧。要严格低延迟就用 ffmpeg/gst
    后端自己控 buffer。
    """

    def __init__(self, url: str, transport: str = "tcp"):
        import cv2
        self._cv2 = cv2
        # OpenCV 的 RTSP 传输方式只能经环境变量给 libav（没有 API 入口）。
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                              "rtsp_transport;%s" % transport)
        self.url = url
        self._cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not self._cap.isOpened():
            raise RuntimeError(
                "cv2.VideoCapture 打不开 %s；确认 cv2.getBuildInformation() 里 "
                "FFMPEG=YES 且流地址可达" % url)
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._stop = False

    def frames(self) -> Iterator[Frame]:
        try:
            while not self._stop:
                ok, bgr = self._cap.read()
                if not ok:
                    break
                rgb = np.ascontiguousarray(bgr[:, :, ::-1])   # ★BGR->RGB 拷贝★
                h, w = rgb.shape[:2]
                yield Frame(data=rgb, w=w, h=h, fmt="RGB", pts=time.monotonic())
        finally:
            self.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._cap.release()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 后端 0：kit 原生（对照组）
# --------------------------------------------------------------------------- #
def open_kit_source(url: str, input_size: int, hw_direct: bool) -> FrameSource:
    """kit 的取帧路径：经能力注册表选实现。

    官方帧代理 `/run/recamera/frame.sock` 在 → `OfficialFrameSource`（dma-buf
    零拷贝，`hw_direct=True` 时 RGA 直出模型尺寸 letterbox，`frame.model_info`
    非空，下游可跳过 Python letterbox）。socket 不在 → 回退成 ffmpeg 拉 RTSP，
    与 `--backend ffmpeg` 同路，但那时 **拿不到 RGA**（见 README）。
    """
    from kit.adapters.registry import select_frame_source
    return select_frame_source(
        url=url, prefer="ffmpeg",
        input_size=input_size if hw_direct else 0,
        direct_preprocess=hw_direct,
        hw_letterbox=False,
    )


# --------------------------------------------------------------------------- #
# 共用的那段"推理代码"（四种后端逐字相同）
# --------------------------------------------------------------------------- #
def prepare(frame: Frame, input_size: int):
    """letterbox 到模型输入尺寸。

    与 `kit/app.py:App.pre()` 同一套选路：帧源已经在 RGA 上做好 letterbox 时
    （`frame.model_info` 非空）直接用，不再走 Python。第三方后端永远走
    else 分支 —— 它们拿不到 dma-buf fd，RGA 路不成立。
    """
    info = getattr(frame, "model_info", None)
    padded = getattr(frame, "model_data", None)
    if padded is not None:
        return padded, info
    if info is not None:
        return frame.data, info                  # kit hw-direct
    return letterbox(frame.data, input_size)     # 其余全部走这里


def run_loop(src: FrameSource, model, input_size: int, conf: float, iou: float,
             n: int, sink=None, backend: str = "?") -> None:
    """取帧 -> 前处理 -> 推理 -> 后处理 -> 输出。四种后端共用。"""
    from kit.runtime.postprocess import detect as detect_post

    t_pre = t_inf = t_post = 0.0
    processed = 0
    t0 = time.monotonic()
    for frame in src.frames():
        a = time.monotonic()
        x, info = prepare(frame, input_size)
        b = time.monotonic()

        results: List[dict] = []
        if model is not None:
            outs = model.infer(x)
            c = time.monotonic()
            results = detect_post.postprocess(outs, info, conf_thres=conf,
                                              iou_thres=iou, input_size=input_size)
            d = time.monotonic()
        else:
            c = d = b                            # 无模型：只量取帧 + 前处理

        t_pre += b - a
        t_inf += c - b
        t_post += d - c
        processed += 1

        if sink is not None:
            sink.set_frame_size(frame.w, frame.h)
            sink.emit({"results": results, "events": [],
                       "inference_time_ms": round((c - b) * 1000, 2),
                       "stream_id": "camera-0"}, frame.pts)

        if processed % 30 == 0 or processed == 1:
            print("[%s] frame#%03d %dx%d dets=%d" %
                  (backend, processed, frame.w, frame.h, len(results)),
                  flush=True)
        if n and processed >= n:
            break

    wall = time.monotonic() - t0
    if not processed:
        print("[%s] 一帧都没拿到" % backend, file=sys.stderr)
        return
    print("\n[%s] %d 帧 / %.2f s = %.1f fps（含取帧等待）" %
          (backend, processed, wall, processed / wall))
    print("[%s] pre %.1f ms  infer %.1f ms  post %.1f ms（每帧均值）" %
          (backend, t_pre / processed * 1000, t_inf / processed * 1000,
           t_post / processed * 1000))


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def probe() -> None:
    """打印每个后端在**当前这台设备**上是否具备前提。不连流，只看依赖。"""
    print("== 后端可用性探测 ==")

    ff = shutil.which("ffmpeg")
    print("ffmpeg      : %s" % (ff or "缺 —— ffmpeg 不在 PATH"))
    if ff:
        try:
            ver = subprocess.check_output([ff, "-version"], text=True,
                                          stderr=subprocess.STDOUT)
            print("              %s" % ver.splitlines()[0])
        except Exception as e:
            print("              版本读不到（%s）" % e)

    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        missing = [name for name in ("uridecodebin", "rtspsrc", "videoconvert",
                                     "appsink")
                   if Gst.ElementFactory.find(name) is None]
        print("gstreamer   : gi OK, Gst %s" % Gst.version_string())
        print("              缺的 element: %s" % (missing or "无"))
        dec = [n for n in ("avdec_h265", "avdec_h264", "openh264dec",
                           "libde265dec", "mppvideodec")
               if Gst.ElementFactory.find(n) is not None]
        print("              可用解码器: %s" % (dec or "无 —— RTSP 路走不通"))
    except Exception as e:
        print("gstreamer   : 不可用（%s）" % e)

    try:
        import cv2
        info = cv2.getBuildInformation()
        line = next((ln.strip() for ln in info.splitlines()
                     if ln.strip().startswith("FFMPEG")), "FFMPEG: ?")
        print("opencv      : %s，%s" % (cv2.__version__, line))
    except Exception as e:
        print("opencv      : 不可用（%s）" % e)

    sock = os.environ.get("RECAMERA_FRAME_SOCK", "/run/recamera/frame.sock")
    print("kit 原生    : 帧代理 %s %s" %
          (sock, "在（零拷贝 + RGA 可用）" if os.path.exists(sock)
           else "不在（kit 会回退 ffmpeg RTSP，RGA 不可用）"))


# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backend", default="ffmpeg",
                    choices=["kit", "ffmpeg", "gst", "opencv"])
    ap.add_argument("--url", default=DEFAULT_SUB_STREAM,
                    help="RTSP 地址（默认 go2rtc sub 流 %(default)s）")
    ap.add_argument("--width", type=int, default=640,
                    help="ffmpeg 后端读 rawvideo 需要知道分辨率（sub 流 640x480）")
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--model", default=None, help=".rknn 路径；省略则只跑取帧+前处理")
    ap.add_argument("--input-size", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.45)
    ap.add_argument("--n", type=int, default=60, help="跑多少帧后停（0=不停）")
    ap.add_argument("--hw-direct", action="store_true",
                    help="仅 --backend kit：请求 RGA 直出模型尺寸 letterbox")
    ap.add_argument("--sink", default="none", choices=["none", "ws", "stdout"])
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--probe", action="store_true", help="只探测后端可用性后退出")
    args = ap.parse_args(argv)

    if args.probe:
        probe()
        return 0

    if args.backend == "kit":
        src = open_kit_source(args.url, args.input_size, args.hw_direct)
    elif args.backend == "ffmpeg":
        src = FfmpegPipeSource(args.url, args.width, args.height)
    elif args.backend == "gst":
        src = GstRtspSource(args.url)
    else:
        src = OpenCvRtspSource(args.url)
    print("[%s] source=%s url=%s" % (args.backend, type(src).__name__, args.url))

    model = None
    if args.model:
        from kit.runtime.engine import RknnModel
        model = RknnModel(args.model)

    sink = None
    if args.sink != "none":
        from kit.adapters.result_sink import open_result_sink
        sink = (open_result_sink("ws", port=args.port, app_id="video-backends")
                if args.sink == "ws" else open_result_sink("stdout"))

    try:
        run_loop(src, model, args.input_size, args.conf, args.iou, args.n,
                 sink=sink, backend=args.backend)
    finally:
        src.close()
        if model is not None:
            model.release()
        if sink is not None:
            sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
