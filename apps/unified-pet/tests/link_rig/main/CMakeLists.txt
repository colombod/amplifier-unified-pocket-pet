# bridge_voice.c, bridge_transport.c and bridge_protocol.c are compiled from the
# application directory exactly as they are; nothing in them is replaced.
set(app "$ENV{PET_APP_MAIN}")
idf_component_register(
    SRCS "rig_main.c" "rig_seams.c" "rig_link.c" "rig_tap.c" "rig_probe.c"
         "${app}/bridge_voice.c" "${app}/bridge_transport.c" "${app}/bridge_protocol.c"
    INCLUDE_DIRS "." "${app}"
    PRIV_INCLUDE_DIRS "$ENV{IDF_PATH}/components/tcp_transport/private_include"
    REQUIRES esp_websocket_client lwip esp_netif esp-tls tcp_transport esp_event esp_timer
             freertos pthread mbedtls log json nvs_flash heap)
# On the device BIT0.. reach bridge_voice.c through SoC headers the linux target
# does not pull in. Supply the same SDK header; the source file is unchanged.
set_source_files_properties("${app}/bridge_voice.c" PROPERTIES COMPILE_OPTIONS "-include;esp_bit_defs.h;-include;${CMAKE_CURRENT_LIST_DIR}/rig.h")
# Duration probes at link seams; see rig_probe.c.
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=esp_websocket_client_send_text"
    "-Wl,--wrap=xQueueTakeMutexRecursive" "-Wl,--wrap=lwip_select")
