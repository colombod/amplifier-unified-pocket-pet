idf_component_register(SRCS "main.c" "network.c" "audio_test.c" "live_voice.c" "touch_calibration.c" INCLUDE_DIRS "."
    REQUIRES esp_driver_i2s esp_codec_dev esp_app_format esp_wifi esp_event esp_netif esp_http_server nvs_flash json lwip esp_websocket_client mbedtls esp_timer
    EMBED_FILES "setup.html")
# Execute only after IDF's early component-dependency pass has returned.
# Generic EMBED_FILES provides byte alignment; RGB565 needs aligned loads.
configure_file("${CMAKE_CURRENT_LIST_DIR}/pet_asset.S.in"
    "${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S" @ONLY)
set_source_files_properties("${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S" PROPERTIES
    OBJECT_DEPENDS "${CMAKE_SOURCE_DIR}/assets/generated/tachikoma.rgb565")
target_sources(${COMPONENT_LIB} PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S")
