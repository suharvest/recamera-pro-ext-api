// 05-cpp-frame -- C ABI 拿帧的最小示例：抓 N 帧，把灰度 Y 平面写成 PGM。
//
// 给要极致性能 / 纯 C 栈的方案商。演示 rc_ext_frame_* 的标准循环：
//   open -> next -> map -> (用 Y 平面) -> release -> ... -> close
//
// API 逐一核实自 sdk/librecamera_ext/include/recamera_ext.h：
//   rc_ext_frame_open(cfg, &err)                        (L186)
//   rc_ext_frame_geometry(h, &w,&h,&fourcc,&pd,&mo)     (L190)
//   rc_ext_frame_next(h, &buf, timeout_ms)  0=帧 1=超时 <0=错误  (L202)
//   rc_ext_frame_map(h, &buf) -> Y 平面首地址           (L208)
//   rc_ext_frame_release(h, &buf)                       (L213)
//   rc_ext_frame_close(h)                               (L216)
//   结构体 rc_ext_frame_buf_t / rc_ext_plane_t          (L148-172)
//
// 编译见 Makefile（交叉编译到设备的 aarch64）。
// 运行：  ./frame_dump [帧数] [输出前缀]
//   例：  ./frame_dump 3 /tmp/frame

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recamera_ext.h"

// 把一帧的 Y 平面写成 PGM（P5，二进制灰度）。yptr 指向 plane[0] 起始。
static int write_pgm(const char *path, const uint8_t *yptr,
                     const rc_ext_plane_t *yplane, uint32_t width, uint32_t height) {
	FILE *f = fopen(path, "wb");
	if (!f) {
		perror("fopen");
		return -1;
	}
	fprintf(f, "P5\n%u %u\n255\n", width, height);
	// 逐行拷贝 width 字节，跳过每行的 stride 补齐部分。
	for (uint32_t r = 0; r < height; r++) {
		const uint8_t *row = yptr + (size_t)r * yplane->stride;
		fwrite(row, 1, width, f);
	}
	fclose(f);
	return 0;
}

int main(int argc, char **argv) {
	int want = (argc > 1) ? atoi(argv[1]) : 3;
	const char *prefix = (argc > 2) ? argv[2] : "frame";

	int err = 0;
	// cfg = NULL -> NPU 同款默认（NV12，NPU 分辨率）。
	rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
	if (!h) {
		fprintf(stderr, "rc_ext_frame_open failed: err=%d\n", err);
		return 1;
	}

	uint32_t w = 0, ht = 0, fourcc = 0, pool_depth = 0, max_out = 0;
	rc_ext_frame_geometry(h, &w, &ht, &fourcc, &pool_depth, &max_out);
	printf("subscribed: %ux%u fourcc=0x%08x pool_depth=%u max_outstanding=%u\n",
	       w, ht, fourcc, pool_depth, max_out);

	int saved = 0;
	rc_ext_frame_buf_t f;
	int rc;
	while (saved < want && (rc = rc_ext_frame_next(h, &f, 1000)) >= 0) {
		if (rc == 1)
			continue;  // 超时，重试

		void *yptr = rc_ext_frame_map(h, &f);  // 内部做 dma-buf cache 同步
		if (yptr) {
			char path[256];
			snprintf(path, sizeof(path), "%s_%05llu.pgm", prefix,
			         (unsigned long long)f.seq);
			// f.plane[0] = Y；用它的真实 stride，不要按 width 推。
			if (write_pgm(path, (const uint8_t *)yptr, &f.plane[0], f.width, f.height) == 0) {
				printf("saved %s  (seq=%llu pts_us=%llu %ux%u)\n", path,
				       (unsigned long long)f.seq, (unsigned long long)f.pts_us,
				       f.width, f.height);
				saved++;
			}
		}
		// 必须 release：SYNC END + 归还 buffer + close fd。
		rc_ext_frame_release(h, &f);
	}
	if (rc < 0)
		fprintf(stderr, "rc_ext_frame_next returned %d (-rc_ext_err_t)\n", rc);

	rc_ext_frame_close(h);
	printf("done, %d frame(s) saved.\n", saved);
	return 0;
}
