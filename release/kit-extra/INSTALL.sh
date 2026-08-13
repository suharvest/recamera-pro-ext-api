#!/bin/sh
# reCamera Pro 扩展 API kit + SDK 一键安装器 (v1.2.0)
# 在设备上运行:  sh INSTALL.sh
#   - kit  -> /userdata/local/kit        (import kit 时 /userdata/local 在 sys.path)
#   - sdk  -> /userdata/sdk              (python + lib + 软链 + 头文件 + VERSION)
# 幂等:重复运行安全。安装前把已存在的旧 kit/sdk 备份为 *.bak.<时间戳>。
# 只写 /userdata,不碰固件 (/oem, rkipc, appmgr, nginx)。
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
KIT_DST=/userdata/local/kit
SDK_DST=/userdata/sdk
TS=$(date +%s)

backup() {  # $1 = path to back up if it exists
    if [ -e "$1" ]; then
        echo "  备份已存在的 $1 -> $1.bak.$TS"
        mv "$1" "$1.bak.$TS"
    fi
}

echo "==> 安装 kit -> $KIT_DST"
mkdir -p /userdata/local
backup "$KIT_DST"
mkdir -p "$KIT_DST"
cp -a "$HERE/kit/." "$KIT_DST/"

echo "==> 安装 SDK -> $SDK_DST"
# 设备约定布局:头文件在 $SDK_DST/recamera_ext.h,python 在 $SDK_DST/python,lib 在 $SDK_DST/lib
backup "$SDK_DST/python"
backup "$SDK_DST/lib"
backup "$SDK_DST/recamera_ext.h"
mkdir -p "$SDK_DST/python" "$SDK_DST/lib"
cp -a "$HERE/sdk/python/." "$SDK_DST/python/"
cp -a "$HERE/sdk/lib/."    "$SDK_DST/lib/"          # -a 保留 .so 软链
cp -a "$HERE/sdk/include/recamera_ext.h" "$SDK_DST/recamera_ext.h"
[ -f "$HERE/sdk/VERSION" ]   && cp -a "$HERE/sdk/VERSION"   "$SDK_DST/VERSION"   || true
[ -f "$HERE/sdk/README.md" ] && cp -a "$HERE/sdk/README.md" "$SDK_DST/README.md" || true

echo "==> provision Python 推理运行时 (rknnlite 2.3.2, best-effort)"
# 幂等 + best-effort：失败仅告警，不中断 kit/sdk 安装。
RKNNENV=/userdata/rknnenv
WHEELS="$HERE/wheels"
provision_rknnlite() {
  # 1) stock rknnlite 硬编码 /usr/lib/librknnrt.so
  if [ ! -e /usr/lib/librknnrt.so ]; then
    if [ -e /oem/usr/lib/librknnrt.so ]; then
      ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so && echo "  linked /usr/lib/librknnrt.so -> /oem/usr/lib/librknnrt.so"
    else
      echo "  WARN: /oem/usr/lib/librknnrt.so 缺失 -- rknnlite 无法加载"
    fi
  else
    echo "  /usr/lib/librknnrt.so 已存在"
  fi
  # 2) venv (--system-site-packages, numpy 用系统的)
  if [ ! -d "$RKNNENV" ]; then
    python3 -m venv --system-site-packages "$RKNNENV" && echo "  created venv $RKNNENV" || { echo "  WARN: venv 创建失败"; return 1; }
  else
    echo "  venv $RKNNENV 已存在"
  fi
  # 3) 离线装 wheel (设备无网)
  "$RKNNENV/bin/pip" install --no-index --find-links "$WHEELS" \
      rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib jinja2 markupsafe \
    && echo "  wheels 离线安装完成 ($WHEELS)" || { echo "  WARN: 离线 pip install 失败"; return 1; }
  # 4) 自检
  if LD_LIBRARY_PATH=/oem/usr/lib "$RKNNENV/bin/python3" \
       -c "from rknnlite.api import RKNNLite; RKNNLite(); print('rknnlite OK')"; then
    echo "  自检 PASSED"
  else
    echo "  WARN: rknnlite 自检失败 -- 上设备排查"
    return 1
  fi
  return 0
}
if [ -d "$WHEELS" ]; then
  if provision_rknnlite; then echo "  rknnlite runtime 已就绪"; else echo "  WARN: rknnlite provision 未完成 -- kit/sdk 安装不受影响"; fi
else
  echo "  WARN: 未找到 $WHEELS -- 跳过 rknnlite provision"
fi

echo ""
echo "==> 安装完成。运行前设置环境变量:"
echo ""
echo "  export PYTHONPATH=/userdata/local:/userdata/sdk/python"
echo "  export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:\$LD_LIBRARY_PATH"
echo ""
echo "  # 需要 NPU 推理的 vision app 用 rknnenv 里的 python (含 rknnlite):"
echo "  #   PYTHONPATH=/userdata/local:/userdata/sdk/python LD_LIBRARY_PATH=/oem/usr/lib /userdata/rknnenv/bin/python3 <app>.py"
echo ""
echo "==> 验证 (烟雾 demo,需已装含扩展 API 的固件,frame/result-in.sock 在):"
echo "  cd $HERE/examples/02-inject-result"
echo "  PYTHONPATH=/userdata/local:/userdata/sdk/python \\"
echo "  LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib \\"
echo "    python3 inject_result.py --task detection"
echo ""
echo "  然后 RTSP (rtsp://<ip>:8554/...) 或 WS 里应看到注入的框。"
