cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.3.21-amplifier")
if(PET_PRIVATE_MIC_DIAG)
    set(PROJECT_VER "0.3.21-amplifier-micdiag")
    # Private configuration must never seed the normal sdkconfig.
    set(SDKCONFIG "${CMAKE_CURRENT_LIST_DIR}/build-bench/sdkconfig")
    set(SDKCONFIG_DEFAULTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults;${CMAKE_CURRENT_LIST_DIR}/sdkconfig.bench.defaults")
endif()
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

# Board-scoped software-reset wait. The offline cache and the component
# manager use different target names for the same pinned Waveshare BSP.
if(TARGET __idf_waveshare__esp32_s3_touch_amoled_1_8)
    set(pet_bsp __idf_waveshare__esp32_s3_touch_amoled_1_8)
elseif(TARGET __idf_esp32_s3_touch_amoled_1_8)
    set(pet_bsp __idf_esp32_s3_touch_amoled_1_8)
else()
    message(FATAL_ERROR "Expected the Waveshare AMOLED 1.8 board component")
endif()
target_compile_definitions(${pet_bsp} PRIVATE esp_lcd_panel_reset=pet_amoled_panel_reset)