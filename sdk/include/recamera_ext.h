// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI v1 (spec §3.5, §2.5) -- result-injection sink +
// frame-source receiver. soname: librecamera_ext.so.1
#ifndef RECAMERA_EXT_H
#define RECAMERA_EXT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ===========================================================================
// M1 result injection (spec §3.5)
// ===========================================================================

// Opaque result-sink handle.
typedef struct rc_ext_result rc_ext_result_t;

// A single detection box. Coordinates are pixels (top-left / bottom-right).
typedef struct {
	float x1;
	float y1;
	float x2;
	float y2;
	float score;       // confidence, 0..1
	const char *label; // class name; NULL -> ""
	int class_id;
} rc_ext_box_t;

// Opens a connection to /run/recamera/result-in.sock and performs the
// Hello/HelloAck handshake. source_id is advisory (the server overrides it with
// the peercred-derived identity). On failure returns NULL and, if err != NULL,
// sets *err to an rc_ext_err_t code; on success sets *err to 0.
rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err);

// Sends one detection result (task_type = DETECTION) with n boxes at pts_us
// (CLOCK_MONOTONIC microseconds; 0 = no frame association). Returns 0 on
// success or a negative rc_ext_err_t on failure.
int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
                                  const rc_ext_box_t *boxes, size_t n);

// --- other task types (spec §3.5) -------------------------------------------
// Each send_* below packs the matching InferenceResult oneof and sends one
// datagram, mirroring rc_ext_result_send_detections: pts_us is CLOCK_MONOTONIC
// microseconds (0 = no frame association); returns 0 on success or a negative
// rc_ext_err_t. All const char* labels accept NULL (treated as "").

// One classification entry (top-k). label may be NULL.
typedef struct {
	float score;       // confidence, 0..1
	int class_id;
	const char *label; // class name; NULL -> ""
} rc_ext_class_t;

// task_type = CLASSIFICATION.
int rc_ext_result_send_classification(rc_ext_result_t *h, uint64_t pts_us,
                                      const rc_ext_class_t *items, size_t n);

// One segmentation entry: an optional ROI box plus a row-major mask buffer.
// mask points to mask_w*mask_h bytes (may be NULL with mask_w=mask_h=0).
typedef struct {
	float x1;
	float y1;
	float x2;
	float y2;
	float score;
	int class_id;
	const char *label;
	const uint8_t *mask; // row-major mask, mask_w*mask_h bytes; NULL -> empty
	int mask_w;
	int mask_h;
} rc_ext_seg_t;

// task_type = SEGMENTATION.
int rc_ext_result_send_segmentation(rc_ext_result_t *h, uint64_t pts_us,
                                    const rc_ext_seg_t *items, size_t n);

// One tracking entry: a detection box plus a persistent track_id.
typedef struct {
	float x1;
	float y1;
	float x2;
	float y2;
	float score;
	int class_id;
	const char *label;
	int track_id;
} rc_ext_track_t;

// task_type = TRACKING.
int rc_ext_result_send_tracking(rc_ext_result_t *h, uint64_t pts_us,
                                const rc_ext_track_t *items, size_t n);

// One keypoint within an instance.
typedef struct {
	float x;
	float y;
	float score;      // keypoint confidence (not object confidence)
	int keypoint_id;  // index in the caller-owned keypoint schema
} rc_ext_point_t;

// One detected instance: optional object box/label (set has_box=0 to omit the
// whole object_info group, per proto) plus its keypoints.
typedef struct {
	int has_box;         // 0 -> omit object box/score/class/label entirely
	float x1;
	float y1;
	float x2;
	float y2;
	float score;         // object confidence
	int class_id;
	const char *label;
	const rc_ext_point_t *points;
	size_t n_points;
} rc_ext_kpinstance_t;

// task_type = KEYPOINTS.
int rc_ext_result_send_keypoints(rc_ext_result_t *h, uint64_t pts_us,
                                 const rc_ext_kpinstance_t *instances, size_t n);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_result_close(rc_ext_result_t *h);

// ===========================================================================
// M2 frame proxy -- zero-copy frame receiver (spec §2.5)
// ===========================================================================
//
//   rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
//   rc_ext_frame_buf_t f;
//   while (rc_ext_frame_next(h, &f, 1000) == 0) {
//       void *y = rc_ext_frame_map(h, &f);   // mmap + DMA_BUF_IOCTL_SYNC START
//       // ... read f.plane[i].offset/stride/vstride, f.pts_us ...
//       rc_ext_frame_release(h, &f);          // SYNC END + release + close fd
//   }
//   rc_ext_frame_close(h);

