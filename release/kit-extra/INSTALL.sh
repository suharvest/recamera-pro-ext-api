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

echo ""
echo "==> 安装完成。运行前设置环境变量:"
echo ""
echo "  export PYTHONPATH=/userdata/local:/userdata/sdk/python"
echo "  export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:\$LD_LIBRARY_PATH"
echo ""
echo "==> 验证 (烟雾 demo,需已装含扩展 API 的固件,frame/result-in.sock 在):"
echo "  cd $HERE/examples/02-inject-result"
echo "  PYTHONPATH=/userdata/local:/userdata/sdk/python \\"
echo "  LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib \\"
echo "    python3 inject_result.py --task detection"
echo ""
echo "  然后 RTSP (rtsp://<ip>:8554/...) 或 WS 里应看到注入的框。"
