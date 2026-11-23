# Host (linux-target) build of the PRODUCTION voice owner and transport on the
# pinned lwIP/mbedTLS/esp-tls/tcp_transport/WebSocket-client sources.
# Built and run only through run_link_rig.py; never part of the firmware image.
cmake_minimum_required(VERSION 3.16)
if(NOT DEFINED ENV{PET_WS_CLIENT_DIR} OR NOT DEFINED ENV{PET_APP_MAIN})
    message(FATAL_ERROR "Use tests/link_rig/run_link_rig.py")
endif()
set(EXTRA_COMPONENT_DIRS "$ENV{PET_WS_CLIENT_DIR}")
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(pet_link_rig)
# Host va_list ABI only; see main/rig_va_shim.h. The pinned source is not edited.
idf_component_get_property(pet_ws_lib esp_websocket_client COMPONENT_LIB)
target_compile_options(${pet_ws_lib} PRIVATE -include "${CMAKE_SOURCE_DIR}/main/rig_va_shim.h")
