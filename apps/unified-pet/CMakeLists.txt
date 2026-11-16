cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.3.8-unified-foundation")
get_filename_component(PET_REPO "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
list(APPEND EXTRA_COMPONENT_DIRS
    "${PET_REPO}/components/pet_tls"
    "${PET_REPO}/components/pet_touch_calibration"
    "${PET_REPO}/components/tailtiny"
    "${PET_REPO}/components/tailtiny/components/wireguard_lwip")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(unified_pet)
idf_build_get_property(pet_idf_version IDF_VER)
if(NOT pet_idf_version STREQUAL "v5.5.4")
    message(FATAL_ERROR "Unified Pet requires the pinned IDF 5.5.4 SDK")
endif()
idf_component_get_property(touch_lib pet_touch_calibration COMPONENT_LIB)
target_compile_definitions(${touch_lib} PRIVATE PET_TOUCH_STORE_NAMESPACE="unified_pet")