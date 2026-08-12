# librecamera_ext -- reCamera Pro extension SDK (C ABI v1), soname .so.1.
# In-tree build (spec §6, G7 for final publish form). Not wired into the rkipc
# app build; built separately against the shared protobuf-c sources.
cmake_minimum_required(VERSION 3.5)
project(librecamera_ext C)

# recamera_ipc/common. This file lives at recamera_ipc/sdk/librecamera_ext/.
get_filename_component(RKIPC_COMMON "${CMAKE_CURRENT_SOURCE_DIR}/../../common" ABSOLUTE)

add_library(recamera_ext SHARED
	recamera_ext.c
	frame_recv.c
	probe_recv.c
	ext_client_common.c
	${RKIPC_COMMON}/rc_notify/inference.pb-c.c
	${RKIPC_COMMON}/rc_ext_core/ext_api.pb-c.c
)

target_include_directories(recamera_ext PUBLIC
	${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_include_directories(recamera_ext PRIVATE
	${RKIPC_COMMON}/rc_notify
	${RKIPC_COMMON}/rc_ext_core
)

target_link_libraries(recamera_ext protobuf-c)

set_target_properties(recamera_ext PROPERTIES
	OUTPUT_NAME recamera_ext
	VERSION 1.0.0
	SOVERSION 1
)

install(TARGETS recamera_ext LIBRARY DESTINATION lib)
install(FILES include/recamera_ext.h DESTINATION include)