#define RC_EXT_FRAME_MAGIC  0x52434652u // "RCFR"
#define RC_EXT_FRAME_VER    1u
#define RC_EXT_FOURCC_NV12  0x3231564Eu // 'N','V','1','2' little-endian

// Opaque frame-source connection handle.
typedef struct rc_ext_frame rc_ext_frame_t;

// One image plane's layout within the shared dma-buf (spec §2.3). Clients must
// use these values verbatim and never derive layout from width/height.
typedef struct {
	uint32_t offset;  // byte offset from the dma-buf start
	uint32_t stride;  // row stride in bytes
	uint32_t vstride; // rows incl. alignment padding
} rc_ext_plane_t;

// A borrowed frame: the header fields carried in the 96-byte wire header plus
// the dma-buf fd. Owned by the caller between rc_ext_frame_next() and
// rc_ext_frame_release(). Do NOT memcpy or reuse across frames -- pass the same
// object to map()/release().
typedef struct {
	uint64_t seq;     // monotonic; a gap means the server dropped frames
	uint64_t pts_us;  // VI PTS (CLOCK_MONOTONIC microseconds)
	uint32_t width;   // valid pixels
	uint32_t height;
	uint32_t fourcc;  // RC_EXT_FOURCC_NV12
	uint32_t buf_size;// total valid dma-buf length
	uint16_t flags;   // bit0: a frame was dropped before this one
	uint8_t chn_id;
	uint8_t n_planes; // NV12 = 2
	rc_ext_plane_t plane[3]; // NV12: plane[0]=Y, plane[1]=UV
	int fd;           // dma-buf fd (>= 0 while borrowed; -1 once released)
	void *_base;      // internal: mmap base (NULL until mapped)
	size_t _map_len;  // internal: mmap length
} rc_ext_frame_buf_t;

// Optional subscription config; pass NULL for the NPU-matched defaults.
typedef struct {
	uint32_t width;       // 0 = default
	uint32_t height;      // 0 = default
	uint32_t fourcc;      // 0 = NV12
	uint32_t fps_divisor; // 0/1 = every frame, 2 = every other, ...
} rc_ext_frame_cfg_t;

// Connects to /run/recamera/frame.sock, performs the Hello/HelloAck handshake
// and sends a FrameSubscribe, blocking for the FrameSubscribeAck. On failure
// returns NULL and, if err != NULL, sets *err to an rc_ext_err_t; on success
// sets *err to 0.
rc_ext_frame_t *rc_ext_frame_open(const rc_ext_frame_cfg_t *cfg, int *err);

// Effective subscription geometry from the FrameSubscribeAck (valid after
// open). Any out pointer may be NULL. Returns 0.
int rc_ext_frame_geometry(rc_ext_frame_t *h, uint32_t *width, uint32_t *height,
                          uint32_t *fourcc, uint32_t *pool_depth,
                          uint32_t *max_outstanding);

// Waits up to timeout_ms for the next frame. Receives one SEQPACKET datagram
// (96-byte header + exactly one dma-buf fd via SCM_RIGHTS, MSG_CMSG_CLOEXEC)
// and fills *out. Returns:
//   0  -> a frame is in *out (fd valid, not yet mapped)
//   1  -> timeout, no frame available (retry)
//  <0  -> -rc_ext_err_t: EOF/transport error (-EINTERNAL) or a protocol
//         violation (-EFORMAT; all received fds already closed).
// A negative return means the caller should stop and close the handle.
int rc_ext_frame_next(rc_ext_frame_t *h, rc_ext_frame_buf_t *out, int timeout_ms);

// mmap()s the frame's dma-buf (PROT_READ, MAP_SHARED) and issues
// DMA_BUF_IOCTL_SYNC(START|READ) so the CPU sees coherent data. Returns a
// pointer to the start of plane[0] (Y for NV12), or NULL on failure. Idempotent
// per frame. The mapping stays valid until rc_ext_frame_release().
void *rc_ext_frame_map(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// Ends CPU access: DMA_BUF_IOCTL_SYNC(END|READ) if mapped, munmap, sends the
// 8-byte release seq back to the server, and closes the dma-buf fd. NULL-safe;
// idempotent (a released frame has f->fd == -1).
void rc_ext_frame_release(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_frame_close(rc_ext_frame_t *h);

#ifdef __cplusplus
}
#endif

#endif // RECAMERA_EXT_H
