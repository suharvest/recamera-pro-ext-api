# 05-cpp-frame -- 交叉编译 frame_dump 到 reCamera Pro (aarch64)。
#
# 工具链前缀：reCamera Pro (RV1126B) SDK 用 aarch64-rockchip1240-linux-gnu-。
# 把 CROSS 指向你的 SDK 里那套交叉编译器，把 EXT_INCLUDE 指向 recamera_ext.h。
#
# 用法：
#   make CROSS=/path/to/aarch64-rockchip1240-linux-gnu- \
#        EXT_INCLUDE=/path/to/sdk/librecamera_ext/include
#
# 若交叉编译器已在 PATH 里，只需：
#   make EXT_INCLUDE=/path/to/librecamera_ext/include

# 交叉编译器前缀（末尾带 '-'）。默认取 PATH 中的同名编译器。
CROSS       ?= aarch64-rockchip1240-linux-gnu-
CC          := $(CROSS)gcc

# recamera_ext.h 所在目录。默认 = 当前目录：把 SDK 的 recamera_ext.h
# 拷到本目录即可（cp <SDK>/librecamera_ext/include/recamera_ext.h .），
# 或用 EXT_INCLUDE=... 指向 SDK 的 include 目录。
EXT_INCLUDE ?= .

CFLAGS  += -O2 -Wall -Wextra -I$(EXT_INCLUDE)
# 链接设备上随固件安装的 librecamera_ext.so.1。链接期只需 -lrecamera_ext；
# 运行期在设备上 dlopen 到 /lib 或 /usr/lib 下的 .so.1。
LDFLAGS += -lrecamera_ext

TARGET := frame_dump

$(TARGET): frame_dump.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

.PHONY: clean
