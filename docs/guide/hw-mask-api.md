# 硬件隐私遮罩 API（`rc_ext_mask_*` / `MaskControl`）

> 状态：**固件与 SDK 已实施、符号验证通过；线 B 冷启动真机验证通过（增量移动不闪、6 块隔离、回滚原厂均确认）。**
> 事实来源：实施 Spec `../../internal/HW_MASK_API_SPEC.md`（端侧 rkipc + `librecamera_ext` C ABI + Python 全套设计与 file:line）。本文是**给方案商的用法文档**，不含端侧实现细节，改动详情看 Spec。
>
> **部署方式（重要）**：遮罩生效必须走**冷启动**——改 `RkLunch.sh` 指向 `/userdata/rkipc.mask` 后 `reboot`。**热替换（bind mount 覆盖运行中 rkipc）会触发 VPSS oops**，必须冷启动或 OTA，详见 §7。

## 0. 定位

reCamera Pro 的 rkipc 在 VI 层有硬件 COVER 遮块（纯色矩形，NPU/编码**之前**生效——被遮区域不进推理、不进码流、不进录像）。本 API 把它暴露成**设备本机应用可调**的 SDK 接口，核心能力是**位置增量更新不闪**：

- 现状全量通路每次遮罩变化都 destroy+rebuild 整组，移动时会闪。
- 本 API 增加一条"仅移动"增量通路：单块就地改矩形，不 destroy/create、**不落盘**。适合遮罩跟随目标平移（打码跟人）等高频场景。

与软件叠加（canvas 画框）不同：硬件遮罩是**真实挡住画面**，进不了码流/录像/推理；软件叠加只在浏览器画，见 [ai-result-overlay.md](./ai-result-overlay.md)。

## 1. 接入方式（同一套契约，C / Python）

与其余扩展 API 一致（见 [README.md](./README.md) §接入）：C ABI `rc_ext_mask_*`（`sdk/include/recamera_ext.h` 的 M4 段，链接 `librecamera_ext.so.1`）；Python `MaskControl` / `MaskRect`（`sdk/python/recamera_ext/`，ctypes 薄封装同一 `.so`）。传输上 mask 组走 rkipc RPC `/var/tmp/rkipc`（请求/响应型），与结果注入的 `result-in.sock` 是两条独立连接。

- 权限：与其余 socket API 同——v1 root-only，扩展应用经启动脚本以 root 拉起。
- 坐标：**一律归一化 [0,1] 分数**（相对帧宽高），库端 + 端侧双重 clamp。

## 2. C ABI（M4 段）

```c
typedef struct rc_ext_mask rc_ext_mask_t;

typedef struct {
    int   id;   // 槽位 id，[0,6)；对应硬件 COVER handle 8+id
    float x, y; // 左上角，[0,1] 分数
    float w, h; // 宽/高，(0,1] 分数
} rc_ext_mask_rect_t;

rc_ext_mask_t *rc_ext_mask_open(int *err);                 // 连 rkipc + 握手
int  rc_ext_mask_set(rc_ext_mask_t*, const rc_ext_mask_rect_t*, size_t n, int *applied); // 全量（落盘）
int  rc_ext_mask_update(rc_ext_mask_t*, const rc_ext_mask_rect_t*);   // 增量单块（不闪/不落盘）
int  rc_ext_mask_clear(rc_ext_mask_t*);                    // 清空（全量/落盘）
int  rc_ext_mask_query(rc_ext_mask_t*, rc_ext_mask_rect_t *out, size_t n); // 读当前生效
void rc_ext_mask_close(rc_ext_mask_t*);                    // NULL-safe
```

失败返回负 `-rc_ext_err_t`（错误码枚举同 `rc_ext_result_*`，见 README.md 错误码表）。

## 3. Python（`MaskControl`）

```python
from recamera_ext import MaskControl, MaskRect

with MaskControl() as mc:
    mc.set([MaskRect(id=0, x=0.1, y=0.1, w=0.3, h=0.2)])   # 建一块（全量，落盘）
    for x in track_x():                                     # 目标平移
        mc.update(MaskRect(id=0, x=x, y=0.1, w=0.3, h=0.2)) # 增量移动，不闪
    print(mc.query())                                       # -> [MaskRect, ...]
```

