idf_component_register(SRCS "main.c" "network.c" "audio_test.c" "live_voice.c" "bridge_voice.c" "bridge_protocol.c" "bridge_transport.c" "tailnet_spike.c" "tailnet_tls.c" INCLUDE_DIRS "."
    REQUIRES pet_tls pet_touch_calibration esp_driver_i2s esp_codec_dev esp_app_format esp_wifi esp_event esp_netif esp_http_server nvs_flash json lwip esp_websocket_client tcp_transport esp-tls mbedtls esp_timer pthread tailtiny esp_driver_usb_serial_jtag
    EMBED_FILES "setup.html")
# Execute only after IDF's early component-dependency pass has returned.
# Generic EMBED_FILES provides byte alignment; RGB565 needs aligned loads.
configure_file("${CMAKE_CURRENT_LIST_DIR}/pet_asset.S.in"
    "${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S" @ONLY)
set_source_files_properties("${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S" PROPERTIES
    OBJECT_DEPENDS "${CMAKE_SOURCE_DIR}/assets/generated/amplifier-assistant.rgb565")
target_sources(${COMPONENT_LIB} PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/pet_asset.S")
if(PET_PRIVATE_MIC_DIAG)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PET_PRIVATE_MIC_DIAG=1)
endif()
# Deliberate pinned 5.5.4 ABI dependency for the connect-only fence. The public
# API cannot install a proxy get_socket callback; never compile a -1 FD chain.
idf_component_get_property(pet_tcp_dir tcp_transport COMPONENT_DIR)
target_include_directories(${COMPONENT_LIB} PRIVATE "${pet_tcp_dir}/private_include")
