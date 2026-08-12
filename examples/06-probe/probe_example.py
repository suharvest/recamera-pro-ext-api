#!/usr/bin/env python3
"""06-probe -- 用 ProbeSource 观测内建推理流水线各级张量/指标。

probe 是"只读观测面"：不改流水线、不注入结果，只从 /run/recamera/probe.sock
订阅内建推理各级（stage）的采样。典型 stage：

  metrics       每帧/周期的指标 JSON（小，走 inline payload）
  preproc.out   预处理输出张量（大，走 memfd + mmap 零拷贝）
  npu.raw       NPU 原始输出
  postproc.out  后处理输出

传输方式（SDK 自动处理，你只管读 sample）：
  - inline：小样本（如 metrics JSON）直接随消息带 payload 字节；
  - memfd ：大张量经 SCM_RIGHTS 传 fd，SDK mmap 成只读视图（普通 mmap，非 dma-buf）。
  两种都通过 sample.payload（bytes 拷贝）/ sample.array（numpy 视图）统一访问。

API 逐一核实自 sdk/python/recamera_ext/__init__.py：
  - ProbeSource(stages, sample_every=1, timeout_ms=1000, lib_path=None)  (L697)
  - 迭代产出 ProbeSample: .stage_id/.seq/.pts_us/.payload/.payload_len/.meta/.array (L625-682)
  - ps.subscribed_mask / ps.sample_every                                 (L713-715)
  - meta（有张量元信息时）: shape/dtype/width/height/stride/scale/zero_point (L642-652)

运行：  python3 probe_example.py [--stage metrics] [--count 5] [--sample-every 1]
需要：  librecamera_ext.so.1（>=1.2.0，含 probe 符号）+ 含扩展 API 的固件；
        --stage preproc.out 演示读张量 meta 时需要 numpy。
"""
import argparse


def sample_stage(stage, count, sample_every):
    from recamera_ext import ProbeSource

    with ProbeSource(stages=[stage], sample_every=sample_every) as ps:
        print("subscribed stage=%r  mask=0x%x  sample_every=%d  (Ctrl-C 退出)"
              % (stage, ps.subscribed_mask, ps.sample_every))
        n = 0
        for s in ps:
            n += 1
            # 每条 sample：stage_id / seq / pts_us / payload 长度。
            print("[%d] stage=%s seq=%d pts=%d payload_len=%d%s"
                  % (n, s.stage_id, s.seq, s.pts_us, s.payload_len,
                     "  (dropped)" if s.dropped else ""))
            # 有张量元信息（多为大张量走 memfd）时打印 shape / 分辨率。
            if s.meta is not None:
                m = s.meta
                print("    meta shape=%s dtype=%d %dx%d stride=%d scale=%.4f zp=%d"
                      % (m["shape"], m["dtype"], m["width"], m["height"],
                         m["stride"], m["scale"], m["zero_point"]))
                # s.array -> 按 meta 定型的 numpy 零拷贝视图（本次迭代内有效）。
                arr = s.array
                print("    array shape=%s dtype=%s" % (arr.shape, arr.dtype))
            if n >= count:
                break
    print("done, %d sample(s) from %r." % (n, stage))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="metrics",
                    help="订阅的 stage：metrics / preproc.out / npu.raw / postproc.out")
    ap.add_argument("--count", type=int, default=5, help="取多少条样本后退出")
    ap.add_argument("--sample-every", type=int, default=1,
                    help="每 N 帧采一次（服务端下采样，减负载）")
    args = ap.parse_args()
    sample_stage(args.stage, args.count, args.sample_every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