## 4. 用法纪律（务必遵守）

- **`set`/`clear` 落盘，`update` 不落盘。** 增量移动每次调用即硬件生效但**只在内存**——高频移动不写 flash（避免磨损）。要持久化"最终位置"，在停止移动/定稿时**显式调一次 `set`** 落盘。重启后恢复的是最后一次 `set` 的布局。
- **`update` 前该块必须已 `set` 建好。** id 越界（∉[0,6)）或该槽未创建 → `update` 返回负码；约定此时**回退到 `set`**（全量重建）。块数增/减、enable/disable 整个 mask 也必须走 `set`，不能用 `update`。
- **节流放应用侧**，建议 ≤30 Hz：`update` 走端侧互斥锁 + 一次 MPI 调用，过高频率会与编码/VI 抢锁。端侧不做丢帧节流（保持"每次调用即生效"）。
- **数量上限 6，超额截断不静默**：`set` 传 n>配额时截断，`applied` 回实际生效数；`query` 返回值可 > 传入 n 表示被截断。

## 5. auto / manual 名额配额

6 个硬件 COVER 槽位在 SDK 应用与 web 前端手动遮罩之间**静态配额隔离**（Spec §9）：

| 区 | id 区间 | 占用方 |
|---|---|---|
| manual（手动） | `[0, 3)` | web 前端 / 用户手动隐私遮罩，优先保留 |
| auto（SDK） | `[3, 6)` | 方案商经 SDK 占用 |

- `rc_ext_mask_*` 只写 auto 区（id≥3）；传超过 3 块 → 截断，`applied` 回实际数。
- 两区互不覆盖：web 全量重建只动 manual 区，不动 auto 区。
- 默认 `K=3`（手动 3 + SDK 3）。取舍：静态配额行为可预期，代价是配额写死——隐私场景不接受"SDK 挤掉手动遮罩"，故不采动态先到先得。

## 6. 真机验证（线 B 冷启动，已验证）

固件已编好、符号验证通过；**线 B 冷启动真机验证通过**（Spec §8 计划项落地结果）：

- **冷启动方式成立**：改 `RkLunch.sh` 指向 `/userdata/rkipc.mask` + `reboot`，遮块随新 rkipc 起来即生效（避开热替换的 VPSS oops，见 §7）。
- **增量移动不闪**：`MaskControl.set` 建 2 块后，`update_mask_rect` 增量移动 3 次——遮块区域 std 塌成纯色、每帧无 dropout（无"遮块缺失"帧），**不闪**。
- **回滚原厂成功**：改回原 `RkLunch.sh` + reboot 恢复原厂 rkipc，无残留。

**填充色实测**：遮块填充为 **`0x818181`（129 灰）**，非 Spec/memory 早期假设的 `0x727272`（114 灰）。不影响功能（遮块照常挡画面/挡推理/挡码流），仅记录以对齐——memory 里的 `0x727272` 可能已过时（**待核实**是否两处色值并存或已统一）。

> 6 块截断（传 8 块 `applied==6`）、`update` 未触发 `rk_param_save`（不写 flash）、25 Hz 长稳这几项以本轮冷启动验证的实测为准补齐；未实测的具体帧率/时延数字不引用。

## 7. 部署：必须冷启动，热替换会 VPSS oops（教训）

带遮罩改动的 rkipc **不能热替换到运行中的进程**：bind mount 覆盖运行中的 `/oem/usr/bin/rkipc`（或 kill 后原地换二进制）会触发 **VPSS oops**——VI/VPSS 层状态与新 rkipc 的 COVER region 生命周期对不齐，内核崩。

**唯一验证过的部署路径 = 冷启动**：

1. 把新二进制放到 `/userdata/rkipc.mask`。
2. 改 `RkLunch.sh` 让开机拉起的 rkipc 指向 `/userdata/rkipc.mask`。
3. `reboot`——新 rkipc 从头初始化 VI/VPSS + COVER，遮块正常生效。
4. 回滚：改回原 `RkLunch.sh` + reboot。

生产分发同理走 OTA / 冷启动，不做运行时热替换。这一条与结果注入/帧代理那几路（M1/M2 可热替换 `/userdata/rkipc.xxx` 直接跑）**不同**——遮罩碰的是 VI 层 COVER region，热替换不安全。
