# 05 — cpp-frame：C ABI 拿帧（可选）

给要极致性能 / 纯 C 栈的方案商。用 C ABI `rc_ext_frame_*` 抓 N 帧，把灰度 Y 平面写成 PGM。Python 版见示例 01；两者行为一致，这里少一层 ctypes 封装。

## 标准循环

```c
int err;
rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);   // NULL = NPU 同款默认
rc_ext_frame_buf_t f;
int rc;
while ((rc = rc_ext_frame_next(h, &f, 1000)) >= 0) {
    if (rc == 1) continue;                 // 超时，重试
    void *y = rc_ext_frame_map(h, &f);     // mmap + DMA_BUF_IOCTL_SYNC(START|READ)
    // 用 f.plane[i].offset/stride/vstride、f.pts_us、f.width/height ...
    rc_ext_frame_release(h, &f);           // 必须：SYNC END + 归还 buffer + close fd
}
rc_ext_frame_close(h);
```

要点（`recamera_ext.h` 注释）：

- `rc_ext_frame_next` 返回值：`0` = `*out` 有一帧；`1` = 超时重试；`<0` = `-rc_ext_err_t`（EOF/传输错误或协议违规），应停止并 `close`。
- `rc_ext_frame_map` 返回 **plane[0]（Y）首地址**，内部已做 dma-buf cache 同步。
- **每帧必须 `rc_ext_frame_release`**（C 侧不像 Python 迭代那样自动释放）——否则背压达上限（`max_outstanding`，默认 2），持续 5 秒会被服务端断开（EBACKPRESSURE）。
- **不要按 width/height 推 layout**：用 `f.plane[i].stride/vstride` 的真实值（含对齐补齐，`stride ≥ width`）。

## 依赖 / 交叉编译

- SDK 头文件 `recamera_ext.h`（`sdk/librecamera_ext/include/`）。
- aarch64 交叉编译器：reCamera Pro (RV1126B) SDK 用前缀 `aarch64-rockchip1240-linux-gnu-`。
- 链接期 `-lrecamera_ext`；运行期设备上 `librecamera_ext.so.1` 随固件安装在 `/lib` 或 `/usr/lib`。

编译（把 `recamera_ext.h` 拷到本目录，或用 `EXT_INCLUDE` 指到 SDK 的 include）：

```sh
cp <SDK>/librecamera_ext/include/recamera_ext.h .
make CROSS=/path/to/aarch64-rockchip1240-linux-gnu-
# 若交叉编译器已在 PATH：
make
```

> 交叉编译需要链接期能找到 `librecamera_ext.so`。若开发机没有，从设备拉一份：
> `adb pull /usr/lib/librecamera_ext.so.1 .` 再 `ln -s librecamera_ext.so.1 librecamera_ext.so`，
> 编译时加 `LDFLAGS="-L. -lrecamera_ext"`（或 `make LDFLAGS='-L. -lrecamera_ext'`）。

## 怎么跑

```sh
adb push frame_dump /root/
adb shell '/root/frame_dump 3 /tmp/frame'   # 抓 3 帧到 /tmp/frame_*.pgm
```

参数：`argv[1]` 帧数（默认 3），`argv[2]` 输出前缀（默认 `frame`）。

## 预期输出

```
subscribed: 640x640 fourcc=0x3231564e pool_depth=6 max_outstanding=2
saved /tmp/frame_00042.pgm  (seq=42 pts_us=123456789 640x640)
saved /tmp/frame_00043.pgm  (seq=43 pts_us=123489789 640x640)
saved /tmp/frame_00044.pgm  (seq=44 pts_us=123522789 640x640)
done, 3 frame(s) saved.
```

## 常见问题

- **链接报 `-lrecamera_ext` 找不到**：从设备拉 `.so.1` 并建 `.so` 软链，见上「交叉编译」注。
- **运行报找不到 `.so.1`**：设 `LD_LIBRARY_PATH` 指向它所在目录。
- **`rc_ext_frame_open failed: err=3`（EBUSY）**：订阅数达上限或 NPU 通道未启用。
- **`err=1`（EVERSION）**：客户端/服务端版本区间无交集（固件太老或不含扩展 API）。
- **跑一会儿 `rc_ext_frame_next` 返回负值断开**：多半是 release 没跟上（背压 EBACKPRESSURE=-5）。确认每帧都 `rc_ext_frame_release`。
